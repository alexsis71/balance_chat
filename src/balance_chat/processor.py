from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from time import perf_counter
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .binding import (
    CanonicalRelationNotFound,
    ContextBindingError,
    InterpretationMutationCompiler,
)
from .compat.envelope_translation import PipelineEnvelopeTranslator
from .contracts import (
    AnalysisIntent,
    AnalysisOperand,
    CanonicalEntityRef,
    ClarificationAnswer,
    ComparisonSpec,
    ContextContractV2,
    ContextMutation,
    EntityMention,
    FieldMutation,
    GroupingSpec,
    IntentPatch,
    InterpretationMode,
    MetadataVersionRef,
    MutationAction,
    OperandEntityRef,
    Operation,
    PeriodRef,
    ResultReference,
    ResultMemoryWrite,
    TransitionOutcome,
)
from .domain_invariants import CANONICAL_VOLUME_UNIT, FORBIDDEN_PLAN_FIELDS
from .execution import NativeExecutor
from .execution_adapter import ExecutionAdapter, ReducerExecutionAdapter
from .gating import EvidenceCompletenessError, RoutingEvidenceGate
from .grouping import CanonicalGroupAggregator, GroupingError, member_facts_from_rows
from .interpretation import HybridInterpretationPolicy, InterpretationError, UnifiedInterpreter
from .domain import interpretation_capabilities, metric_definition
from .observability import log_event
from .planning import NativeMultiOperandPlanner, PlanningError
from .reducer import ContextReductionError
from .result_memory import PipelineResultMemoryAdapter
from .service import TurnProcessResult, TurnProcessingError


LOGGER = logging.getLogger("balance_chat.processor")


_REVERSE_DIRECTION = re.compile(
    r"^\s*(?:(?:а|и)\s+)?(?:обратно|наоборот)\b",
    re.IGNORECASE,
)
_COMPARE_WITH_EXTREMUM = re.compile(
    r"\bсравн\w*\s+с\s+(?P<extremum>миним\w*|максим\w*)\b",
    re.IGNORECASE,
)


class PipelineV2TurnProcessor:
    """Production wiring for interpretation, binding, planning, execution and memory."""

    def __init__(
        self,
        *,
        runtime: Any,
        registry: Any,
        interpreter: UnifiedInterpreter,
        compiler: InterpretationMutationCompiler,
        executor: NativeExecutor,
        result_memory: PipelineResultMemoryAdapter | None = None,
        planner: NativeMultiOperandPlanner | None = None,
        policy: HybridInterpretationPolicy | None = None,
        gate: RoutingEvidenceGate | None = None,
        execution_adapter: ExecutionAdapter | None = None,
    ) -> None:
        self.runtime = runtime
        self.registry = registry
        self.interpreter = interpreter
        self.compiler = compiler
        self.executor = executor
        self.result_memory = result_memory
        self.planner = planner or NativeMultiOperandPlanner()
        self.gate = gate or RoutingEvidenceGate(policy)
        self.execution_adapter = execution_adapter or ReducerExecutionAdapter()
        self.translator = PipelineEnvelopeTranslator(registry)
        try:
            self._normalize_lemmas = runtime._import_pipeline_module(
                "pipeline_v2.nlp_ru"
            ).normalize_query_lemmas
        except Exception:
            self._normalize_lemmas = None
        try:
            self._analyze_query = runtime._import_pipeline_module(
                "pipeline_v2.query_analyzer"
            ).analyze_query
        except Exception:
            self._analyze_query = None

    def _materialize_mutation(
        self,
        state: ContextContractV2,
        mutation: ContextMutation,
    ) -> AnalysisIntent:
        try:
            return self.execution_adapter.effective_intent(state, mutation)
        except ContextReductionError as exc:
            raise TurnProcessingError(
                "context mutation reduction failed",
                code="context_reduction_failed",
            ) from exc

    def _dispatch_mutation(
        self,
        state: ContextContractV2,
        mutation: ContextMutation,
        *,
        normalized_message,
        execute_db,
        request_id,
        started,
        interpretation_mode,
        memory_chunks=(),
        decision=None,
        evidence_businesses=(),
        evidence_geos=(),
        effective_intent: AnalysisIntent | None = None,
    ) -> TurnProcessResult:
        intent = effective_intent
        if intent is None:
            intent = self._materialize_mutation(state, mutation)
        if intent.grouping or intent.operation == Operation.GROUP:
            return self._execute_grouping_mutation(
                state,
                mutation,
                effective_intent=intent,
                normalized_message=normalized_message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode=interpretation_mode,
                decision=decision,
                memory_chunks=memory_chunks,
                evidence_businesses=evidence_businesses,
                evidence_geos=evidence_geos,
            )
        return self._execute_mutation(
            state,
            mutation,
            effective_intent=intent,
            normalized_message=normalized_message,
            execute_db=execute_db,
            request_id=request_id,
            started=started,
            interpretation_mode=interpretation_mode,
            memory_chunks=memory_chunks,
            decision=decision,
            evidence_businesses=evidence_businesses,
            evidence_geos=evidence_geos,
        )

    def _synchronize_effective_intent(
        self,
        state: ContextContractV2,
        mutation: ContextMutation,
        *,
        previous_effective_intent: AnalysisIntent,
        normalized_effective_intent: AnalysisIntent,
    ) -> ContextMutation:
        if normalized_effective_intent == previous_effective_intent:
            return mutation
        synchronized = mutation.model_copy(
            update={
                "replace_intent": normalized_effective_intent,
                "patch": IntentPatch(),
            },
            deep=True,
        )
        if (
            self._materialize_mutation(state, synchronized)
            != normalized_effective_intent
        ):
            raise TurnProcessingError(
                "executed effective intent would differ from committed intent",
                code="context_reduction_failed",
            )
        return synchronized

    def process(
        self,
        state: ContextContractV2,
        *,
        message: str,
        execute_db: bool,
        clarification: ClarificationAnswer | None,
        request_id: str,
    ) -> TurnProcessResult:
        started = perf_counter()
        turn_id = str(uuid4())
        if clarification is not None:
            _validate_clarification_answer(state, clarification)
        if state.active_dialog_scope is not None:
            return self._process_contextual(
                state,
                message=message,
                execute_db=execute_db,
                clarification=clarification,
                request_id=request_id,
                turn_id=turn_id,
                started=started,
            )
        reverse = self._deterministic_reverse_mutation(state, message, turn_id)
        if reverse is not None:
            mutation, relation_found = reverse
            if not relation_found:
                return self._reverse_no_data(
                    state,
                    mutation,
                    request_id=request_id,
                    started=started,
                )
            return self._dispatch_mutation(
                state,
                mutation,
                normalized_message=mutation.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode="deterministic_reverse",
                memory_chunks=[],
            )
        extremum_comparison = _deterministic_extremum_comparison(
            state,
            message,
            turn_id,
        )
        if extremum_comparison is not None:
            return self._dispatch_mutation(
                state,
                extremum_comparison,
                normalized_message=extremum_comparison.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode="deterministic_extremum_comparison",
                memory_chunks=[],
            )
        direction_comparison = self._deterministic_direction_comparison_mutation(
            message, turn_id
        )
        if direction_comparison is not None:
            return self._dispatch_mutation(
                state,
                direction_comparison,
                normalized_message=direction_comparison.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode="deterministic_direction_comparison",
                memory_chunks=[],
                evidence_businesses=self._tagged_business_balances(message),
            )
        directed_flow_balance = self._deterministic_analyzed_flow_mutation(
            message, turn_id
        )
        if directed_flow_balance is not None:
            return self._dispatch_mutation(
                state,
                directed_flow_balance,
                normalized_message=directed_flow_balance.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode="deterministic_analyzed_directed_flow",
                memory_chunks=[],
            )
        if self._is_explicit_flow_balance_query(message):
            return self._standalone(
                state,
                message=message,
                execute_db=execute_db,
                request_id=request_id,
                turn_id=turn_id,
                started=started,
                interpretation_mode="deterministic_flow_balance_bridge",
            )
        explicit_geos = self._tagged_geo_objects(message)
        explicit_businesses = self._tagged_business_balances(message)
        business_mentions = self._tagged_business_entity_mentions(message)
        routing = self.gate.route(
            message,
            state,
            explicit_businesses=explicit_businesses,
            explicit_geos=explicit_geos,
            clarification_answer=clarification is not None,
        )
        log_event(
            LOGGER,
            logging.INFO,
            "routing_evidence_gate",
            request_id=request_id,
            route="interpret" if routing.invoke_interpreter else "legacy_scalar",
            reason=routing.reason,
            explicit_business_count=routing.explicit_business_count,
            explicit_geo_count=routing.explicit_geo_count,
        )
        balance_section = self._deterministic_balance_section_mutation(
            message,
            explicit_businesses,
            turn_id,
        )
        if balance_section is not None:
            return self._dispatch_mutation(
                state,
                balance_section,
                normalized_message=balance_section.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode="deterministic_balance_section",
                memory_chunks=[],
                evidence_businesses=explicit_businesses,
            )
        full_balance = self._deterministic_full_balance_mutation(
            message,
            explicit_businesses,
            turn_id,
        )
        if full_balance is not None:
            return self._dispatch_mutation(
                state,
                full_balance,
                normalized_message=full_balance.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode="deterministic_full_balance",
                memory_chunks=[],
                evidence_businesses=explicit_businesses,
            )
        directed_flow = self._deterministic_directed_flow_mutation(
            message,
            explicit_businesses,
            explicit_geos,
            turn_id,
        )
        if directed_flow is not None:
            return self._dispatch_mutation(
                state,
                directed_flow,
                normalized_message=directed_flow.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode="deterministic_directed_flow",
                memory_chunks=[],
                evidence_businesses=explicit_businesses,
                evidence_geos=explicit_geos,
            )
        mixed_metric_operands = self._matched_distribution_own_consumers(message)
        if mixed_metric_operands:
            return self._canonical_entity_comparison(
                state,
                message=message,
                operand_entities=mixed_metric_operands,
                operand_metrics=("distribution", "consumption"),
                execute_db=execute_db,
                request_id=request_id,
                turn_id=turn_id,
                started=started,
                interpretation_mode="deterministic_distribution_own_consumers",
            )
        role_separated = len(explicit_businesses) == 1 and len(explicit_geos) == 1
        if role_separated and routing.reason == "multiple_explicit_entities":
            return self._role_separated_standalone(
                state,
                message=message,
                balance=explicit_businesses[0],
                geos=explicit_geos,
                execute_db=execute_db,
                request_id=request_id,
                turn_id=turn_id,
                started=started,
            )
        peer_entities = self._matched_peer_entities(message)
        if peer_entities:
            return self._canonical_entity_comparison(
                state,
                message=message,
                operand_entities=peer_entities,
                execute_db=execute_db,
                request_id=request_id,
                turn_id=turn_id,
                started=started,
                interpretation_mode="deterministic_peer_entity",
            )
        grouping_query = _deterministic_grouping_query(state, message)
        if grouping_query is not None:
            scope = state.active_dialog_scope
            mutation = ContextMutation(
                turn_id=turn_id,
                user_message=message,
                normalized_message=grouping_query,
                replace_intent=AnalysisIntent(
                    operation=Operation.GROUP,
                    operands=[AnalysisOperand(
                        operand_id="grouped",
                        metric=scope.intent.operands[0].metric,
                        unit=scope.intent.operands[0].unit,
                    )],
                    periods=list(scope.intent.periods),
                    grouping=[GroupingSpec(dimension="geo_group")],
                ),
            )
            return self._dispatch_mutation(
                state,
                mutation,
                normalized_message=grouping_query,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode="deterministic_context_grouping",
                memory_chunks=[],
            )
        deterministic = _deterministic_period_mutation(state, message, turn_id)
        deterministic_mode = "deterministic_period"
        if deterministic is None:
            deterministic = self._deterministic_geo_mutation(state, message, turn_id)
            deterministic_mode = "deterministic_geo"
        if deterministic is not None:
            return self._dispatch_mutation(
                state,
                deterministic,
                normalized_message=deterministic.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode=deterministic_mode,
                memory_chunks=[],
            )
        if not routing.invoke_interpreter:
            return self._standalone(
                state,
                message=message,
                execute_db=execute_db,
                request_id=request_id,
                turn_id=turn_id,
                started=started,
            )

        memory_chunks = []
        if self.result_memory and state.metadata and state.result_references:
            memory_chunks = self.result_memory.retrieve(
                session_id=state.session_id,
                query=message,
                metadata_bundle_version=state.metadata.bundle_version,
                intent=(state.active_dialog_scope.intent if state.active_dialog_scope else None),
            )
        try:
            decision = self.interpreter.interpret(
                message=message,
                state=state,
                capabilities=_capabilities(),
                domain_hints=[
                    *(
                        f"explicit_business:{item.canonical_name}"
                        for item in explicit_businesses
                    ),
                    *(f"explicit_geo:{item.canonical_name}" for item in explicit_geos),
                    *_domain_hints(self.registry),
                ],
                metadata_bundle_version=self.registry.manifest.bundle_version,
                current_message_tags=[
                    *[
                        {
                            "tag": "BUSINESS_ENTITY",
                            "text": item.text,
                            "canonical_name": item.text,
                            "role_hint": item.role,
                        }
                        for item in business_mentions
                    ],
                    *[
                        {
                            "tag": "GEO",
                            "text": item.canonical_name,
                            "canonical_name": item.canonical_name,
                            "role_hint": "destination",
                        }
                        for item in explicit_geos
                    ],
                ],
                result_references=(
                    self.result_memory.for_interpretation(memory_chunks)
                    if self.result_memory
                    else []
                ),
                clarification_answer=(
                    clarification.model_dump(mode="json")
                    if clarification is not None else None
                ),
                request_id=request_id,
            )
        except InterpretationError as exc:
            raise TurnProcessingError(
                "interpretation contract validation failed",
                code="interpretation_contract_invalid",
            ) from exc
        if decision.mode == InterpretationMode.CLARIFY:
            if state.active_dialog_scope is None or decision.clarification is None:
                raise TurnProcessingError("clarification requires active context")
            return TurnProcessResult(
                mutation=ContextMutation(
                    turn_id=turn_id,
                    user_message=message,
                    normalized_message=decision.normalized_message,
                    replace_intent=state.active_dialog_scope.intent,
                ),
                outcome=TransitionOutcome.CLARIFICATION,
                clarification_questions=[decision.clarification.model_dump(mode="json")],
                diagnostics=_diagnostics(decision, memory_chunks, 0, started),
            )
        if decision.mode == InterpretationMode.UNSUPPORTED:
            raise TurnProcessingError(
                f"unsupported capability: {decision.unsupported_capability}"
            )
        try:
            mutation = self.compiler.compile(
                decision,
                state,
                turn_id=turn_id,
                user_message=message,
                current_entity_mentions=[
                    *business_mentions,
                    *[
                        EntityMention(
                            text=item.canonical_name, role="destination"
                        )
                        for item in explicit_geos
                    ],
                ],
            )
        except CanonicalRelationNotFound as exc:
            base = state.active_dialog_scope.intent
            attempted = _single_operand_attempt(base, exc.operand)
            mutation = ContextMutation(
                turn_id=turn_id,
                user_message=message,
                normalized_message=decision.normalized_message,
                replace_intent=attempted,
            )
            return self._reverse_no_data(
                state,
                mutation,
                request_id=request_id,
                started=started,
                interpretation_mode="conversation_graph",
                interpretation_source="qwen",
            )
        except ContextBindingError as exc:
            raise TurnProcessingError(
                "interpretation could not be bound to canonical metadata",
                code="interpretation_binding_failed",
            ) from exc
        effective_intent = self._materialize_mutation(state, mutation)
        decision, mutation, effective_intent = self._repair_incomplete_evidence(
            state,
            message=message,
            turn_id=turn_id,
            decision=decision,
            mutation=mutation,
            explicit_businesses=explicit_businesses,
            current_business_mentions=business_mentions,
            explicit_geos=explicit_geos,
            memory_chunks=memory_chunks,
            clarification=clarification,
            request_id=request_id,
            effective_intent=effective_intent,
        )
        return self._dispatch_mutation(
            state,
            mutation,
            effective_intent=effective_intent,
            normalized_message=decision.normalized_message,
            execute_db=execute_db,
            request_id=request_id,
            started=started,
            interpretation_mode=decision.mode.value,
            memory_chunks=memory_chunks,
            decision=decision,
            evidence_businesses=explicit_businesses,
            evidence_geos=explicit_geos,
        )

    def _process_contextual(
        self,
        state: ContextContractV2,
        *,
        message: str,
        execute_db: bool,
        clarification: ClarificationAnswer | None,
        request_id: str,
        turn_id: str,
        started: float,
    ) -> TurnProcessResult:
        """Interpret every active-session turn against the seven-turn ledger."""
        period_patch = (
            _deterministic_period_patch(state, message, turn_id)
            if clarification is None and state.pending_clarification is None
            else None
        )
        if period_patch is not None:
            log_event(
                LOGGER,
                logging.INFO,
                "deterministic_period_patch_recognized",
                request_id=request_id,
                mutation_mode="period_patch",
            )
            return self._dispatch_mutation(
                state,
                period_patch,
                normalized_message=period_patch.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode="deterministic_period_patch",
                memory_chunks=[],
            )
        geo_patch = (
            self._deterministic_geo_patch(state, message, turn_id)
            if clarification is None and state.pending_clarification is None
            else None
        )
        if geo_patch is not None:
            mutation, candidate = geo_patch
            log_event(
                LOGGER,
                logging.INFO,
                "deterministic_geo_patch_recognized",
                request_id=request_id,
                mutation_mode="geo_patch",
                geo_id=str(candidate.geo.geo_id),
            )
            return self._dispatch_mutation(
                state,
                mutation,
                normalized_message=mutation.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode="deterministic_geo_patch",
                memory_chunks=[],
                evidence_geos=[candidate.geo],
            )
        direction_comparison = self._deterministic_direction_comparison_mutation(
            message, turn_id
        )
        if direction_comparison is not None:
            return self._dispatch_mutation(
                state,
                direction_comparison,
                normalized_message=direction_comparison.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode="deterministic_direction_comparison",
                memory_chunks=[],
                evidence_businesses=self._tagged_business_balances(message),
            )
        directed_flow_balance = self._deterministic_analyzed_flow_mutation(
            message, turn_id
        )
        if directed_flow_balance is not None:
            return self._dispatch_mutation(
                state,
                directed_flow_balance,
                normalized_message=directed_flow_balance.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode="deterministic_analyzed_directed_flow",
                memory_chunks=[],
            )
        if self._is_explicit_flow_balance_query(message):
            return self._standalone(
                state,
                message=message,
                execute_db=execute_db,
                request_id=request_id,
                turn_id=turn_id,
                started=started,
                interpretation_mode="deterministic_flow_balance_bridge",
            )
        explicit_geos = self._tagged_geo_objects(message)
        explicit_businesses = self._tagged_business_balances(message)
        business_mentions = self._tagged_business_entity_mentions(message)
        balance_section = self._deterministic_balance_section_mutation(
            message,
            explicit_businesses,
            turn_id,
        )
        if balance_section is not None:
            return self._dispatch_mutation(
                state,
                balance_section,
                normalized_message=balance_section.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode="deterministic_balance_section",
                memory_chunks=[],
                evidence_businesses=explicit_businesses,
            )
        full_balance = self._deterministic_full_balance_mutation(
            message,
            explicit_businesses,
            turn_id,
        )
        if full_balance is not None:
            return self._dispatch_mutation(
                state,
                full_balance,
                normalized_message=full_balance.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode="deterministic_full_balance",
                memory_chunks=[],
                evidence_businesses=explicit_businesses,
            )
        directed_flow = self._deterministic_directed_flow_mutation(
            message,
            explicit_businesses,
            explicit_geos,
            turn_id,
        )
        if directed_flow is not None:
            return self._dispatch_mutation(
                state,
                directed_flow,
                normalized_message=directed_flow.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode="deterministic_directed_flow",
                memory_chunks=[],
                evidence_businesses=explicit_businesses,
                evidence_geos=explicit_geos,
            )
        routing = self.gate.route(
            message,
            state,
            explicit_businesses=explicit_businesses,
            explicit_geos=explicit_geos,
            clarification_answer=clarification is not None,
        )
        log_event(
            LOGGER,
            logging.INFO,
            "routing_evidence_gate",
            request_id=request_id,
            route="interpret",
            reason=routing.reason,
            explicit_business_count=routing.explicit_business_count,
            explicit_geo_count=routing.explicit_geo_count,
        )
        memory_chunks = []
        if self.result_memory and state.metadata and state.result_references:
            memory_chunks = self.result_memory.retrieve(
                session_id=state.session_id,
                query=message,
                metadata_bundle_version=state.metadata.bundle_version,
                intent=state.active_dialog_scope.intent,
            )
        try:
            decision = self.interpreter.interpret(
                message=message,
                state=state,
                capabilities=_capabilities(),
                domain_hints=[
                    *(
                        f"explicit_business:{item.canonical_name}"
                        for item in explicit_businesses
                    ),
                    *(f"explicit_geo:{item.canonical_name}" for item in explicit_geos),
                    *_domain_hints(self.registry),
                ],
                metadata_bundle_version=self.registry.manifest.bundle_version,
                current_message_tags=[
                    *[
                        {
                            "tag": "BUSINESS_ENTITY",
                            "text": item.text,
                            "canonical_name": item.text,
                            "role_hint": item.role,
                        }
                        for item in business_mentions
                    ],
                    *[
                        {
                            "tag": "GEO",
                            "text": item.canonical_name,
                            "canonical_name": item.canonical_name,
                            "role_hint": "destination",
                        }
                        for item in explicit_geos
                    ],
                ],
                result_references=(
                    self.result_memory.for_interpretation(memory_chunks)
                    if self.result_memory else []
                ),
                clarification_answer=(
                    clarification.model_dump(mode="json")
                    if clarification is not None else None
                ),
                request_id=request_id,
            )
        except InterpretationError as exc:
            raise TurnProcessingError(
                "interpretation contract validation failed",
                code="interpretation_contract_invalid",
            ) from exc
        if decision.mode == InterpretationMode.CLARIFY:
            if decision.clarification is None:
                raise TurnProcessingError("clarification contract is missing")
            return TurnProcessResult(
                mutation=ContextMutation(
                    turn_id=turn_id,
                    user_message=message,
                    normalized_message=decision.normalized_message,
                    replace_intent=state.active_dialog_scope.intent,
                ),
                outcome=TransitionOutcome.CLARIFICATION,
                clarification_questions=[decision.clarification.model_dump(mode="json")],
                diagnostics=_diagnostics(decision, memory_chunks, 0, started),
            )
        if decision.mode == InterpretationMode.UNSUPPORTED:
            raise TurnProcessingError(
                f"unsupported capability: {decision.unsupported_capability}"
            )
        try:
            mutation = self.compiler.compile(
                decision,
                state,
                turn_id=turn_id,
                user_message=message,
                current_entity_mentions=[
                    *business_mentions,
                    *[
                        EntityMention(
                            text=item.canonical_name, role="destination"
                        )
                        for item in explicit_geos
                    ],
                ],
            )
        except CanonicalRelationNotFound as exc:
            base = state.active_dialog_scope.intent
            attempted = _single_operand_attempt(base, exc.operand)
            return self._reverse_no_data(
                state,
                ContextMutation(
                    turn_id=turn_id,
                    user_message=message,
                    normalized_message=decision.normalized_message,
                    replace_intent=attempted,
                ),
                request_id=request_id,
                started=started,
                interpretation_mode="conversation_graph",
                interpretation_source="qwen",
            )
        except ContextBindingError as exc:
            log_event(
                LOGGER,
                logging.WARNING,
                "context_graph_binding_failed",
                request_id=request_id,
                error=str(exc),
                normalized_message=decision.normalized_message,
            )
            raise TurnProcessingError(
                "interpretation could not be bound to canonical metadata",
                code="interpretation_binding_failed",
            ) from exc
        effective_intent = self._materialize_mutation(state, mutation)
        decision, mutation, effective_intent = self._repair_incomplete_evidence(
            state,
            message=message,
            turn_id=turn_id,
            decision=decision,
            mutation=mutation,
            explicit_businesses=explicit_businesses,
            current_business_mentions=business_mentions,
            explicit_geos=explicit_geos,
            memory_chunks=memory_chunks,
            clarification=clarification,
            request_id=request_id,
            effective_intent=effective_intent,
        )
        return self._dispatch_mutation(
            state,
            mutation,
            effective_intent=effective_intent,
            normalized_message=decision.normalized_message,
            execute_db=execute_db,
            request_id=request_id,
            started=started,
            interpretation_mode="conversation_graph",
            memory_chunks=memory_chunks,
            decision=decision,
            evidence_businesses=explicit_businesses,
            evidence_geos=explicit_geos,
        )

    def _repair_incomplete_evidence(
        self,
        state,
        *,
        message,
        turn_id,
        decision,
        mutation,
        explicit_businesses,
        current_business_mentions,
        explicit_geos,
        memory_chunks,
        clarification,
        request_id,
        effective_intent,
    ):
        initial_issues: list[str]
        try:
            self.gate.validate_bound_intent(
                effective_intent,
                explicit_businesses=explicit_businesses,
                explicit_geos=explicit_geos,
            )
            return decision, mutation, effective_intent
        except EvidenceCompletenessError as initial_error:
            initial_issues = list(initial_error.issues)
            log_event(
                LOGGER,
                logging.INFO,
                "routing_evidence_repair_started",
                request_id=request_id,
                issues=initial_issues,
                pre_database=True,
            )
        try:
            repaired = self.interpreter.interpret(
                message=message,
                state=state,
                capabilities=_capabilities(),
                domain_hints=[
                    *(
                        f"explicit_business:{item.canonical_name}"
                        for item in explicit_businesses
                    ),
                    *(f"explicit_geo:{item.canonical_name}" for item in explicit_geos),
                    *_domain_hints(self.registry),
                ],
                metadata_bundle_version=self.registry.manifest.bundle_version,
                current_message_tags=[
                    *[
                        {
                            "tag": "BUSINESS_ENTITY",
                            "text": item.text,
                            "canonical_name": item.text,
                            "role_hint": item.role,
                        }
                        for item in current_business_mentions
                    ],
                    *[
                        {
                            "tag": "GEO",
                            "text": item.canonical_name,
                            "canonical_name": item.canonical_name,
                            "role_hint": "destination",
                        }
                        for item in explicit_geos
                    ],
                ],
                validation_feedback=initial_issues,
                result_references=(
                    self.result_memory.for_interpretation(memory_chunks)
                    if self.result_memory else []
                ),
                clarification_answer=(
                    clarification.model_dump(mode="json")
                    if clarification is not None else None
                ),
                request_id=f"{request_id}:evidence_repair",
            )
            if repaired.mode not in {
                InterpretationMode.STANDALONE,
                InterpretationMode.MUTATION,
            }:
                raise EvidenceCompletenessError(["repair_not_executable"])
            repaired_mutation = self.compiler.compile(
                repaired,
                state,
                turn_id=turn_id,
                user_message=message,
                current_entity_mentions=[
                    *current_business_mentions,
                    *[
                        EntityMention(text=item.canonical_name, role="destination")
                        for item in explicit_geos
                    ],
                ],
            )
            repaired_intent = self._materialize_mutation(state, repaired_mutation)
            self.gate.validate_bound_intent(
                repaired_intent,
                explicit_businesses=explicit_businesses,
                explicit_geos=explicit_geos,
            )
        except (InterpretationError, ContextBindingError, EvidenceCompletenessError) as exc:
            issues = list(exc.issues) if isinstance(exc, EvidenceCompletenessError) else [type(exc).__name__]
            log_event(
                LOGGER,
                logging.WARNING,
                "routing_evidence_repair_rejected",
                request_id=request_id,
                issues=issues,
                pre_database=True,
            )
            raise TurnProcessingError(
                "canonical routing evidence is incomplete",
                code="routing_evidence_incomplete",
            ) from exc
        log_event(
            LOGGER,
            logging.INFO,
            "routing_evidence_repair_accepted",
            request_id=request_id,
            operation=repaired_intent.operation.value,
            pre_database=True,
        )
        return repaired, repaired_mutation, repaired_intent

    def _execute_grouping_mutation(
        self,
        state,
        mutation,
        *,
        effective_intent,
        normalized_message,
        execute_db,
        request_id,
        started,
        interpretation_mode,
        decision=None,
        memory_chunks=(),
        evidence_businesses=(),
        evidence_geos=(),
    ):
        intent = effective_intent
        normalized_intent = _normalize_temporal_grouping_intent(
            intent, normalized_message
        )
        normalization_changed = normalized_intent != intent
        mutation = self._synchronize_effective_intent(
            state,
            mutation,
            previous_effective_intent=intent,
            normalized_effective_intent=normalized_intent,
        )
        intent = normalized_intent
        if normalization_changed:
            log_event(
                LOGGER,
                logging.INFO,
                "temporal_grouping_normalized",
                request_id=request_id,
                grain=intent.grain,
                aggregate_type=intent.grouping[0].aggregate_type,
                evidence="explicit_query_grain",
            )
        if (
            len(intent.grouping) == 1
            and intent.grouping[0].dimension == "period"
        ):
            return self._execute_mutation(
                state,
                mutation,
                effective_intent=intent,
                normalized_message=normalized_message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode=interpretation_mode,
                memory_chunks=memory_chunks,
                decision=decision,
                evidence_businesses=evidence_businesses,
                evidence_geos=evidence_geos,
            )
        try:
            self.gate.validate_bound_intent(
                intent,
                explicit_businesses=evidence_businesses,
                explicit_geos=evidence_geos,
            )
        except EvidenceCompletenessError as exc:
            log_event(
                LOGGER,
                logging.WARNING,
                "routing_evidence_gate_rejected",
                request_id=request_id,
                phase="grouping_intent",
                issues=list(exc.issues),
                operation=intent.operation.value,
                pre_database=True,
            )
            raise TurnProcessingError(
                "canonical routing evidence is incomplete",
                code="routing_evidence_incomplete",
            ) from exc
        log_event(
            LOGGER,
            logging.INFO,
            "routing_evidence_gate_accepted",
            request_id=request_id,
            phase="grouping_intent",
            operation=intent.operation.value,
            operand_count=len(intent.operands),
            task_count=1,
            explicit_business_count=len(evidence_businesses),
            explicit_geo_count=len(evidence_geos),
            pre_database=True,
        )
        if len(intent.operands) != 1 or len(intent.grouping) != 1:
            raise TurnProcessingError(
                "grouping requires one operand and one canonical dimension",
                code="native_grouping_failed",
            )
        source_reference = next(
            (
                item
                for item in reversed(state.result_references)
                if state.active_dialog_scope is not None
                and item.turn_id == state.active_dialog_scope.turn_id
                and item.status == TransitionOutcome.SUCCESS
                and item.facts
            ),
            None,
        )
        if source_reference is not None:
            envelope = {
                "status": "ok",
                "rows": source_reference.facts,
                "unit": intent.operands[0].unit,
                "warnings": [],
            }
            grouping_source = "result_reference"
        else:
            envelope = self.runtime.execute_raw(
                normalized_message,
                execute_db=execute_db,
                request_id=request_id,
            )
            grouping_source = "pipeline"
        canonical_unit = _canonical_grouping_unit(
            intent,
            normalized_message,
            envelope.get("rows") or [],
        )
        if canonical_unit and intent.operands[0].unit != canonical_unit:
            previous_intent = intent
            operand = intent.operands[0].model_copy(
                update={"unit": canonical_unit}, deep=True
            )
            intent = intent.model_copy(update={"operands": [operand]}, deep=True)
            mutation = self._synchronize_effective_intent(
                state,
                mutation,
                previous_effective_intent=previous_intent,
                normalized_effective_intent=intent,
            )
        status = str(envelope.get("status") or "error")
        outcome = _outcome(status)
        grouped = []
        if outcome == TransitionOutcome.SUCCESS:
            try:
                interpretation = (
                    envelope.get("interpretation")
                    if isinstance(envelope.get("interpretation"), dict)
                    else {}
                )
                members = member_facts_from_rows(
                    envelope.get("rows") or [],
                    dimension=intent.grouping[0].dimension,
                    default_unit=(
                        canonical_unit
                        or envelope.get("unit")
                        or interpretation.get("unit")
                        or intent.operands[0].unit
                    ),
                    authoritative_unit=canonical_unit or intent.operands[0].unit,
                    canonical_resolver=self._resolve_group_row_entity,
                )
                grouped = CanonicalGroupAggregator().aggregate(members)
            except (GroupingError, ValueError) as exc:
                raise TurnProcessingError(
                    "canonical grouping result is invalid",
                    code="native_grouping_failed",
                ) from exc
        facts = [_safe_mapping(item.model_dump(mode="json")) for item in grouped]
        result_ref = (
            _grouped_result_reference(mutation.turn_id, intent, facts)
            if outcome == TransitionOutcome.SUCCESS else None
        )
        return TurnProcessResult(
            mutation=mutation,
            outcome=outcome,
            response={
                "operation": Operation.GROUP.value,
                "status": status,
                "facts": facts,
                "warnings": envelope.get("warnings") or [],
            },
            result_reference=result_ref,
            memory_write=(
                _memory_write(
                    mutation.turn_id,
                    normalized_message,
                    intent,
                    result_ref,
                    facts,
                    {"title": "Сгруппированный результат"},
                )
                if result_ref is not None and execute_db and state.metadata else None
            ),
            diagnostics={
                "interpretation": {
                    "mode": interpretation_mode,
                    "source": "qwen" if decision is not None else "deterministic",
                    "confidence": decision.confidence if decision is not None else 1.0,
                },
                "execution": {
                    "task_count": 1,
                    "group_count": len(grouped),
                    "grouping_source": grouping_source,
                    "elapsed_ms": int((perf_counter() - started) * 1000),
                },
            },
        )

    def _resolve_group_row_entity(
        self, row: dict[str, Any], dimension: str
    ) -> CanonicalEntityRef | None:
        if dimension not in {"geo", "geo_group"} or self._normalize_lemmas is None:
            return None
        labels = [
            str(row.get(key) or "").strip()
            for key in ("geo", "article_scope", "article_name")
        ]
        matches: dict[str, Any] = {}
        for label in labels:
            if not label:
                continue
            normalized = self._normalize_lemmas(label)
            for geo in self.registry.geo_objects:
                if any(
                    self._normalize_lemmas(candidate) == normalized
                    for candidate in (geo.canonical_name, *geo.aliases)
                ):
                    matches[str(geo.geo_id)] = geo
            if len(matches) == 1:
                geo = next(iter(matches.values()))
                return CanonicalEntityRef(
                    entity_id=str(geo.geo_id),
                    entity_type="geo_object",
                    display_name=_official_name(geo.canonical_name),
                )
            if len(matches) > 1:
                return None
        return None

    def _deterministic_geo_mutation(self, state, message: str, turn_id: str):
        scope = state.active_dialog_scope
        if scope is None or len(scope.intent.operands) != 1 or self._normalize_lemmas is None:
            return None
        matches = self._matched_geo_objects(message)
        if len(matches) != 1:
            return None
        geo = matches[0]
        operand = scope.intent.operands[0]
        retained = [
            item
            for item in operand.entities
            if item.role not in {"balance", "article", "source", "destination", "route"}
        ]
        retained.append(
            OperandEntityRef(
                role="destination",
                entity=CanonicalEntityRef(
                    entity_id=str(geo.geo_id),
                    entity_type="geo_object",
                    display_name=_official_name(geo.canonical_name),
                ),
            )
        )
        intent = scope.intent.model_copy(
            update={
                "operands": [operand.model_copy(update={"entities": retained}, deep=True)],
                "comparison": None,
            },
            deep=True,
        )
        return ContextMutation(
            turn_id=turn_id,
            user_message=message,
            normalized_message=message,
            replace_intent=intent,
        )

    def _deterministic_reverse_mutation(self, state, message: str, turn_id: str):
        """Resolve a conversational reverse only from canonical active metadata."""
        if _REVERSE_DIRECTION.search(str(message)) is None:
            return None
        scope = state.active_dialog_scope
        if scope is None or len(scope.intent.operands) != 1:
            return None
        operand = scope.intent.operands[0]
        by_role = {item.role: item for item in operand.entities}
        balance_ref = by_role.get("balance")
        article_ref = by_role.get("article")
        if balance_ref is None or article_ref is None:
            source = by_role.get("source")
            destination = by_role.get("destination")
            if source is None or destination is None:
                return None
            reversed_operand = InterpretationMutationCompiler._reverse_operand(operand)
            intent = scope.intent.model_copy(
                update={
                    "operands": [reversed_operand],
                    "comparison": None,
                },
                deep=True,
            )
            return (
                ContextMutation(
                    turn_id=turn_id,
                    user_message=message,
                    normalized_message=message,
                    replace_intent=intent,
                ),
                True,
            )

        source_balance = self.registry.balance(
            _metadata_numeric_id(balance_ref.entity.entity_id)
        )
        current_article = self.registry.article(
            _metadata_numeric_id(article_ref.entity.entity_id)
        )
        if source_balance is None or current_article is None:
            return None
        destination_balance = next(
            (
                self.registry.balance(value)
                for value in (
                    current_article.canonical_name,
                    *current_article.aliases,
                    article_ref.entity.display_name,
                )
                if self.registry.balance(value) is not None
            ),
            None,
        )
        reverse_articles: dict[int, Any] = {}
        if destination_balance is not None:
            for value in (source_balance.canonical_name, *source_balance.aliases):
                for candidate in self.registry.find_article_candidates(
                    value,
                    balance_id=destination_balance.balance_id,
                ):
                    if (
                        str(candidate.section).strip().casefold() == "распределение"
                        and any(
                            _normalize_text(part) == "за пределы"
                            for part in candidate.path
                        )
                    ):
                        reverse_articles[candidate.article_id] = candidate
        reverse_article = (
            next(iter(reverse_articles.values()))
            if len(reverse_articles) == 1
            else None
        )
        relation_found = destination_balance is not None and reverse_article is not None
        reversed_entities = (
            _balance_article_entities(destination_balance, reverse_article)
            if relation_found
            else list(operand.entities)
        )
        intent = scope.intent.model_copy(
            update={
                "operands": [
                    operand.model_copy(update={"entities": reversed_entities}, deep=True)
                ],
                "comparison": None,
            },
            deep=True,
        )
        normalized = (
            f"Обратное направление: {destination_balance.canonical_name} — "
            f"{reverse_article.canonical_name}"
            if relation_found
            else message
        )
        return (
            ContextMutation(
                turn_id=turn_id,
                user_message=message,
                normalized_message=normalized,
                replace_intent=intent,
            ),
            relation_found,
        )

    def _reverse_no_data(
        self,
        state,
        mutation,
        *,
        request_id,
        started,
        interpretation_mode="deterministic_reverse",
        interpretation_source="deterministic",
    ):
        effective_intent = self._materialize_mutation(state, mutation)
        log_event(
            LOGGER,
            logging.INFO,
            "reverse_relation_not_found",
            request_id=request_id,
            pre_database=True,
        )
        return TurnProcessResult(
            mutation=mutation,
            outcome=TransitionOutcome.NO_DATA,
            response={
                "operation": effective_intent.operation.value,
                "status": "no_data",
                "facts": [],
                "warnings": [
                    {
                        "code": "reverse_relation_not_found",
                        "message": "Для обратного направления нет однозначной связи в metadata.",
                    }
                ],
            },
            diagnostics={
                "interpretation": {
                    "mode": interpretation_mode,
                    "source": interpretation_source,
                    "confidence": 1.0,
                },
                "execution": {
                    "status": "no_data",
                    "task_count": 0,
                    "layer": "pre_database_metadata_gate",
                    "elapsed_ms": int((perf_counter() - started) * 1000),
                },
            },
        )

    def _matched_geo_objects(self, message: str) -> list[Any]:
        if self._normalize_lemmas is None:
            return []
        query = self._normalize_lemmas(message)
        matches: dict[str, tuple[int, int, Any]] = {}
        for geo in self.registry.geo_objects:
            for label in (geo.canonical_name, *geo.aliases):
                normalized = self._normalize_lemmas(label)
                if not normalized:
                    continue
                match = re.search(rf"(?<!\w){re.escape(normalized)}(?!\w)", query)
                if match is None:
                    continue
                candidate = (match.start(), -len(normalized), geo)
                current = matches.get(str(geo.geo_id))
                if current is None or candidate[:2] < current[:2]:
                    matches[str(geo.geo_id)] = candidate
        return [item[2] for item in sorted(matches.values(), key=lambda item: item[:2])]

    def _tagged_geo_objects(self, message: str) -> list[Any]:
        """Extract ordered current-turn GEO tags, including safe inflections."""
        if self._normalize_lemmas is None:
            return self._matched_geo_objects(message)
        query_tokens = self._normalize_lemmas(message).split()
        business_spans = _business_entity_token_spans(query_tokens)
        candidates: list[tuple[int, int, float, Any]] = []
        for geo in self.registry.geo_objects:
            best: tuple[int, int, float, Any] | None = None
            for label in (geo.canonical_name, *geo.aliases):
                label_tokens = self._normalize_lemmas(label).split()
                if not label_tokens or any(len(item) < 3 for item in label_tokens):
                    continue
                width = len(label_tokens)
                for start in range(0, len(query_tokens) - width + 1):
                    end = start + width
                    if any(start < right and end > left for left, right in business_spans):
                        continue
                    window = query_tokens[start:start + width]
                    scores = [
                        SequenceMatcher(None, left, right).ratio()
                        for left, right in zip(window, label_tokens)
                    ]
                    threshold = 0.84 if width == 1 else 0.76
                    if min(scores) < threshold:
                        continue
                    candidate = (start, end, sum(scores) / width, geo)
                    if best is None or candidate[2] > best[2]:
                        best = candidate
            if best is not None:
                candidates.append(best)
        by_span: dict[tuple[int, int], list[tuple[int, int, float, Any]]] = {}
        for item in candidates:
            by_span.setdefault((item[0], item[1]), []).append(item)
        selected: list[tuple[int, int, float, Any]] = []
        for values in by_span.values():
            ranked = sorted(values, key=lambda item: item[2], reverse=True)
            if len(ranked) > 1 and ranked[0][2] - ranked[1][2] < 0.03:
                continue
            selected.append(ranked[0])
        selected.sort(key=lambda item: (item[0], -item[2]))
        output: list[Any] = []
        occupied: set[int] = set()
        for start, end, _score, geo in selected:
            span = set(range(start, end))
            if span & occupied:
                continue
            occupied.update(span)
            output.append(geo)
        return output

    def _tagged_business_balances(self, message: str) -> list[Any]:
        """First pass: bind qualified `ГП ТГ` / `ТГ` spans as balances."""
        if self._normalize_lemmas is None:
            return []
        tokens = self._normalize_lemmas(message).split()
        matches: list[tuple[int, Any]] = []
        for start, end in _business_entity_token_spans(tokens):
            name = " ".join(tokens[start:end]).strip()
            if not name:
                continue
            record = next(
                (
                    candidate
                    for value in (f"гп тг {name}", f"тг {name}", name)
                    if (candidate := self.registry.balance(value)) is not None
                ),
                None,
            )
            if record is not None:
                matches.append((start, record))
        output: list[Any] = []
        seen: set[str] = set()
        for _start, record in sorted(matches, key=lambda item: item[0]):
            key = str(record.balance_id)
            if key not in seen:
                seen.add(key)
                output.append(record)
        return output

    def _detect_geo_followup(
        self,
        message: str,
        active_intent: AnalysisIntent,
    ) -> GeoPatchCandidate | None:
        """Resolve one conservative GEO replacement from canonical metadata."""
        if (
            active_intent.operation not in {Operation.SHOW, Operation.AGGREGATE}
            or len(active_intent.operands) != 1
            or active_intent.grouping
            or active_intent.comparison is not None
            or active_intent.formula is not None
            or active_intent.ranking is not None
            or self._normalize_lemmas is None
        ):
            return None
        operand = active_intent.operands[0]
        if operand.metric not in {"distribution", "export"}:
            return None
        followup = re.fullmatch(
            r"(?:а\s+)?(?:по|для)\s+(?P<geo>.+)",
            _normalize_text(message),
        )
        if followup is None:
            return None
        mention = followup.group("geo")
        balance_lookup = getattr(self.registry, "balance", None)
        if callable(balance_lookup) and balance_lookup(mention) is not None:
            return None
        matches = self._tagged_geo_objects(mention)
        if (
            len(matches) != 1
            or not _whole_geo_mention_matches(
                mention,
                matches[0],
                self._normalize_lemmas,
            )
        ):
            return None
        geo = matches[0]
        destinations = [
            (index, item)
            for index, item in enumerate(operand.entities)
            if item.role == "destination"
            and item.entity.entity_type == "geo_object"
        ]
        roles = {item.role for item in operand.entities}
        if len(destinations) != 1 or roles & {"source", "route"}:
            return None
        article_indexes = [
            index
            for index, item in enumerate(operand.entities)
            if item.role == "article"
        ]
        entities = [item.model_copy(deep=True) for item in operand.entities]
        destination_index, _destination = destinations[0]
        entities[destination_index] = _typed_entity(
            "destination", "geo_object", geo
        )
        if article_indexes:
            balances = [
                item
                for item in operand.entities
                if item.role == "balance" and item.entity.entity_type == "balance"
            ]
            if (
                operand.metric != "distribution"
                or len(article_indexes) != 1
                or len(balances) != 1
                or not callable(balance_lookup)
            ):
                return None
            balance = balance_lookup(
                _metadata_numeric_id(balances[0].entity.entity_id)
            )
            article = (
                _unique_direction_article(
                    self.registry,
                    balance=balance,
                    target=geo,
                    metric="distribution",
                )
                if balance is not None
                else None
            )
            if article is None:
                return None
            entities[article_indexes[0]] = _typed_entity(
                "article", "article", article
            )
        updated_operand = operand.model_copy(
            update={"entities": entities},
            deep=True,
        )
        return GeoPatchCandidate(geo=geo, operands=(updated_operand,))

    def _deterministic_geo_patch(
        self,
        state: ContextContractV2,
        message: str,
        turn_id: str,
    ) -> tuple[ContextMutation, GeoPatchCandidate] | None:
        scope = state.active_dialog_scope
        if scope is None:
            return None
        candidate = self._detect_geo_followup(message, scope.intent)
        if candidate is None:
            return None
        mutation = ContextMutation(
            turn_id=turn_id,
            user_message=message,
            normalized_message=message,
            patch=IntentPatch(
                operands=FieldMutation(
                    action=MutationAction.SET,
                    value=list(candidate.operands),
                )
            ),
        )
        return mutation, candidate

    def _is_explicit_flow_balance_query(self, message: str) -> bool:
        """Recognize a complete V1 flow-balance contract before scalar binding.

        ``flow_balance`` resolves two physical articles (incoming and
        distribution) from one explicit business viewpoint.  It therefore
        cannot pass through the scalar ContextResolver binder.  The existing
        unified analyzer remains authoritative for detecting the capability;
        only complete, unambiguous source/destination and period evidence is
        admitted to the explicit compatibility bridge.
        """

        if self._analyze_query is None:
            return False
        try:
            analyzed = self._analyze_query(message)
        except Exception:
            return False
        if str(getattr(analyzed, "metric", "")).casefold() != "flow_balance":
            return False
        if bool(getattr(analyzed, "needs_clarification", False)):
            return False
        source = str(getattr(analyzed, "from_node", "") or "").strip()
        destination = str(getattr(analyzed, "to_node", "") or "").strip()
        if not source or not destination or source.casefold() == destination.casefold():
            return False
        periods = list(getattr(analyzed, "periods", None) or [])
        has_period = bool(periods) or bool(
            getattr(analyzed, "date_from", None)
            and getattr(analyzed, "date_to", None)
        )
        return has_period

    def _deterministic_analyzed_flow_mutation(
        self,
        message: str,
        turn_id: str,
    ) -> ContextMutation | None:
        """Bind an explicitly directed flow resolved by the unified analyzer.

        The analyzer historically names both an explicit directed transfer and
        a two-sided "баланс потоков" as ``flow_balance``.  An arrow or an
        ``из A в B`` construction is nevertheless one directed distribution
        from the source viewpoint.  A phrase "между A и B" remains the
        two-sided flow-balance compatibility contract.
        """

        normalized = message.casefold()
        explicit_direction = bool(
            re.search(r"(?:→|->)", message)
            or re.search(r"\bиз\b.+\b(?:в|во)\b", normalized)
        )
        if not explicit_direction or re.search(r"\bмежду\b", normalized):
            return None
        if self._analyze_query is None:
            return None
        try:
            analyzed = self._analyze_query(message)
        except Exception:
            return None
        if (
            str(getattr(analyzed, "metric", "")).casefold() != "flow_balance"
            or bool(getattr(analyzed, "needs_clarification", False))
        ):
            return None
        source = self.registry.balance(getattr(analyzed, "from_node", None))
        destination = self.registry.balance(getattr(analyzed, "to_node", None))
        if source is None or destination is None:
            return None
        article = _unique_direction_article(
            self.registry,
            balance=source,
            target=destination,
            metric="distribution",
        )
        if article is None:
            return None
        periods = [
            PeriodRef(date_from=item.date_from, date_to=item.date_to)
            for item in (getattr(analyzed, "periods", None) or [])
        ]
        if not periods:
            date_from = getattr(analyzed, "date_from", None)
            date_to = getattr(analyzed, "date_to", None)
            if not date_from or not date_to:
                return None
            periods = [PeriodRef(date_from=date_from, date_to=date_to)]
        operation = (
            Operation.COMPARE_PERIODS
            if len(periods) == 2
            and str(getattr(analyzed, "intent", "")).casefold() == "compare"
            else Operation.SHOW
        )
        return ContextMutation(
            turn_id=turn_id,
            user_message=message,
            normalized_message=message,
            replace_intent=AnalysisIntent(
                operation=operation,
                operands=[AnalysisOperand(
                    operand_id="directed_flow",
                    metric="distribution",
                    aggregate_type=str(
                        getattr(analyzed, "aggregate_type", None) or "sum"
                    ),
                    unit=CANONICAL_VOLUME_UNIT,
                    entities=[
                        _typed_entity("balance", "balance", source),
                        _typed_entity("destination", "balance", destination),
                        _typed_entity("article", "article", article),
                    ],
                )],
                periods=periods,
            ),
        )

    def _deterministic_direction_comparison_mutation(
        self,
        message: str,
        turn_id: str,
    ) -> ContextMutation | None:
        """Bind two explicit business directions to two canonical operands.

        A comparison may legitimately reuse the same two business objects in
        opposite roles.  The global entity set therefore cannot represent the
        query: every ordered ``source -> destination`` pair is resolved and
        validated independently against the source balance metadata.
        """

        if not re.search(r"\bсравн\w*", _normalize_text(message)):
            return None
        mentions = self._tagged_business_entity_mentions(message)
        pairs: list[tuple[Any, Any]] = []
        pending_source: Any | None = None
        for mention in mentions:
            record = self.registry.balance(mention.text)
            if record is None:
                return None
            if mention.role == "source":
                if pending_source is not None:
                    return None
                pending_source = record
            elif mention.role == "destination" and pending_source is not None:
                if str(record.balance_id) == str(pending_source.balance_id):
                    return None
                pairs.append((pending_source, record))
                pending_source = None
        if pending_source is not None or len(pairs) != 2 or len(set(
            (str(source.balance_id), str(destination.balance_id))
            for source, destination in pairs
        )) != 2:
            return None

        periods = self._analyzed_periods(message)
        if len(periods) != 1:
            return None
        operands: list[AnalysisOperand] = []
        for index, (source, destination) in enumerate(pairs, start=1):
            article = _unique_direction_article(
                self.registry,
                balance=source,
                target=destination,
                metric="distribution",
            )
            if article is None:
                return None
            operands.append(AnalysisOperand(
                operand_id=f"direction_{index}",
                metric="distribution",
                aggregate_type="sum",
                unit=CANONICAL_VOLUME_UNIT,
                entities=[
                    _typed_entity("balance", "balance", source),
                    _typed_entity("destination", "balance", destination),
                    _typed_entity("article", "article", article),
                ],
            ))
        return ContextMutation(
            turn_id=turn_id,
            user_message=message,
            normalized_message=message,
            replace_intent=AnalysisIntent(
                operation=Operation.COMPARE,
                operands=operands,
                periods=periods,
                comparison=ComparisonSpec(
                    baseline_operand_id=operands[0].operand_id,
                    target_operand_id=operands[1].operand_id,
                ),
            ),
        )

    def _analyzed_periods(self, message: str) -> list[PeriodRef]:
        """Return exact periods produced by the existing deterministic analyzer."""

        if self._analyze_query is None:
            return []
        try:
            analyzed = self._analyze_query(message)
        except Exception:
            return []
        periods = [
            PeriodRef(date_from=item.date_from, date_to=item.date_to)
            for item in (getattr(analyzed, "periods", None) or [])
        ]
        if periods:
            return periods
        date_from = getattr(analyzed, "date_from", None)
        date_to = getattr(analyzed, "date_to", None)
        if not date_from or not date_to:
            return []
        return [PeriodRef(date_from=date_from, date_to=date_to)]

    def _deterministic_full_balance_mutation(
        self,
        message: str,
        balances: Sequence[Any],
        turn_id: str,
    ) -> ContextMutation | None:
        """Bind an explicit full-balance snapshot without a second LLM pass."""

        period = _explicit_single_day_period(message)
        if len(balances) != 1 or period is None or self._analyze_query is None:
            return None
        try:
            analyzed = self._analyze_query(message)
        except Exception:
            return None
        if (
            str(getattr(analyzed, "intent", "")).casefold() != "show"
            or str(getattr(analyzed, "metric", "")).casefold() != "balance"
            or str(getattr(analyzed, "article_policy", "") or "").casefold()
            not in {"", "balance_only"}
            or getattr(analyzed, "article_text", None)
        ):
            return None
        balance = balances[0]
        intent = AnalysisIntent(
            operation=Operation.SHOW,
            operands=[AnalysisOperand(
                operand_id="balance_snapshot",
                metric="balance",
                aggregate_type="sum",
                unit="тыс. м3",
                entities=[OperandEntityRef(
                    role="balance",
                    entity=CanonicalEntityRef(
                        entity_id=f"BAL:{balance.balance_id}",
                        entity_type="balance",
                        display_name=balance.canonical_name,
                    ),
                )],
            )],
            periods=[period],
        )
        return ContextMutation(
            turn_id=turn_id,
            user_message=message,
            normalized_message=message,
            replace_intent=intent,
        )

    def _deterministic_balance_section_mutation(
        self,
        message: str,
        balances: Sequence[Any],
        turn_id: str,
    ) -> ContextMutation | None:
        """Bind an explicit metadata section and exact day without an LLM pass."""

        period = _explicit_single_day_period(message)
        if len(balances) != 1 or period is None or self._analyze_query is None:
            return None
        try:
            analyzed = self._analyze_query(message)
        except Exception:
            return None
        if str(getattr(analyzed, "intent", "")).casefold() != "show":
            return None
        balance = balances[0]
        section = _explicit_section_article(self.registry, balance, message)
        if section is None:
            return None
        intent = AnalysisIntent(
            operation=Operation.SHOW,
            operands=[AnalysisOperand(
                operand_id="section_snapshot",
                metric="balance_section",
                aggregate_type="sum",
                entities=[
                    OperandEntityRef(
                        role="balance",
                        entity=CanonicalEntityRef(
                            entity_id=f"BAL:{balance.balance_id}",
                            entity_type="balance",
                            display_name=balance.canonical_name,
                        ),
                    ),
                    OperandEntityRef(
                        role="article",
                        entity=CanonicalEntityRef(
                            entity_id=f"ART:{section.article_id}",
                            entity_type="article",
                            display_name=section.canonical_name,
                        ),
                    ),
                ],
            )],
            periods=[period],
        )
        return ContextMutation(
            turn_id=turn_id,
            user_message=message,
            normalized_message=message,
            replace_intent=intent,
        )

    def _deterministic_directed_flow_mutation(
        self,
        message: str,
        balances: Sequence[Any],
        geos: Sequence[Any],
        turn_id: str,
    ) -> ContextMutation | None:
        """Bind an exact-day directed flow from typed metadata evidence.

        The execution viewpoint is always explicit: incoming is read on the
        destination balance, while distribution is read on the source balance.
        The direction-bound article must exist uniquely in that balance.  No
        relation or article is inferred from name similarity alone.
        """

        period = _explicit_single_day_period(message)
        if period is None or self._analyze_query is None:
            return None
        try:
            analyzed = self._analyze_query(message)
        except Exception:
            return None
        if str(getattr(analyzed, "intent", "")).casefold() != "show":
            return None
        metric = str(getattr(analyzed, "metric", "")).casefold()
        if metric not in {"incoming", "distribution"}:
            return None

        mentions = self._tagged_business_entity_mentions(message)
        business_by_role: dict[str, Any] = {}
        for mention in mentions:
            record = self.registry.balance(mention.text)
            if record is not None and mention.role not in business_by_role:
                business_by_role[mention.role] = record

        balance: Any | None = None
        article: Any | None = None
        directional: OperandEntityRef | None = None
        if metric == "incoming" and not geos:
            source = business_by_role.get("source")
            destination = business_by_role.get("destination")
            if source is None or destination is None or len(balances) != 2:
                return None
            balance = destination
            article = _unique_direction_article(
                self.registry,
                balance=balance,
                target=source,
                metric=metric,
            )
            directional = _typed_entity("source", "balance", source)
        elif metric == "distribution" and not geos:
            source = business_by_role.get("source") or business_by_role.get("balance")
            destination = business_by_role.get("destination")
            if source is None or destination is None or len(balances) != 2:
                return None
            balance = source
            article = _unique_direction_article(
                self.registry,
                balance=balance,
                target=destination,
                metric=metric,
            )
            directional = _typed_entity("destination", "balance", destination)
        elif metric == "distribution" and len(balances) == 1 and len(geos) == 1:
            balance = balances[0]
            geo = geos[0]
            article = _unique_direction_article(
                self.registry,
                balance=balance,
                target=geo,
                metric=metric,
            )
            directional = _typed_entity("destination", "geo_object", geo)
        if balance is None or article is None or directional is None:
            return None

        intent = AnalysisIntent(
            operation=Operation.SHOW,
            operands=[AnalysisOperand(
                operand_id="directed_flow",
                metric=metric,
                aggregate_type="sum",
                unit=CANONICAL_VOLUME_UNIT,
                entities=[
                    _typed_entity("balance", "balance", balance),
                    directional,
                    _typed_entity("article", "article", article),
                ],
            )],
            periods=[period],
        )
        return ContextMutation(
            turn_id=turn_id,
            user_message=message,
            normalized_message=message,
            replace_intent=intent,
        )

    def _tagged_business_entity_mentions(self, message: str) -> list[EntityMention]:
        """Preserve every qualified business span and its local routing role."""
        if self._normalize_lemmas is None:
            return []
        tokens = self._normalize_lemmas(message).split()
        resolved: list[tuple[int, int, Any]] = []
        for start, end in _business_entity_token_spans(tokens):
            name = " ".join(tokens[start:end]).strip()
            if not name:
                continue
            record = next(
                (
                    candidate
                    for value in (f"гп тг {name}", f"тг {name}", name)
                    if (candidate := self.registry.balance(value)) is not None
                ),
                None,
            )
            if record is None:
                continue
            resolved.append((start, end, record))
        output: list[EntityMention] = []
        unique_balances = {str(record.balance_id) for _start, _end, record in resolved}
        for start, _end, record in resolved:
            name = " ".join(tokens[start + 1 if tokens[start] == "тг" else start:_end]).strip()
            if not name:
                name = record.canonical_name
            # A single qualified business namespace is a balance scope even
            # after a grammatical preposition ("в ГП ТГ Москва"). Directional
            # roles become authoritative only when distinct business balances
            # participate in the same phrase.
            if len(unique_balances) == 1:
                role = "balance"
                output.append(EntityMention(text=record.canonical_name, role=role))
                continue
            qualifier = start - 1
            previous = tokens[qualifier - 1] if qualifier > 0 else ""
            if previous == "гп" and qualifier > 1:
                previous = tokens[qualifier - 2]
            if previous in {"от", "из", "иза"}:
                role = "source"
            elif previous in {"в", "во", "к", "до"}:
                role = "destination"
            else:
                role = "balance"
            output.append(EntityMention(text=f"ТГ {name}", role=role))
        return output

    def _enforce_role_separated_entities(
        self,
        mutation: ContextMutation,
        *,
        balance: Any,
        geos: list[Any],
    ) -> ContextMutation:
        """Make deterministic first/second-pass roles authoritative for one route."""
        intent = mutation.replace_intent
        if intent is None or len(intent.operands) != 1 or len(geos) != 1:
            raise TurnProcessingError(
                "role-separated entity interpretation is not scalar",
                code="interpretation_binding_failed",
            )
        entities = self.compiler.binder.bind([
            EntityMention(text=balance.canonical_name, role="balance"),
            EntityMention(text=geos[0].canonical_name, role="destination"),
        ])
        operand = intent.operands[0].model_copy(
            update={"entities": entities}, deep=True
        )
        canonical_intent = intent.model_copy(
            update={"operands": [operand]}, deep=True
        )
        log_event(
            LOGGER,
            logging.INFO,
            "role_separated_entities_bound",
            balance_id=str(balance.balance_id),
            geo_ids=[str(item.geo_id) for item in geos],
            roles=[item.role for item in entities],
        )
        return mutation.model_copy(
            update={"replace_intent": canonical_intent}, deep=True
        )

    def _role_separated_standalone(
        self,
        state: ContextContractV2,
        *,
        message: str,
        balance: Any,
        geos: list[Any],
        execute_db: bool,
        request_id: str,
        turn_id: str,
        started: float,
    ) -> TurnProcessResult:
        """Resolve non-entity semantics, then apply authoritative typed roles."""
        semantic_envelope = self.runtime.execute_raw(
            message,
            execute_db=False,
            request_id=f"{request_id}:semantics",
        )
        try:
            intent = self.translator.intent(semantic_envelope, canonical_geos=[])
        except Exception as exc:
            raise _standalone_translation_error(exc) from exc
        if len(intent.operands) != 1 or intent.grouping:
            raise TurnProcessingError(
                "role-separated entity semantics are not scalar",
                code="resolved_plan_translation_failed",
            )
        mutation = ContextMutation(
            turn_id=turn_id,
            user_message=message,
            normalized_message=message,
            replace_intent=intent,
        )
        mutation = self._enforce_role_separated_entities(
            mutation,
            balance=balance,
            geos=geos,
        )
        effective_intent = self._materialize_mutation(state, mutation)
        log_event(
            LOGGER,
            logging.INFO,
            "role_separated_standalone_resolved",
            request_id=request_id,
            balance_id=str(balance.balance_id),
            geo_ids=[str(item.geo_id) for item in geos],
            operation=effective_intent.operation.value,
            periods=[
                item.model_dump(mode="json")
                for item in effective_intent.periods
            ],
        )
        return self._dispatch_mutation(
            state,
            mutation,
            effective_intent=effective_intent,
            normalized_message=message,
            execute_db=execute_db,
            request_id=request_id,
            started=started,
            interpretation_mode="deterministic_role_separated",
            memory_chunks=[],
        )

    def _canonical_entity_comparison(
        self,
        state,
        *,
        message: str,
        operand_entities: list[list[OperandEntityRef]],
        operand_metrics: tuple[str, ...] | None = None,
        execute_db: bool,
        request_id: str,
        turn_id: str,
        started: float,
        interpretation_mode: str,
    ) -> TurnProcessResult:
        semantic_envelope = self.runtime.execute_raw(
            message,
            execute_db=False,
            request_id=f"{request_id}:semantics",
        )
        try:
            intent = self.translator.peer_entity_intent(
                semantic_envelope,
                operand_entities,
            )
            if operand_metrics is not None:
                if len(operand_metrics) != len(intent.operands):
                    raise ValueError("operand metric evidence does not match operands")
                intent = intent.model_copy(
                    update={
                        "operands": [
                            operand.model_copy(update={"metric": metric}, deep=True)
                            for operand, metric in zip(intent.operands, operand_metrics)
                        ]
                    },
                    deep=True,
                )
        except Exception as exc:
            raise TurnProcessingError(
                "explicit peer entity semantics could not be resolved",
                code="explicit_operand_binding_failed",
            ) from exc
        mutation = ContextMutation(
            turn_id=turn_id,
            user_message=message,
            normalized_message=message,
            replace_intent=intent,
        )
        return self._dispatch_mutation(
            state,
            mutation,
            normalized_message=message,
            execute_db=execute_db,
            request_id=request_id,
            started=started,
            interpretation_mode=interpretation_mode,
            memory_chunks=[],
        )

    def _matched_peer_entities(self, message: str) -> list[list[OperandEntityRef]]:
        mentions = _peer_destination_mentions(message)
        if mentions is None:
            return []
        output: list[list[OperandEntityRef]] = []
        for mention in mentions:
            articles = [
                item
                for item in self.registry.find_article_candidates(mention)
                if str(item.section).strip().lower() == "распределение"
            ]
            if len(articles) == 1:
                article = articles[0]
                balance = self.registry.balance(article.balance_id)
                if balance is None:
                    return []
                output.append(
                    [
                        OperandEntityRef(
                            role="balance",
                            entity=CanonicalEntityRef(
                                entity_id=str(balance.balance_id),
                                entity_type="balance",
                                display_name=balance.canonical_name,
                            ),
                        ),
                        OperandEntityRef(
                            role="article",
                            entity=CanonicalEntityRef(
                                entity_id=str(article.article_id),
                                entity_type="article",
                                display_name=article.canonical_name,
                            ),
                        ),
                    ]
                )
                continue
            geo = self._exact_geo_object(mention)
            if geo is not None:
                output.append(
                    [
                        OperandEntityRef(
                            role="destination",
                            entity=CanonicalEntityRef(
                                entity_id=str(geo.geo_id),
                                entity_type="geo_object",
                                display_name=_official_name(geo.canonical_name),
                            ),
                        )
                    ]
                )
                continue
            return []
        return output

    def _exact_geo_object(self, mention: str):
        if self._normalize_lemmas is None:
            return None
        normalized_mention = self._normalize_lemmas(mention)
        matches = [
            geo
            for geo in self.registry.geo_objects
            if normalized_mention
            and any(
                self._normalize_lemmas(label) == normalized_mention
                for label in (geo.canonical_name, *geo.aliases)
            )
        ]
        return matches[0] if len(matches) == 1 else None

    def _matched_distribution_own_consumers(
        self, message: str
    ) -> list[list[OperandEntityRef]]:
        mentions = _distribution_own_consumers_mentions(message)
        if mentions is None:
            return []
        destination_name, balance_name = mentions
        articles = [
            item
            for item in self.registry.find_article_candidates(destination_name)
            if str(item.section).strip().casefold() == "распределение"
        ]
        balance = self.registry.balance(balance_name)
        if len(articles) != 1 or balance is None:
            return []
        distribution_article = articles[0]
        distribution_balance = self.registry.balance(distribution_article.balance_id)
        own_articles = [
            item
            for item in self.registry.articles_for_balance(balance.balance_id)
            if _normalize_text(item.canonical_name) == "собственные потребители"
            and str(item.section).strip().casefold() == "распределение"
        ]
        if distribution_balance is None or len(own_articles) != 1:
            return []
        own_article = own_articles[0]
        return [
            _balance_article_entities(distribution_balance, distribution_article),
            _balance_article_entities(balance, own_article),
        ]

    def _execute_mutation(
        self,
        state,
        mutation,
        *,
        effective_intent,
        normalized_message,
        execute_db,
        request_id,
        started,
        interpretation_mode,
        memory_chunks,
        decision=None,
        evidence_businesses=(),
        evidence_geos=(),
    ):
        intent = effective_intent
        normalized_comparison = _normalize_same_scope_period_comparison_intent(intent)
        comparison_normalization_changed = normalized_comparison != intent
        mutation = self._synchronize_effective_intent(
            state,
            mutation,
            previous_effective_intent=intent,
            normalized_effective_intent=normalized_comparison,
        )
        intent = normalized_comparison
        if comparison_normalization_changed:
            log_event(
                LOGGER,
                logging.INFO,
                "same_scope_period_comparison_normalized",
                request_id=request_id,
                operation=intent.operation.value,
                period_count=len(intent.periods),
                operand_count=len(intent.operands),
            )
        normalized_intent = _normalize_series_reduction_intent(
            state, intent, normalized_message
        )
        series_normalization_changed = normalized_intent != intent
        mutation = self._synchronize_effective_intent(
            state,
            mutation,
            previous_effective_intent=intent,
            normalized_effective_intent=normalized_intent,
        )
        intent = normalized_intent
        if series_normalization_changed:
            log_event(
                LOGGER,
                logging.INFO,
                "series_reduction_normalized",
                request_id=request_id,
                operation=intent.operation.value,
                grain=intent.grain,
                aggregate_type=intent.operands[0].aggregate_type,
                bucket_aggregate=metric_definition(
                    intent.operands[0].metric
                ).default_aggregate,
            )
        try:
            self.gate.validate_bound_intent(
                intent,
                explicit_businesses=evidence_businesses,
                explicit_geos=evidence_geos,
            )
        except EvidenceCompletenessError as exc:
            log_event(
                LOGGER,
                logging.WARNING,
                "routing_evidence_gate_rejected",
                request_id=request_id,
                phase="bound_intent",
                issues=list(exc.issues),
                operation=intent.operation.value,
                pre_database=True,
            )
            raise TurnProcessingError(
                "canonical routing evidence is incomplete",
                code="routing_evidence_incomplete",
            ) from exc
        try:
            plan = self.planner.plan(intent)
        except PlanningError as exc:
            raise TurnProcessingError(
                "native intent planning failed", code="native_planning_failed"
            ) from exc
        try:
            self.gate.validate_plan(plan)
        except EvidenceCompletenessError as exc:
            log_event(
                LOGGER,
                logging.WARNING,
                "routing_evidence_gate_rejected",
                request_id=request_id,
                phase="execution_plan",
                issues=list(exc.issues),
                operation=intent.operation.value,
                pre_database=True,
            )
            raise TurnProcessingError(
                "canonical execution evidence is incomplete",
                code="routing_evidence_incomplete",
            ) from exc
        log_event(
            LOGGER,
            logging.INFO,
            "routing_evidence_gate_accepted",
            request_id=request_id,
            phase="execution_plan",
            operation=intent.operation.value,
            operand_count=len(intent.operands),
            task_count=len(plan.tasks),
            explicit_business_count=len(evidence_businesses),
            explicit_geo_count=len(evidence_geos),
            pre_database=True,
        )
        if (
            _is_full_balance_show(intent)
            or _is_balance_section_show(intent)
            or _is_directed_flow_show(intent)
        ):
            return self._execute_full_balance_mutation(
                state,
                mutation,
                intent=intent,
                normalized_message=normalized_message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode=interpretation_mode,
                memory_chunks=memory_chunks,
                decision=decision,
            )
        try:
            native = self.executor.execute(
                plan,
                original_query=normalized_message,
                execute_db=execute_db,
                request_id=request_id,
            )
        except Exception as exc:
            raise TurnProcessingError("native scalar execution failed") from exc
        outcome = _outcome(native.status)
        response = _public_native_result(native)
        deterministic_summary = (
            _derived_contract_summary(native, intent)
            or _ranking_contract_summary(native, intent)
            or _series_reduction_contract_summary(native, plan, intent)
            or _extremum_comparison_summary(
                native,
                intent,
                upstream_summary=None,
            )
            or _period_comparison_contract_summary(native, intent)
            or _comparison_contract_summary(native, intent)
        )
        if deterministic_summary is not None:
            response["summary"] = deterministic_summary
        summary_diagnostics: dict[str, Any] = {
            "requested": False,
            "execution_layer": "native_deterministic",
        }
        summarize = getattr(self.runtime, "summarize_envelope", None)
        if (
            _should_summarize(interpretation_mode)
            and outcome == TransitionOutcome.SUCCESS
            and execute_db
            and callable(summarize)
        ):
            summary_diagnostics["requested"] = True
            try:
                summary_envelope = summarize(
                    _native_summary_envelope(native, plan, normalized_message, intent),
                    request_id=f"{request_id}:summary",
                )
                public_summary = _public_summary(summary_envelope.get("summary"))
                contract_summary = _extremum_comparison_summary(
                    native,
                    intent,
                    upstream_summary=public_summary,
                )
                contract_summary = deterministic_summary or contract_summary
                response["summary"] = contract_summary or public_summary
                response["warnings"] = summary_envelope.get("warnings") or []
                summary_diagnostics.update(_summary_diagnostics(summary_envelope))
                if contract_summary is not None:
                    summary_diagnostics.update(
                        generated_by=contract_summary["generated_by"],
                        upstream_generated_by=(
                            public_summary.get("generated_by")
                            if isinstance(public_summary, dict)
                            else None
                        ),
                    )
            except Exception as exc:
                summary_diagnostics.update(
                    {"status": "error", "error_type": type(exc).__name__}
                )
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "native_summary_failed",
                    request_id=request_id,
                    operation=native.operation.value,
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
        result_ref = None
        memory_diag: dict[str, Any] = self.result_memory.safe_diagnostics(memory_chunks) if self.result_memory else {}
        if outcome == TransitionOutcome.SUCCESS:
            result_ref = _result_reference(mutation.turn_id, native, intent)
        memory_write = (
            _memory_write(
                mutation.turn_id,
                normalized_message,
                intent,
                result_ref,
                result_ref.facts,
                _native_memory_summary(native),
            )
            if result_ref is not None and self.result_memory and execute_db and state.metadata
            else None
        )
        diagnostics = (
            _diagnostics(decision, memory_chunks, len(plan.tasks), started)
            if decision is not None
            else {
                "interpretation": {
                    "mode": interpretation_mode,
                    "source": "deterministic",
                    "confidence": 1.0,
                },
                "execution": {
                    "task_count": len(plan.tasks),
                    "layer": "native_deterministic",
                    "elapsed_ms": int((perf_counter() - started) * 1000),
                },
            }
        )
        diagnostics.setdefault("execution", {})["layer"] = "native_deterministic"
        diagnostics["execution"].update(
            calculation=(
                native.derived.operator
                if native.derived is not None
                else f"rank:{native.ranking.direction}:{native.ranking.grain}"
                if native.ranking is not None
                else None
            ),
            calculation_node_count=len(plan.tasks)
            + int(native.comparison is not None)
            + int(native.comparison_set is not None and len(native.comparison_set.members) > 2)
            + int(native.derived is not None)
            + int(native.ranking is not None),
            source_execution_count=native.source_execution_count,
            tasks=[
                _native_task_evidence(task, result)
                for task, result in zip(plan.tasks, native.task_results)
            ],
        )
        diagnostics["result_memory"] = memory_diag
        diagnostics["summary"] = summary_diagnostics
        return TurnProcessResult(
            mutation=mutation,
            outcome=outcome,
            response=response,
            result_reference=result_ref,
            memory_write=memory_write,
            diagnostics=diagnostics,
        )

    def _execute_full_balance_mutation(
        self,
        state,
        mutation,
        *,
        intent,
        normalized_message,
        execute_db,
        request_id,
        started,
        interpretation_mode,
        memory_chunks,
        decision=None,
    ) -> TurnProcessResult:
        """Preserve full-balance or metadata-section hierarchical rows."""
        execute = getattr(self.runtime, "execute", None)
        execute_balance_day = getattr(self.runtime, "execute_balance_day", None)
        if not callable(execute) and not callable(execute_balance_day):
            raise TurnProcessingError(
                "balance-level execution is unavailable",
                code="balance_level_execution_unavailable",
            )
        section = _balance_section_article(intent, self.registry)
        directed_article = _directed_flow_article(intent, self.registry)
        source_intent = (
            _full_balance_source_intent(intent)
            if section is not None or directed_article is not None
            else intent
        )
        execution_layer = (
            "unified_balance_section"
            if section is not None
            else "unified_directed_flow"
            if directed_article is not None
            else "unified_balance_level"
        )
        try:
            if section is not None or directed_article is not None:
                if not callable(execute_balance_day):
                    raise TurnProcessingError(
                        "canonical balance-day execution is unavailable",
                        code=(
                            "directed_flow_execution_unavailable"
                            if directed_article is not None
                            else "balance_level_execution_unavailable"
                        ),
                    )
                balance_ref = source_intent.operands[0].entities[0].entity
                period = source_intent.periods[0]
                envelope = execute_balance_day(
                    balance_id=_metadata_numeric_id(balance_ref.entity_id),
                    day=period.date_from.isoformat(),
                    request_id=f"{request_id}:balance",
                ) if execute_db else {
                    "status": "ok",
                    "rows": [],
                    "warnings": [],
                    "debug": {
                        "sql_function": "api.show_balance_day",
                        "params": {
                            "balance_id": _metadata_numeric_id(balance_ref.entity_id),
                            "day": period.date_from.isoformat(),
                        },
                    },
                }
            else:
                if not callable(execute):
                    raise TurnProcessingError(
                        "balance-level execution is unavailable",
                        code="balance_level_execution_unavailable",
                    )
                envelope = execute(
                    normalized_message,
                    source_intent,
                    execute_db=execute_db,
                    request_id=f"{request_id}:balance",
                    apply_summary=False,
                )
        except Exception as exc:
            if isinstance(exc, TurnProcessingError):
                raise
            raise TurnProcessingError(
                "balance-level execution failed",
                code="balance_level_execution_failed",
            ) from exc
        if section is not None:
            envelope = _filter_balance_section_envelope(
                envelope,
                section,
                self.registry,
            )
        if directed_article is not None:
            envelope = _filter_directed_flow_envelope(envelope, directed_article)
        envelope = _normalize_full_balance_envelope(envelope, source_intent)
        status = str(envelope.get("status") or "error")
        outcome = _outcome(status)
        summary_diagnostics: dict[str, Any] = {
            "requested": False,
            "execution_layer": execution_layer,
        }
        summarize = getattr(self.runtime, "summarize_envelope", None)
        if (
            _should_summarize(interpretation_mode)
            and outcome == TransitionOutcome.SUCCESS
            and execute_db
            and callable(summarize)
        ):
            summary_diagnostics["requested"] = True
            try:
                envelope = summarize(
                    envelope,
                    request_id=f"{request_id}:summary",
                )
                summary_diagnostics.update(_summary_diagnostics(envelope))
            except Exception as exc:
                summary_diagnostics.update(
                    {"status": "error", "error_type": type(exc).__name__}
                )
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "balance_level_summary_failed",
                    request_id=request_id,
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
        facts = _standalone_facts(envelope) if outcome == TransitionOutcome.SUCCESS else []
        result_ref = (
            _grouped_result_reference(mutation.turn_id, intent, facts)
            if outcome == TransitionOutcome.SUCCESS
            else None
        )
        memory_diag: dict[str, Any] = (
            self.result_memory.safe_diagnostics(memory_chunks)
            if self.result_memory else {}
        )
        summary = envelope.get("summary") if isinstance(envelope.get("summary"), dict) else {}
        memory_write = (
            _memory_write(
                mutation.turn_id,
                normalized_message,
                intent,
                result_ref,
                facts,
                summary,
            )
            if result_ref is not None and self.result_memory and execute_db and state.metadata
            else None
        )
        _, technical_warnings = _partition_public_warnings(
            envelope.get("warnings") or []
        )
        if technical_warnings:
            log_event(
                LOGGER,
                logging.INFO,
                "technical_result_warnings_suppressed",
                request_id=request_id,
                warnings=technical_warnings,
            )
        diagnostics = (
            _diagnostics(decision, memory_chunks, 1, started)
            if decision is not None
            else {
                "interpretation": {
                    "mode": interpretation_mode,
                    "source": "deterministic",
                    "confidence": 1.0,
                },
                "execution": {},
            }
        )
        diagnostics.setdefault("execution", {}).update(
            status=status,
            task_count=1,
            layer=execution_layer,
            source_execution_count=1,
            row_count=len(envelope.get("rows") or []),
            elapsed_ms=int((perf_counter() - started) * 1000),
            **_safe_execution_evidence(envelope),
        )
        diagnostics["result_memory"] = memory_diag
        diagnostics["summary"] = summary_diagnostics
        return TurnProcessResult(
            mutation=mutation,
            outcome=outcome,
            response=_public_pipeline_result(envelope),
            result_reference=result_ref,
            memory_write=memory_write,
            diagnostics=diagnostics,
        )

    def _standalone(
        self,
        state,
        *,
        message,
        execute_db,
        request_id,
        turn_id,
        started,
        user_message=None,
        interpretation_mode="standalone",
    ):
        envelope = self.runtime.execute_raw(
            message,
            execute_db=execute_db,
            request_id=request_id,
        )
        status = str(envelope.get("status") or "error")
        summary_diagnostics: dict[str, Any] = {
            "requested": False,
            "execution_layer": "unified_strict",
        }
        summarize = getattr(self.runtime, "summarize_envelope", None)
        if (
            _should_summarize(interpretation_mode)
            and status in {"ok", "partial"}
            and execute_db
            and callable(summarize)
        ):
            summary_diagnostics["requested"] = True
            try:
                envelope = summarize(
                    envelope,
                    request_id=f"{request_id}:summary",
                )
                summary_diagnostics.update(_summary_diagnostics(envelope))
            except Exception as exc:
                summary_diagnostics.update(
                    {"status": "error", "error_type": type(exc).__name__}
                )
                log_event(
                    LOGGER,
                    logging.WARNING,
                    "standalone_summary_failed",
                    request_id=request_id,
                    error_type=type(exc).__name__,
                    exc_info=True,
                )
        try:
            intent = self.translator.intent(
                envelope,
                canonical_geos=self._matched_geo_objects(user_message or message),
            )
        except Exception as exc:
            raise _standalone_translation_error(exc) from exc
        mutation = ContextMutation(
            turn_id=turn_id,
            user_message=user_message or message,
            normalized_message=message,
            replace_intent=intent,
        )
        outcome = _outcome(status)
        result_ref = None
        memory_write = None
        if outcome == TransitionOutcome.SUCCESS:
            facts = _standalone_facts(envelope)
            result_ref = _grouped_result_reference(turn_id, intent, facts)
            if self.result_memory and execute_db and state.metadata:
                summary = (
                    envelope.get("summary")
                    if isinstance(envelope.get("summary"), dict) else {}
                )
                memory_write = _memory_write(
                    turn_id,
                    message,
                    intent,
                    result_ref,
                    facts,
                    summary,
                )
        public_response = _public_pipeline_result(envelope)
        _, technical_warnings = _partition_public_warnings(
            envelope.get("warnings") or []
        )
        if technical_warnings:
            log_event(
                LOGGER,
                logging.INFO,
                "technical_result_warnings_suppressed",
                request_id=request_id,
                warnings=technical_warnings,
            )
        return TurnProcessResult(
            mutation=mutation,
            outcome=outcome,
            response=public_response,
            result_reference=result_ref,
            memory_write=memory_write,
            diagnostics={
                "interpretation": {
                    "mode": interpretation_mode,
                    "source": "pipeline",
                },
                "execution": {
                    "status": status,
                    "task_count": 1,
                    "layer": "unified_strict",
                    "elapsed_ms": int((perf_counter() - started) * 1000),
                    **_safe_execution_evidence(envelope),
                },
                "summary": summary_diagnostics,
            },
        )


def metadata_ref(registry: Any) -> MetadataVersionRef:
    manifest = registry.manifest
    return MetadataVersionRef(
        bundle_id=manifest.bundle_id,
        bundle_version=manifest.bundle_version,
        schema_version=manifest.schema_version,
    )


def _validate_clarification_answer(
    state: ContextContractV2,
    answer: ClarificationAnswer,
) -> None:
    pending = state.pending_clarification
    if pending is None or pending.turn_id != answer.source_turn_id:
        raise TurnProcessingError(
            "clarification answer does not match pending turn",
            code="clarification_stale",
        )
    question = next(
        (
            item for item in pending.questions
            if item.clarification_id == answer.clarification_id
        ),
        None,
    )
    if question is None or answer.selected_option not in question.options:
        raise TurnProcessingError(
            "clarification answer is not one of the offered options",
            code="clarification_invalid",
        )


def _capabilities() -> list[str]:
    return interpretation_capabilities()


def _domain_hints(registry: Any) -> list[str]:
    return [
        *(f"geo_group:{item.canonical_name}" for item in registry.geo_groups[:8]),
        *(f"route:{item.canonical_name}" for item in registry.routes[:8]),
    ]


def _outcome(status: str) -> TransitionOutcome:
    if status in {"ok", "partial"}:
        return TransitionOutcome.SUCCESS
    if status == "no_data":
        return TransitionOutcome.NO_DATA
    return TransitionOutcome.ERROR


def _standalone_translation_error(exc: Exception) -> TurnProcessingError:
    reason = str(exc)
    if reason == "resolved_comparison_degraded":
        return TurnProcessingError(reason, code="resolved_comparison_degraded")
    if reason == "resolved plan has no canonical period":
        return TurnProcessingError("period not detected", code="period_required")
    if reason.startswith("unsupported resolved operation:"):
        return TurnProcessingError(reason, code="resolved_operation_unsupported")
    if "grouping" in reason:
        return TurnProcessingError(reason, code="resolved_grouping_unsupported")
    return TurnProcessingError(
        "standalone resolved plan translation failed",
        code="resolved_plan_translation_failed",
    )


def _result_reference(turn_id, native, intent) -> ResultReference:
    facts = _authoritative_native_facts(native)
    digest = hashlib.sha256(
        json.dumps(
            {"intent": intent.model_dump(mode="json"), "facts": facts},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ResultReference(
        turn_id=turn_id,
        status=TransitionOutcome.SUCCESS,
        resolved_plan_hash=f"sha256:{digest}",
        row_count=len(facts),
        facts=facts,
    )


def _grouped_result_reference(turn_id, intent, facts) -> ResultReference:
    digest = hashlib.sha256(
        json.dumps(
            {"intent": intent.model_dump(mode="json"), "facts": facts},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return ResultReference(
        turn_id=turn_id,
        status=TransitionOutcome.SUCCESS,
        resolved_plan_hash=f"sha256:{digest}",
        row_count=len(facts),
        facts=facts,
    )


def _memory_write(turn_id, query, intent, result_ref, facts, summary):
    return ResultMemoryWrite(
        turn_id=turn_id,
        query=query,
        intent=intent,
        result_id=result_ref.result_id,
        facts=facts,
        summary=summary or {},
    )


def _native_memory_summary(native) -> dict[str, Any]:
    if native.derived is not None:
        derived = native.derived
        return {
            "title": "Сохранённый производный показатель",
            "text": f"{derived.operator}={derived.value} {derived.unit}",
        }
    if native.ranking is not None:
        selected = native.ranking.selected[0]
        return {
            "title": "Сохранённый экстремум временного ряда",
            "text": (
                f"{native.ranking.direction}={selected.value} {selected.unit}; "
                f"grain={native.ranking.grain}"
            ),
        }
    if native.comparison is not None:
        comparison = native.comparison
        return {
            "title": "Сохранённое сравнение",
            "text": (
                f"baseline={comparison.baseline_value} {comparison.unit}; "
                f"target={comparison.target_value} {comparison.unit}; "
                f"delta={comparison.delta} {comparison.unit}"
            ),
        }
    return {"title": "Сохранённый deterministic результат"}


def _standalone_facts(envelope: dict[str, Any]) -> list[dict[str, Any]]:
    interpretation = (
        envelope.get("interpretation")
        if isinstance(envelope.get("interpretation"), dict)
        else {}
    )
    del interpretation
    rows = [
        _canonical_volume_row(item)
        for item in (envelope.get("rows") or [])
        if isinstance(item, dict)
    ]
    if rows:
        return rows
    return [{"status": str(envelope.get("status") or "ok")}]


def _public_native_result(native) -> dict[str, Any]:
    facts = [
        _safe_mapping(item.fact.model_dump(mode="json"))
        for item in native.task_results
        if item.fact is not None
    ]
    if native.operation == Operation.GROUP:
        facts.extend(
            _safe_mapping(fact.model_dump(mode="json"))
            for item in native.task_results
            for fact in item.series
        )
    return {
        "operation": native.operation.value,
        "status": native.status,
        "facts": facts,
        "comparison": (
            _public_safe_value(native.comparison.model_dump(mode="json"))
            if native.comparison else None
        ),
        "comparison_set": (
            _public_safe_value(native.comparison_set.model_dump(mode="json"))
            if native.comparison_set and len(native.comparison_set.members) > 2
            else None
        ),
        "derived": (
            _public_safe_value(native.derived.model_dump(mode="json"))
            if native.derived else None
        ),
        "ranking": (
            _public_safe_value(native.ranking.model_dump(mode="json"))
            if native.ranking else None
        ),
    }


def _native_summary_envelope(native, plan, question: str, intent: AnalysisIntent) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    comparison = native.comparison
    for task, result in zip(plan.tasks, native.task_results):
        operand = task.scalar_intent.operands[0]
        task_facts = result.series or ([result.fact] if result.fact is not None else [])
        for fact in task_facts:
            row: dict[str, Any] = {
                "label": fact.label,
                "fact_value": str(fact.value),
                "unit": fact.unit,
                "aggregate_type": operand.aggregate_type,
            }
            if fact.extremum_at is not None:
                row["gas_day"] = fact.extremum_at.isoformat()
            if fact.periods:
                row.update(
                    date_from=fact.periods[0].get("date_from"),
                    date_to=fact.periods[0].get("date_to"),
                )
            if task.period_index is not None:
                row["period_no"] = task.period_index + 1
            if comparison is not None:
                if fact.task_id == comparison.baseline_task_id:
                    row["side"] = "left"
                elif fact.task_id == comparison.target_task_id:
                    row["side"] = "right"
            rows.append(row)
    if native.derived is not None:
        rows.append(
            {
                "label": _formula_label(native.derived.operator),
                "fact_value": str(native.derived.value),
                "unit": native.derived.unit,
                "aggregate_type": native.derived.operator,
                "derived": True,
            }
        )
    metrics = list(dict.fromkeys(item.metric for item in intent.operands))
    return {
        "status": native.status,
        "operation": native.operation.value,
        "question": question,
        "interpretation": {
            "metric": metrics[0] if len(metrics) == 1 else "composite",
            "unit": rows[0].get("unit") if rows else None,
        },
        "periods": [item.model_dump(mode="json") for item in intent.periods],
        "rows": rows,
        "summary": {"title": "Результат готов", "text": ""},
        "warnings": [],
        "debug": {"execution_layer": "native_deterministic"},
    }


def _authoritative_native_facts(native) -> list[dict[str, Any]]:
    facts = [
        _safe_mapping(item.fact.model_dump(mode="json"))
        for item in native.task_results
        if item.fact is not None
    ]
    facts.extend(
        _safe_mapping(fact.model_dump(mode="json"))
        for item in native.task_results
        for fact in item.series
    )
    if native.derived is not None:
        facts.append(
            {
                "kind": "derived",
                "operator": native.derived.operator,
                "value": str(native.derived.value),
                "unit": native.derived.unit,
                "numerator_value": str(native.derived.numerator_value),
                "denominator_value": str(native.derived.denominator_value),
            }
        )
    return facts


def _formula_label(operator: str) -> str:
    return {
        "percent_of": "Доля",
        "percent_change": "Изменение",
        "ratio": "Отношение",
        "delta": "Абсолютное отклонение",
    }.get(operator, operator)


def _derived_contract_summary(native, intent: AnalysisIntent) -> dict[str, Any] | None:
    derived = native.derived
    if derived is None:
        return None
    label = _formula_label(derived.operator)
    value = _format_ru_number(derived.value)
    numerator = _format_ru_number(derived.numerator_value)
    denominator = _format_ru_number(derived.denominator_value)
    by_task = {
        item.task_id: item.fact
        for item in native.task_results
        if item.fact is not None
    }
    numerator_fact = by_task.get(derived.numerator_task_id)
    denominator_fact = by_task.get(derived.denominator_task_id)
    numerator_label = _formula_operand_label(
        intent,
        intent.formula.numerator_operand_id if intent.formula else None,
        numerator_fact.label if numerator_fact is not None else "Числитель",
    )
    denominator_label = _formula_operand_label(
        intent,
        intent.formula.denominator_operand_id if intent.formula else None,
        denominator_fact.label if denominator_fact is not None else "Знаменатель",
    )
    numerator_unit = numerator_fact.unit if numerator_fact is not None else ""
    denominator_unit = denominator_fact.unit if denominator_fact is not None else ""
    return {
        "title": f"{label}: {value} {derived.unit}",
        "text": (
            f"Расчёт выполнен по двум каноническим показателям: "
            f"{numerator_label} — {numerator} {numerator_unit}, "
            f"{denominator_label} — {denominator} {denominator_unit}. "
            f"Результат — {value} {derived.unit}."
        ),
        "bullets": [
            f"Числитель ({numerator_label}): {numerator} {numerator_unit}",
            f"Знаменатель ({denominator_label}): {denominator} {denominator_unit}",
            f"Результат: {value} {derived.unit}",
        ],
        "metrics": {
            "operator": derived.operator,
            "numerator": str(derived.numerator_value),
            "denominator": str(derived.denominator_value),
            "value": str(derived.value),
            "unit": derived.unit,
        },
        "confidence": "high",
        "generated_by": "deterministic_contract",
    }


def _formula_operand_label(
    intent: AnalysisIntent, operand_id: str | None, fallback: str
) -> str:
    operand = next(
        (item for item in intent.operands if item.operand_id == operand_id),
        None,
    )
    if operand is None:
        return fallback
    balance = next(
        (
            item.entity.display_name
            for item in operand.entities
            if item.role == "balance"
        ),
        None,
    )
    if not balance or balance.casefold() in fallback.casefold():
        return fallback
    return f"{fallback}; баланс: {balance}"


def _normalize_temporal_grouping_intent(
    intent: AnalysisIntent, message: str
) -> AnalysisIntent:
    if (
        intent.operation != Operation.GROUP
        or len(intent.grouping) != 1
        or intent.grouping[0].dimension != "period"
    ):
        return intent
    text = str(message or "").casefold().replace("ё", "е")
    patterns = (
        ("day", r"\b(?:по\s+дн(?:ям|ям)|посуточн\w*|ежедневн\w*)\b"),
        ("month", r"\b(?:по\s+месяц(?:ам|ах)|помесячн\w*|ежемесячн\w*)\b"),
        ("quarter", r"\b(?:по\s+квартал(?:ам|ах)|поквартальн\w*)\b"),
        ("year", r"\b(?:по\s+год(?:ам|ах)|ежегодн\w*)\b"),
    )
    explicit_grain = next(
        (candidate for candidate, pattern in patterns if re.search(pattern, text)),
        None,
    )
    grain = explicit_grain or intent.grain
    aggregate_patterns = (
        ("avg", r"\b(?:средн\w*|усредн\w*)\b"),
        ("max", r"\b(?:максим\w*|наибольш\w*)\b"),
        ("min", r"\b(?:миним\w*|наименьш\w*)\b"),
        ("first", r"\b(?:перв\w*|начальн\w*)\b"),
        ("last", r"\b(?:последн\w*|конечн\w*)\b"),
        ("sum", r"\b(?:сумм\w*|итог\w*|общ(?:ий|ая|ее|ие|ую|его|ему|им)?)\b"),
    )
    explicit_aggregate = next(
        (candidate for candidate, pattern in aggregate_patterns if re.search(pattern, text)),
        None,
    )
    aggregate_type = explicit_aggregate or metric_definition(
        intent.operands[0].metric
    ).default_aggregate
    grouping = intent.grouping[0]
    if grain == intent.grain and aggregate_type == grouping.aggregate_type:
        return intent
    return intent.model_copy(
        update={
            "grain": grain,
            "grouping": [
                grouping.model_copy(
                    update={"aggregate_type": aggregate_type}, deep=True
                )
            ],
        },
        deep=True,
    )


def _normalize_series_reduction_intent(
    state: ContextContractV2,
    intent: AnalysisIntent,
    message: str,
) -> AnalysisIntent:
    if intent.operation != Operation.AGGREGATE or len(intent.operands) != 1:
        return intent
    operand = intent.operands[0]
    if operand.aggregate_type not in {"sum", "avg", "min", "max", "first", "last"}:
        return intent
    text = str(message or "").casefold().replace("ё", "е")
    explicit_patterns = (
        ("day", r"\b(?:средне(?:суточн|дневн)\w*)\b"),
        ("month", r"\b(?:среднемесячн\w*)\b"),
        ("quarter", r"\b(?:среднеквартальн\w*)\b"),
        ("year", r"\b(?:среднегодов\w*)\b"),
    )
    grain = next(
        (candidate for candidate, pattern in explicit_patterns if re.search(pattern, text)),
        None,
    )
    if grain is None and re.search(r"\bсредн\w*\b", text):
        active = state.active_dialog_scope
        active_result = next(
            (
                item for item in reversed(state.result_references)
                if active is not None
                and item.turn_id == active.turn_id
                and item.status == TransitionOutcome.SUCCESS
                and (item.row_count or 0) > 1
            ),
            None,
        )
        if (
            active is not None
            and active_result is not None
            and active.intent.grain in {"day", "month", "quarter", "year"}
            and len(active.intent.operands) == 1
            and _same_operand_scope(operand, active.intent.operands[0])
        ):
            grain = active.intent.grain
    if grain is None or grain == intent.grain:
        return intent
    return intent.model_copy(update={"grain": grain}, deep=True)


def _same_operand_scope(left: AnalysisOperand, right: AnalysisOperand) -> bool:
    if left.metric != right.metric:
        return False
    left_scope = {
        (item.role, item.entity.entity_type, item.entity.entity_id)
        for item in left.entities
    }
    right_scope = {
        (item.role, item.entity.entity_type, item.entity.entity_id)
        for item in right.entities
    }
    return left_scope == right_scope


def _normalize_same_scope_period_comparison_intent(
    intent: AnalysisIntent,
) -> AnalysisIntent:
    """Canonicalize one scalar target compared over two distinct periods.

    The conversational model may represent references to two historical turns
    as two operands.  When metric, aggregate and canonical entity scope are
    identical and only the exact periods differ, the domain contract is
    unambiguously ``compare_periods``: one operand plus two global periods.
    Comparisons of different entities or different aggregates are untouched.
    """
    if intent.operation != Operation.COMPARE or len(intent.operands) != 2:
        return intent
    first, second = intent.operands
    if (
        first.metric != second.metric
        or first.aggregate_type != second.aggregate_type
        or not _same_operand_scope(first, second)
        or len(first.periods) != 1
        or len(second.periods) != 1
        or first.periods[0] == second.periods[0]
    ):
        return intent
    operand = first.model_copy(update={"periods": []}, deep=True)
    return AnalysisIntent(
        operation=Operation.COMPARE_PERIODS,
        operands=[operand],
        periods=[
            first.periods[0].model_copy(deep=True),
            second.periods[0].model_copy(deep=True),
        ],
        grain=intent.grain,
    )


_RUSSIAN_MONTHS = {
    r"январ\w*": 1,
    r"феврал\w*": 2,
    r"март\w*": 3,
    r"апрел\w*": 4,
    r"ма(?:й|я|е)": 5,
    r"июн\w*": 6,
    r"июл\w*": 7,
    r"август\w*": 8,
    r"сентябр\w*": 9,
    r"октябр\w*": 10,
    r"ноябр\w*": 11,
    r"декабр\w*": 12,
}


def _explicit_single_day_period(message: str) -> PeriodRef | None:
    """Parse one explicit calendar day using bounded generic date forms."""

    text = str(message)
    candidates: list[date] = []
    occupied: list[tuple[int, int]] = []
    for match in re.finditer(r"(?<!\d)(20\d{2})-(\d{1,2})-(\d{1,2})(?!\d)", text):
        try:
            candidates.append(
                date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            )
            occupied.append(match.span())
        except ValueError:
            return None
    for match in re.finditer(
        r"(?<!\d)([0-3]?\d)[./-]([01]?\d)[./-](20\d{2})(?!\d)",
        text,
    ):
        if any(start <= match.start() < end for start, end in occupied):
            continue
        try:
            candidates.append(
                date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
            )
        except ValueError:
            return None
    normalized = _normalize_text(text)
    for month_pattern, month in _RUSSIAN_MONTHS.items():
        match = re.search(
            rf"\b([0-3]?\d)\s+{month_pattern}\s+(20\d{{2}})\b",
            normalized,
        )
        if not match:
            continue
        try:
            candidates.append(date(int(match.group(2)), month, int(match.group(1))))
        except ValueError:
            return None
    unique = list(dict.fromkeys(candidates))
    if len(unique) != 1:
        return None
    start = unique[0]
    return PeriodRef(date_from=start, date_to=start + timedelta(days=1))


_SECTION_MARKER = re.compile(r"\bраздел[а-я]*\b", re.IGNORECASE)


def _explicit_section_article(registry: Any, balance: Any, message: str) -> Any | None:
    """Resolve an explicit ``section <metadata name>`` within one balance."""

    normalized = _normalize_text(message)
    marker = _SECTION_MARKER.search(normalized)
    articles_for_balance = getattr(registry, "articles_for_balance", None)
    if marker is None or not callable(articles_for_balance):
        return None
    suffix = f" {normalized[marker.end():].strip()} "
    articles = tuple(articles_for_balance(balance.balance_id))
    matches: dict[int, tuple[int, Any]] = {}
    for article in articles:
        path = tuple(getattr(article, "path", ()) or ())
        if not path or not any(
            len(tuple(getattr(candidate, "path", ()) or ())) > len(path)
            and tuple(getattr(candidate, "path", ()) or ())[:len(path)] == path
            for candidate in articles
        ):
            continue
        variants = (
            getattr(article, "canonical_name", ""),
            *(getattr(article, "aliases", ()) or ()),
        )
        lengths = [
            len(candidate)
            for value in variants
            if (candidate := _normalize_text(value))
            and f" {candidate} " in suffix
        ]
        if lengths:
            matches[int(article.article_id)] = (max(lengths), article)
    if not matches:
        return None
    longest = max(item[0] for item in matches.values())
    selected = [item[1] for item in matches.values() if item[0] == longest]
    return selected[0] if len(selected) == 1 else None


def _is_balance_section_show(intent: AnalysisIntent) -> bool:
    if (
        intent.operation != Operation.SHOW
        or len(intent.operands) != 1
        or intent.operands[0].metric != "balance_section"
        or intent.grouping
        or intent.comparison is not None
        or intent.formula is not None
        or intent.ranking is not None
    ):
        return False
    return [item.role for item in intent.operands[0].entities] == [
        "balance", "article",
    ]


def _balance_section_article(intent: AnalysisIntent, registry: Any) -> Any | None:
    if not _is_balance_section_show(intent):
        return None
    article_ref = intent.operands[0].entities[1].entity
    article_id = _numeric_metadata_id(article_ref.entity_id)
    lookup = getattr(registry, "article", None)
    if article_id is None or not callable(lookup):
        return None
    return lookup(article_id)


def _typed_entity(role: str, entity_type: str, record: Any) -> OperandEntityRef:
    id_attributes = {
        "balance": ("balance_id", "BAL:"),
        "article": ("article_id", "ART:"),
        "geo_object": ("geo_id", ""),
        "route": ("route_id", ""),
    }
    attribute, prefix = id_attributes[entity_type]
    raw_id = getattr(record, attribute)
    name = getattr(record, "canonical_name")
    return OperandEntityRef(
        role=role,
        entity=CanonicalEntityRef(
            entity_id=f"{prefix}{raw_id}",
            entity_type=entity_type,
            display_name=(
                _official_name(name) if entity_type == "geo_object" else name
            ),
        ),
    )


def _direction_target_labels(target: Any) -> set[str]:
    labels = {
        _normalize_text(value)
        for value in (
            getattr(target, "canonical_name", ""),
            *(getattr(target, "aliases", ()) or ()),
        )
        if _normalize_text(value)
    }
    expanded = set(labels)
    for label in labels:
        compact = re.sub(r"\s+суточный\s+баланс$", "", label).strip()
        expanded.add(compact)
        for prefix in (
            "гп ",
            "ооо ",
            "газпром трансгаз ",
            "ооо газпром трансгаз ",
        ):
            if compact.startswith(prefix):
                expanded.add(compact[len(prefix):].strip())
    return {item for item in expanded if item}


def _unique_direction_article(
    registry: Any,
    *,
    balance: Any,
    target: Any,
    metric: str,
) -> Any | None:
    """Resolve one direction-bound article by exact normalized metadata names."""

    articles_for_balance = getattr(registry, "articles_for_balance", None)
    if not callable(articles_for_balance):
        return None
    target_labels = _direction_target_labels(target)
    matches: dict[int, Any] = {}
    for article in articles_for_balance(balance.balance_id):
        article_label = _normalize_text(article.canonical_name)
        path = {_normalize_text(item) for item in (article.path or ())}
        if metric == "incoming":
            if "поступление" not in path:
                continue
            article_label = re.sub(r"^от\s+", "", article_label).strip()
        elif metric == "distribution":
            if _normalize_text(article.section) != "распределение":
                continue
        else:
            continue
        if article_label in target_labels:
            matches[int(article.article_id)] = article
    return next(iter(matches.values())) if len(matches) == 1 else None


def _is_directed_flow_show(intent: AnalysisIntent) -> bool:
    if (
        intent.operation != Operation.SHOW
        or len(intent.operands) != 1
        or intent.operands[0].metric not in {"incoming", "distribution"}
        or intent.grouping
        or intent.comparison is not None
        or intent.formula is not None
        or intent.ranking is not None
    ):
        return False
    roles = [item.role for item in intent.operands[0].entities]
    return (
        roles in (["balance", "source", "article"], ["balance", "destination", "article"])
        and intent.operands[0].aggregate_type == "sum"
    )


def _directed_flow_article(intent: AnalysisIntent, registry: Any) -> Any | None:
    if not _is_directed_flow_show(intent):
        return None
    article_ref = next(
        item.entity
        for item in intent.operands[0].entities
        if item.role == "article"
    )
    article_id = _numeric_metadata_id(article_ref.entity_id)
    lookup = getattr(registry, "article", None)
    return lookup(article_id) if article_id is not None and callable(lookup) else None


def _full_balance_source_intent(intent: AnalysisIntent) -> AnalysisIntent:
    operand = intent.operands[0]
    balance = next(item for item in operand.entities if item.role == "balance")
    return AnalysisIntent(
        operation=Operation.SHOW,
        operands=[AnalysisOperand(
            operand_id="balance_snapshot",
            metric="balance",
            aggregate_type="sum",
            entities=[balance.model_copy(deep=True)],
        )],
        periods=[
            item.model_copy(deep=True)
            for item in (intent.periods or operand.periods)
        ],
    )


def _filter_balance_section_envelope(
    envelope: dict[str, Any],
    root: Any,
    registry: Any,
) -> dict[str, Any]:
    root_path = tuple(getattr(root, "path", ()) or ())
    articles_for_balance = getattr(registry, "articles_for_balance", None)
    if not root_path or not callable(articles_for_balance):
        return {**envelope, "status": "no_data", "rows": []}
    allowed_ids = {
        int(item.article_id)
        for item in articles_for_balance(root.balance_id)
        if tuple(getattr(item, "path", ()) or ())[:len(root_path)] == root_path
    }
    selected = [
        dict(row)
        for row in (envelope.get("rows") or [])
        if isinstance(row, Mapping)
        and _numeric_metadata_id(row.get("article_id")) in allowed_ids
    ]
    root_row = next(
        (
            row for row in selected
            if _numeric_metadata_id(row.get("article_id")) == int(root.article_id)
        ),
        None,
    )
    if root_row is None:
        return {**envelope, "status": "no_data", "rows": []}
    base_indent = _row_article_indent(root_row)
    normalized_rows = []
    for row in selected:
        name = str(row.get("article_name") or "").strip()
        normalized_rows.append({
            **row,
            "article_name": name,
            "article_scope": str(row.get("article_scope") or name).strip(),
            "article_indent": max(0, _row_article_indent(row) - base_indent),
        })
    return {**envelope, "rows": normalized_rows}


def _filter_directed_flow_envelope(
    envelope: dict[str, Any],
    article: Any,
) -> dict[str, Any]:
    selected = [
        dict(row)
        for row in (envelope.get("rows") or [])
        if isinstance(row, Mapping)
        and _metadata_numeric_id(row.get("article_id")) == int(article.article_id)
    ]
    rows = []
    for row in selected:
        name = str(row.get("article_name") or article.canonical_name).strip()
        rows.append({
            **row,
            "article_name": name,
            "article_scope": str(row.get("article_scope") or name).strip(),
            "article_indent": _row_article_indent(row),
        })
    return {
        **envelope,
        "status": str(envelope.get("status") or "ok") if rows else "no_data",
        "rows": rows,
    }


def _row_article_indent(row: Mapping[str, Any]) -> int:
    raw = row.get("article_indent")
    if raw is not None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    name = str(row.get("article_name") or "")
    return len(name) - len(name.lstrip())


def _numeric_metadata_id(value: Any) -> int | None:
    tail = str(value or "").strip().rsplit(":", 1)[-1]
    return int(tail) if tail.isdigit() else None


def _normalize_full_balance_envelope(
    envelope: dict[str, Any],
    intent: AnalysisIntent,
) -> dict[str, Any]:
    """Attach the canonical daily-balance unit without changing DB facts."""

    if not _is_full_balance_show(intent):
        return envelope
    unit = CANONICAL_VOLUME_UNIT
    normalized = dict(envelope)
    normalized["unit"] = unit
    normalized["rows"] = [
        _canonical_volume_row(row)
        if isinstance(row, dict)
        else row
        for row in (envelope.get("rows") or [])
    ]
    return normalized


def _is_full_balance_show(intent: AnalysisIntent) -> bool:
    if (
        intent.operation != Operation.SHOW
        or len(intent.operands) != 1
        or intent.operands[0].metric != "balance"
        or intent.grouping
        or intent.comparison is not None
        or intent.formula is not None
        or intent.ranking is not None
    ):
        return False
    entities = intent.operands[0].entities
    return (
        len(entities) == 1
        and entities[0].role == "balance"
        and entities[0].entity.entity_type == "balance"
    )


def _single_operand_attempt(
    base: AnalysisIntent,
    operand: AnalysisOperand,
) -> AnalysisIntent:
    """Build a valid attempted scope for strict reverse ``no_data``.

    A failed reverse selected from a comparison cannot retain operation
    ``compare`` with one operand: that shape is invalid before the explicit
    no-data response can be produced.  Preserve the attempted canonical
    operand and its periods while dropping comparison-only structure.
    """
    operation = (
        Operation.AGGREGATE
        if operand.aggregate_type in {"min", "max", "avg", "first", "last"}
        else Operation.SHOW
    )
    return AnalysisIntent(
        operation=operation,
        operands=[operand.model_copy(deep=True)],
        periods=[item.model_copy(deep=True) for item in base.periods],
        grain=base.grain,
    )


def _series_reduction_contract_summary(native, plan, intent: AnalysisIntent):
    if len(plan.tasks) != 1 or len(native.task_results) != 1:
        return None
    task = plan.tasks[0]
    result = native.task_results[0]
    fact = result.fact
    if task.series_reduce is None or fact is None or not result.series:
        return None
    grain_names = {
        "day": ("Среднесуточное", "дневных"),
        "month": ("Среднемесячное", "месячных"),
        "quarter": ("Среднеквартальное", "квартальных"),
        "year": ("Среднегодовое", "годовых"),
    }
    prefix, bucket_label = grain_names.get(
        task.series_grain, ("Среднее", "временных")
    )
    metric_label = metric_definition(intent.operands[0].metric).public_label
    scope = next(
        (
            item.entity.display_name
            for item in intent.operands[0].entities
            if item.role == "balance"
        ),
        fact.label,
    )
    value = _format_ru_number(fact.value)
    reduction_label = {
        "avg": "среднее",
        "sum": "сумма",
        "min": "минимум",
        "max": "максимум",
        "first": "первое значение",
        "last": "последнее значение",
    }.get(task.series_reduce, task.series_reduce)
    bucket_aggregate_label = {
        "avg": "среднее",
        "sum": "сумма",
        "min": "минимум",
        "max": "максимум",
        "first": "первое значение",
        "last": "последнее значение",
    }.get(task.bucket_aggregate, task.bucket_aggregate)
    minimum = min(result.series, key=lambda item: item.value)
    maximum = max(result.series, key=lambda item: item.value)
    min_label = _ranking_dimension_label(task.series_grain, minimum)
    max_label = _ranking_dimension_label(task.series_grain, maximum)
    return {
        "title": f"{prefix} {metric_label}: {value} {fact.unit}",
        "text": (
            f"{prefix} {metric_label} по {scope} рассчитано как "
            f"{reduction_label} по {len(result.series)} {bucket_label} значениям. "
            f"Внутри каждого периода рассчитана {bucket_aggregate_label}. "
            f"Результат — {value} {fact.unit}."
        ),
        "bullets": [
            f"Баланс: {scope}",
            f"Количество {bucket_label} значений: {len(result.series)}",
            (
                f"Минимум: {_format_ru_number(minimum.value)} {minimum.unit}"
                + (f" ({min_label})" if min_label else "")
            ),
            (
                f"Максимум: {_format_ru_number(maximum.value)} {maximum.unit}"
                + (f" ({max_label})" if max_label else "")
            ),
        ],
        "metrics": {
            "value": str(fact.value),
            "unit": fact.unit,
            "reduction": task.series_reduce,
            "grain": task.series_grain,
            "bucket_aggregate": task.bucket_aggregate,
            "bucket_count": len(result.series),
        },
        "confidence": "high",
        "generated_by": "deterministic_contract",
    }


def _ranking_contract_summary(native, intent: AnalysisIntent) -> dict[str, Any] | None:
    ranking = native.ranking
    if ranking is None or not ranking.selected:
        return None
    selected = ranking.selected[0]
    label = "Максимум" if ranking.direction == "max" else "Минимум"
    dimension = _ranking_dimension_label(ranking.grain, selected)
    value = _format_ru_number(selected.value)
    return {
        "title": f"{label} по временным интервалам",
        "text": (
            f"{label} при зернистости {ranking.grain}: {value} {selected.unit}"
            f"{f' ({dimension})' if dimension else ''}."
        ),
        "bullets": [
            f"Период: {dimension}" if dimension else f"Зернистость: {ranking.grain}",
            f"Значение: {value} {selected.unit}",
        ],
        "metrics": {
            "direction": ranking.direction,
            "grain": ranking.grain,
            "value": str(selected.value),
            "source_row_count": ranking.source_row_count,
        },
        "confidence": "high",
        "generated_by": "deterministic_contract",
    }


def _ranking_dimension_label(grain: str, selected: Any) -> str:
    raw = ""
    if selected.periods:
        raw = str(selected.periods[0].get("date_from") or "")
    if not raw and selected.dimension:
        raw = str(selected.dimension.get("value") or "")
    try:
        value = date.fromisoformat(raw)
    except ValueError:
        return raw
    if grain == "day":
        return value.strftime("%d.%m.%Y")
    if grain == "month":
        months = (
            "январь", "февраль", "март", "апрель", "май", "июнь",
            "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
        )
        return f"{months[value.month - 1]} {value.year}"
    if grain == "quarter":
        return f"{(value.month - 1) // 3 + 1} квартал {value.year}"
    if grain == "year":
        return str(value.year)
    return raw


def _comparison_contract_summary(native, intent: AnalysisIntent) -> dict[str, Any] | None:
    """Present canonical comparison math without letting summary reverse it."""
    comparison_set = native.comparison_set
    if native.operation != Operation.COMPARE or comparison_set is None:
        return None
    members = comparison_set.members
    if len(members) < 2:
        return None
    baseline = members[0]
    bullets = [
        f"{item.label}: {_format_ru_number(item.value)} {item.unit}"
        for item in members
    ]
    if len(members) > 2:
        details = "; ".join(bullets)
        return {
            "title": f"Сравнение {len(members)} показателей",
            "text": (
                f"Базовый показатель — {baseline.label}: "
                f"{_format_ru_number(baseline.value)} {baseline.unit}. "
                f"Все значения: {details}."
            ),
            "bullets": bullets,
            "metrics": {
                "baseline_operand_id": baseline.operand_id,
                "members": [item.model_dump(mode="json") for item in members],
            },
            "confidence": "high",
            "generated_by": "deterministic_contract",
        }
    target = members[1]
    delta = target.delta_from_baseline
    percent = target.percent_change_from_baseline
    direction = "выше" if delta > 0 else "ниже" if delta < 0 else "равен"
    delta_text = _format_ru_number(abs(delta))
    percent_text = (
        f" ({_format_ru_number(abs(percent))}%)" if percent is not None else ""
    )
    return {
        "title": f"Сравнение: {baseline.label} и {target.label}",
        "text": (
            f"{baseline.label}: {_format_ru_number(baseline.value)} {baseline.unit}. "
            f"{target.label}: {_format_ru_number(target.value)} {target.unit}. "
            f"Второй показатель {direction} первого на {delta_text} "
            f"{target.unit}{percent_text}."
        ),
        "bullets": [
            *bullets,
            f"Отклонение target - baseline: {_format_ru_number(delta)} {target.unit}",
        ],
        "metrics": {
            "baseline_operand_id": baseline.operand_id,
            "target_operand_id": target.operand_id,
            "baseline": str(baseline.value),
            "target": str(target.value),
            "delta": str(delta),
            "percent_change": str(percent) if percent is not None else None,
        },
        "confidence": "high",
        "generated_by": "deterministic_contract",
    }


def _period_comparison_contract_summary(
    native, intent: AnalysisIntent
) -> dict[str, Any] | None:
    comparison = native.comparison
    if native.operation != Operation.COMPARE_PERIODS or comparison is None:
        return None
    facts = [item.fact for item in native.task_results if item.fact is not None]
    if len(facts) != 2:
        return None
    baseline, target = facts

    def period_label(fact, index: int) -> str:
        if not fact.periods:
            return f"Период {index}"
        value = fact.periods[0]
        try:
            start = date.fromisoformat(str(value.get("date_from")))
            end = date.fromisoformat(str(value.get("date_to"))) - timedelta(days=1)
            return f"Период {index} ({start:%d.%m.%Y}–{end:%d.%m.%Y})"
        except (TypeError, ValueError):
            return f"Период {index}"

    baseline_label = period_label(baseline, 1)
    target_label = period_label(target, 2)
    delta = comparison.delta
    percent = comparison.percent_change
    direction = "выше" if delta > 0 else "ниже" if delta < 0 else "равен"
    percent_text = (
        f" ({_format_ru_number(abs(percent))}%)" if percent is not None else ""
    )
    return {
        "title": "Сравнение двух периодов",
        "text": (
            f"{baseline_label}: {_format_ru_number(baseline.value)} {baseline.unit}. "
            f"{target_label}: {_format_ru_number(target.value)} {target.unit}. "
            f"Второй период {direction} первого на "
            f"{_format_ru_number(abs(delta))} {target.unit}{percent_text}."
        ),
        "bullets": [
            f"{baseline_label}: {_format_ru_number(baseline.value)} {baseline.unit}",
            f"{target_label}: {_format_ru_number(target.value)} {target.unit}",
            f"Отклонение target - baseline: {_format_ru_number(delta)} {target.unit}",
        ],
        "metrics": {
            "baseline": str(comparison.baseline_value),
            "target": str(comparison.target_value),
            "delta": str(delta),
            "percent_change": str(percent) if percent is not None else None,
        },
        "confidence": "high",
        "generated_by": "deterministic_contract",
    }


def _extremum_comparison_summary(
    native,
    intent: AnalysisIntent,
    *,
    upstream_summary: dict[str, Any] | None,
) -> dict[str, Any] | None:
    aggregates = [operand.aggregate_type for operand in intent.operands]
    if (
        native.operation != Operation.COMPARE
        or len(aggregates) != 2
        or set(aggregates) != {"max", "min"}
        or native.comparison is None
    ):
        return None
    if (
        isinstance(upstream_summary, dict)
        and upstream_summary.get("generated_by") == "llm"
    ):
        return None
    facts = [result.fact for result in native.task_results]
    if len(facts) != 2 or any(fact is None for fact in facts):
        return None
    by_aggregate = dict(zip(aggregates, facts))
    maximum = by_aggregate["max"]
    minimum = by_aggregate["min"]
    spread = maximum.value - minimum.value
    comparison = native.comparison
    direction = (
        "Минимум ниже максимума"
        if comparison.delta < 0
        else "Максимум выше минимума"
    )
    percent = abs(comparison.percent_change) if comparison.percent_change is not None else None
    percent_text = (
        f", или на {_format_ru_number(percent)}% относительно базового значения"
        if percent is not None
        else ""
    )
    maximum_date = _format_fact_date(maximum.extremum_at)
    minimum_date = _format_fact_date(minimum.extremum_at)
    return {
        "title": "Сравнение максимума и минимума за сохранённый период",
        "text": (
            f"Максимум составил {_format_ru_number(maximum.value)} {maximum.unit}"
            f"{f' и был достигнут {maximum_date}' if maximum_date else ''}. "
            f"Минимум составил {_format_ru_number(minimum.value)} {minimum.unit}"
            f"{f' и был достигнут {minimum_date}' if minimum_date else ''}. "
            f"{direction} на {_format_ru_number(abs(spread))} {maximum.unit}"
            f"{percent_text}."
        ),
        "bullets": [
            f"Максимум: {_format_ru_number(maximum.value)} {maximum.unit}"
            + (f" — {maximum_date}" if maximum_date else ""),
            f"Минимум: {_format_ru_number(minimum.value)} {minimum.unit}"
            + (f" — {minimum_date}" if minimum_date else ""),
        ],
        "metrics": {
            "maximum": str(maximum.value),
            "minimum": str(minimum.value),
            "delta": str(comparison.delta),
            "percent_change": (
                str(comparison.percent_change)
                if comparison.percent_change is not None
                else None
            ),
        },
        "confidence": "high",
        "generated_by": "deterministic_contract",
    }


def _format_ru_number(value: Any) -> str:
    number = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    rendered = f"{number:,.2f}"
    return rendered.replace(",", "\u00a0").replace(".", ",")


def _format_fact_date(value: Any) -> str | None:
    return value.strftime("%d.%m.%Y") if value is not None else None


_TECHNICAL_SUMMARY_TEXT = re.compile(
    r"^Операция\s+\S+\s+выполнена\s+через\s+deterministic execution layer\.?$",
    re.I,
)


def _public_summary(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    summary = _safe_mapping(value)
    if _TECHNICAL_SUMMARY_TEXT.fullmatch(str(summary.get("text") or "").strip()):
        summary.pop("text", None)
    return summary


def _summary_diagnostics(
    envelope: dict[str, Any], *, requested: bool = True
) -> dict[str, Any]:
    summary = envelope.get("summary") if isinstance(envelope.get("summary"), dict) else {}
    warning_codes = [
        str(item.get("code"))
        for item in (envelope.get("warnings") or [])
        if isinstance(item, dict) and item.get("code")
    ]
    generated_by = str(summary.get("generated_by") or "deterministic")
    return {
        "requested": requested,
        "status": (
            "skipped"
            if not requested
            else "fallback" if "summary_writer_failed" in warning_codes else "ok"
        ),
        "generated_by": generated_by,
        "technical_text_suppressed": bool(
            _TECHNICAL_SUMMARY_TEXT.fullmatch(str(summary.get("text") or "").strip())
        ),
    }


_SAFE_EXECUTION_PARAM_KEYS = {
    "aggregate_type",
    "article_id",
    "article_ids",
    "balance_id",
    "balance_ids",
    "date_from",
    "date_to",
    "day",
    "group_by",
    "period_grain",
}


def _safe_execution_evidence(envelope: dict[str, Any]) -> dict[str, Any]:
    """Return bounded plan evidence suitable for audit and Golden Queries.

    Raw SQL and DB rows remain private. Function name and canonical scalar
    parameters are enough to prove which execution contract was selected.
    """

    debug = envelope.get("debug") if isinstance(envelope.get("debug"), dict) else {}
    function = debug.get("sql_function")
    params = debug.get("params") if isinstance(debug.get("params"), dict) else {}
    safe_params: dict[str, Any] = {}
    for key, value in params.items():
        if str(key).casefold() not in _SAFE_EXECUTION_PARAM_KEYS:
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe_params[str(key)] = value
        elif isinstance(value, list) and len(value) <= 100 and all(
            isinstance(item, (str, int, float, bool)) or item is None
            for item in value
        ):
            safe_params[str(key)] = value
    evidence: dict[str, Any] = {}
    if isinstance(function, str) and function.strip():
        evidence["sql_function"] = function.strip()
    if safe_params:
        evidence["sql_params"] = safe_params
    return evidence


def _native_task_evidence(task: Any, result: Any) -> dict[str, Any]:
    """Expose bounded canonical evidence for one decomposed scalar task."""

    operand = task.scalar_intent.operands[0]
    periods = operand.periods or task.scalar_intent.periods
    evidence: dict[str, Any] = {
        "task_id": task.task_id,
        "operand_id": task.operand_id,
        "metric": operand.metric,
        "aggregate_type": operand.aggregate_type,
        "canonical_entities": [
            {
                "role": item.role,
                "entity_id": item.entity.entity_id,
                "entity_type": item.entity.entity_type,
            }
            for item in operand.entities
        ],
        "periods": [item.model_dump(mode="json") for item in periods],
    }
    evidence.update(_safe_execution_evidence(result.envelope))
    return evidence


def _public_pipeline_result(envelope: dict[str, Any]) -> dict[str, Any]:
    public_warnings, _ = _partition_public_warnings(envelope.get("warnings") or [])
    ranked = _bucket_rank_public_result(envelope)
    if ranked is not None:
        ranked["warnings"] = public_warnings
        return ranked
    return {
        "status": envelope.get("status"),
        "rows": [
            _public_result_row(item)
            for item in (envelope.get("rows") or [])
            if isinstance(item, dict)
        ],
        "summary": _public_summary(envelope.get("summary")),
        "warnings": public_warnings,
    }


_TECHNICAL_WARNING_PATTERNS = (
    re.compile(r"^unified selected .+ over .+ candidate$", re.I),
    re.compile(r"^unified normalized [a-z0-9_]+ from .+ to .+$", re.I),
    re.compile(r"^unified normalized additive .+ ranking to bucket sums$", re.I),
    re.compile(r"^unified analyzer disagreement detected.*$", re.I),
)


def _partition_public_warnings(values: Any) -> tuple[list[Any], list[str]]:
    public: list[Any] = []
    technical: list[str] = []
    for value in values if isinstance(values, list) else []:
        if isinstance(value, dict):
            text = str(
                value.get("message") or value.get("value") or value.get("raw") or ""
            ).strip()
        else:
            text = str(value).strip()
        if text and any(pattern.fullmatch(text) for pattern in _TECHNICAL_WARNING_PATTERNS):
            technical.append(text)
        else:
            public.append(value)
    return public, technical


_PRIVATE_RESULT_FIELDS = {
    "provenance", "raw_rows", "raw", "sql", "rendered_sql", "params", "debug"
}

_PUBLIC_HIDDEN_ROW_FIELDS = {
    "balance_id", "balance_ids", "article_id", "article_ids",
    *FORBIDDEN_PLAN_FIELDS,
}


def _public_result_row(value: dict[str, Any]) -> dict[str, Any]:
    canonical = _canonical_volume_row(value)
    row = {
        key: item
        for key, item in canonical.items()
        if key.casefold() not in _PUBLIC_HIDDEN_ROW_FIELDS
    }
    return row


def _canonical_volume_row(value: Mapping[str, Any]) -> dict[str, Any]:
    row = {
        key: item
        for key, item in _safe_mapping(dict(value)).items()
        if key.casefold() not in FORBIDDEN_PLAN_FIELDS
    }
    if any(key in row for key in ("fact", "fact_value", "amount", "volume")):
        row["unit"] = CANONICAL_VOLUME_UNIT
    return row


def _bucket_rank_public_result(envelope: dict[str, Any]) -> dict[str, Any] | None:
    plan = _resolved_plan_from_public_envelope(envelope)
    raw_intent = (
        plan.get("_intent")
        if isinstance(plan.get("_intent"), dict)
        else {}
    )
    if str(raw_intent.get("intent") or "").strip().lower() != "rank":
        return None
    grain = str(
        raw_intent.get("period_grain") or plan.get("period_grain") or ""
    ).strip().lower()
    if grain not in {"day", "month", "quarter", "year"}:
        return None
    aggregate = _rank_aggregate(raw_intent)
    if aggregate is None:
        return None
    rows = [item for item in (envelope.get("rows") or []) if isinstance(item, dict)]
    ranked: list[tuple[Decimal, int, dict[str, Any]]] = []
    for index, row in enumerate(rows):
        value = _row_decimal(row)
        if value is not None:
            ranked.append((value, index, row))
    if not ranked:
        return None
    selected_value, _index, selected_row = (
        max(ranked, key=lambda item: item[0])
        if aggregate == "max"
        else min(ranked, key=lambda item: item[0])
    )
    unit = _result_unit(envelope, selected_row)
    public_row = _public_result_row({**selected_row, **({"unit": unit} if unit else {})})
    metric_label = _metric_label(raw_intent.get("metric"))
    grain_label = _grain_label(grain)
    aggregate_label = "Максимум" if aggregate == "max" else "Минимум"
    period_label = _rank_period_label(selected_row, grain)
    value_text = _format_ru_number(selected_value)
    text = (
        f"{aggregate_label} {grain_label} {metric_label}"
        f"{f' был в {period_label}' if period_label else ' найден'}: "
        f"{value_text}{f' {unit}' if unit else ''}."
    )
    return {
        "status": envelope.get("status"),
        "rows": [public_row],
        "summary": {
            "title": f"{aggregate_label} по {grain_label} за период",
            "text": text,
            "bullets": [
                f"{period_label}: {value_text}{f' {unit}' if unit else ''}"
                if period_label else f"{value_text}{f' {unit}' if unit else ''}",
            ],
            "metrics": {
                aggregate: str(selected_value),
                "source_row_count": len(rows),
            },
            "confidence": "high",
            "generated_by": "deterministic_contract",
        },
        "warnings": [],
    }


def _resolved_plan_from_public_envelope(envelope: dict[str, Any]) -> dict[str, Any]:
    debug = envelope.get("debug") if isinstance(envelope.get("debug"), dict) else {}
    for key in (
        "resolved_plan",
        "pipeline_unified_resolved_plan",
        "pipeline_v2_resolved_plan",
    ):
        if isinstance(debug.get(key), dict):
            return debug[key]
    return {}


def _rank_aggregate(raw_intent: dict[str, Any]) -> str | None:
    aggregate = str(raw_intent.get("aggregate_type") or "").strip().lower()
    if aggregate in {"max", "min"}:
        return aggregate
    query = str(raw_intent.get("query") or "").casefold().replace("ё", "е")
    if re.search(r"\b(максим|наибольш|пик)", query):
        return "max"
    if re.search(r"\b(миним|наименьш)", query):
        return "min"
    return None


def _row_decimal(row: dict[str, Any]) -> Decimal | None:
    for key in ("fact_value", "fact", "value", "amount", "volume"):
        value = row.get(key)
        if value in (None, ""):
            continue
        try:
            return Decimal(str(value).replace(" ", "").replace(",", "."))
        except Exception:
            return None
    return None


def _result_unit(envelope: dict[str, Any], row: dict[str, Any]) -> str:
    del envelope, row
    return CANONICAL_VOLUME_UNIT


def _metric_label(metric: Any) -> str:
    labels = {
        "incoming": "поступления",
        "distribution": "распределения",
        "export": "экспорта",
        "stock": "запаса",
        "flow_balance": "транспорта газа",
    }
    return labels.get(str(metric or "").strip().lower(), "показателя")


def _grain_label(grain: str) -> str:
    return {
        "day": "по дням",
        "month": "по месяцам",
        "quarter": "по кварталам",
        "year": "по годам",
    }.get(grain, "по периодам")


def _rank_period_label(row: dict[str, Any], grain: str) -> str:
    start = _parse_iso_date(row.get("date_from"))
    end = _parse_iso_date(row.get("date_to"))
    if grain == "month" and start is not None:
        names = (
            "январь", "февраль", "март", "апрель", "май", "июнь",
            "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь",
        )
        return f"{names[start.month - 1]} {start.year}"
    if grain == "day" and start is not None:
        return start.strftime("%d.%m.%Y")
    if start is not None and end is not None:
        return f"{start.isoformat()} - {(end - timedelta(days=1)).isoformat()}"
    return str(row.get("period") or row.get("label") or "").strip()


def _parse_iso_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except Exception:
        return None


def _safe_mapping(value: dict[str, Any]) -> dict[str, Any]:
    return _public_safe_value(value)


def _public_safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        hidden = _PRIVATE_RESULT_FIELDS | _PUBLIC_HIDDEN_ROW_FIELDS
        return {
            str(key): _public_safe_value(item)
            for key, item in value.items()
            if str(key).casefold() not in hidden
        }
    if isinstance(value, list):
        return [_public_safe_value(item) for item in value]
    return value


def _diagnostics(decision, chunks, task_count, started):
    return {
        "interpretation": {
            "mode": decision.mode.value,
            "source": "qwen",
            "confidence": decision.confidence,
        },
        "result_memory": {
            "retrieved_chunks": len(chunks),
            "result_refs": list(dict.fromkeys(item.result_ref for item in chunks)),
        },
        "execution": {
            "task_count": task_count,
            "elapsed_ms": int((perf_counter() - started) * 1000),
        },
    }


def _should_summarize(interpretation_mode: str) -> bool:
    return interpretation_mode not in {
        "deterministic_period_patch",
        "deterministic_geo_patch",
    }


_MONTHS = (
    (r"\bянвар\w*", 1), (r"\bфеврал\w*", 2), (r"\bмарт\w*", 3),
    (r"\bапрел\w*", 4), (r"\bма(?:й|я|е|ю|ем)\b", 5),
    (r"\bиюн\w*", 6), (r"\bиюл\w*", 7), (r"\bавгуст\w*", 8),
    (r"\bсентябр\w*", 9), (r"\bоктябр\w*", 10),
    (r"\bноябр\w*", 11), (r"\bдекабр\w*", 12),
)

_SEASONS = (
    (r"\bвесн\w*", 3, 6, "весна"),
    (r"\bлет\w*", 6, 9, "лето"),
    (r"\bосен\w*", 9, 12, "осень"),
    (r"\bзим\w*", 12, 3, "зима"),
)


@dataclass(frozen=True, slots=True)
class PeriodPatchCandidate:
    periods: tuple[PeriodRef, ...]


@dataclass(frozen=True, slots=True)
class GeoPatchCandidate:
    geo: Any
    operands: tuple[AnalysisOperand, ...]


def _whole_geo_mention_matches(
    mention: str,
    geo: Any,
    normalize_lemmas: Any,
) -> bool:
    """Require the whole follow-up payload to name one metadata GEO."""

    def _tokens(value: str) -> list[str]:
        region_types = {"область": "обл", "област": "обл"}
        return [
            region_types.get(token, token)
            for token in normalize_lemmas(value).split()
        ]

    mention_tokens = _tokens(mention)
    if not mention_tokens:
        return False
    for label in (geo.canonical_name, *(geo.aliases or ())):
        label_tokens = _tokens(label)
        if len(label_tokens) != len(mention_tokens):
            continue
        threshold = 0.84 if len(label_tokens) == 1 else 0.76
        if all(
            SequenceMatcher(None, left, right).ratio() >= threshold
            for left, right in zip(mention_tokens, label_tokens)
        ):
            return True
    return False


def _detect_period_followup(
    message: str,
    active_intent: AnalysisIntent,
) -> PeriodPatchCandidate | None:
    if (
        active_intent.operation not in {Operation.SHOW, Operation.AGGREGATE}
        or len(active_intent.operands) != 1
        or len(active_intent.periods) != 1
        or any(operand.periods for operand in active_intent.operands)
    ):
        return None
    text = _normalize_text(message)
    followup = re.fullmatch(
        r"(?:а\s+)?(?:покажи\s+)?за\s+(?P<period>.+)",
        text,
    )
    if followup is None:
        return None
    period_text = followup.group("period")
    for month_pattern, month in _MONTHS:
        explicit_day = re.fullmatch(
            rf"[0-3]?\d\s+{month_pattern}\s+20\d{{2}}(?:\s+г(?:од(?:а)?)?)?",
            period_text,
        )
        if explicit_day is not None:
            period = _explicit_single_day_period(period_text)
            return (
                PeriodPatchCandidate(periods=(period,))
                if period is not None
                else None
            )
        named_month = re.fullmatch(
            rf"{month_pattern}(?:\s+(20\d{{2}})(?:\s+г(?:од(?:а)?)?)?)?",
            period_text,
        )
        if named_month is None:
            continue
        year = (
            int(named_month.group(1))
            if named_month.group(1)
            else active_intent.periods[0].date_from.year
        )
        date_from = date(year, month, 1)
        date_to = (
            date(year + 1, 1, 1)
            if month == 12
            else date(year, month + 1, 1)
        )
        return PeriodPatchCandidate(
            periods=(PeriodRef(date_from=date_from, date_to=date_to),)
        )
    return None


def _deterministic_period_patch(
    state: ContextContractV2,
    message: str,
    turn_id: str,
) -> ContextMutation | None:
    scope = state.active_dialog_scope
    if scope is None:
        return None
    candidate = _detect_period_followup(message, scope.intent)
    if candidate is None:
        return None
    return ContextMutation(
        turn_id=turn_id,
        user_message=message,
        normalized_message=message,
        patch=IntentPatch(
            periods=FieldMutation(
                action=MutationAction.SET,
                value=list(candidate.periods),
            )
        ),
    )


def _deterministic_period_mutation(state, message: str, turn_id: str):
    scope = state.active_dialog_scope
    if scope is None or len(scope.intent.operands) != 1:
        return None
    text = str(message).strip().lower().replace("ё", "е")
    if re.search(r"\bсравн\w*", text):
        named = _named_comparison_periods(scope.intent, text)
        if len(named) == 2:
            intent = scope.intent.model_copy(
                update={
                    "operation": Operation.COMPARE_PERIODS,
                    "periods": named,
                    "comparison": None,
                    "grouping": [],
                },
                deep=True,
            )
            return ContextMutation(
                turn_id=turn_id,
                user_message=message,
                normalized_message=message,
                replace_intent=intent,
            )
    if not re.search(r"\bсравн\w*\s+с\b", text):
        return None
    month = next((number for pattern, number in _MONTHS if re.search(pattern, text)), None)
    if month is None:
        return None
    year_match = re.search(r"\b(20\d{2})\b", text)
    if year_match:
        year = int(year_match.group(1))
    elif scope.intent.periods:
        year = scope.intent.periods[-1].date_from.year
    else:
        return None
    date_from = date(year, month, 1)
    date_to = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    target = PeriodRef(date_from=date_from, date_to=date_to)
    baseline = scope.intent.periods[-1] if scope.intent.periods else None
    if baseline is None or baseline == target:
        return None
    intent = scope.intent.model_copy(
        update={
            "operation": Operation.COMPARE_PERIODS,
            "periods": [baseline, target],
            "comparison": None,
            "grain": None,
        },
        deep=True,
    )
    return ContextMutation(
        turn_id=turn_id,
        user_message=message,
        normalized_message=message,
        replace_intent=intent,
    )


def _named_comparison_periods(intent: AnalysisIntent, text: str) -> list[Any]:
    years = [int(item) for item in re.findall(r"\b(20\d{2})\b", text)]
    if years:
        year = years[0]
    else:
        candidates = [period.date_from.year for period in intent.periods]
        year = min(candidates) if candidates else date.today().year
    matches: list[tuple[int, Any]] = []
    for pattern, start_month, end_month, label in _SEASONS:
        for match in re.finditer(pattern, text):
            end_year = year + 1 if end_month <= start_month else year
            matches.append((
                match.start(),
                PeriodRef(
                    date_from=date(year, start_month, 1),
                    date_to=date(end_year, end_month, 1),
                    label=label,
                ),
            ))
    if len(matches) < 2:
        for pattern, month in _MONTHS:
            for match in re.finditer(pattern, text):
                next_month = month % 12 + 1
                next_year = year + 1 if month == 12 else year
                matches.append((
                    match.start(),
                    PeriodRef(
                        date_from=date(year, month, 1),
                        date_to=date(next_year, next_month, 1),
                    ),
                ))
    ordered = [period for _position, period in sorted(matches, key=lambda item: item[0])]
    unique = []
    for period in ordered:
        key = (period.date_from, period.date_to)
        if key not in {(item.date_from, item.date_to) for item in unique}:
            unique.append(period)
    return unique[:2]


def _official_name(value: str) -> str:
    text = str(value).strip()
    return text[:1].upper() + text[1:] if text else text


def _canonical_grouping_unit(
    intent: AnalysisIntent,
    normalized_message: str,
    rows: Sequence[Mapping[str, Any]],
) -> str | None:
    """All grouped source facts use the authoritative physical unit."""
    del intent, normalized_message, rows
    return CANONICAL_VOLUME_UNIT


def _normalize_text(value: str) -> str:
    text = str(value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^0-9a-zа-я]+", " ", text).split())


_BUSINESS_ENTITY_BOUNDARIES = {
    "в", "из", "до", "между", "и", "за", "по", "для", "с", "на", "к", "от"
}


def _business_entity_token_spans(tokens: list[str]) -> list[tuple[int, int]]:
    """First pass: reserve names qualified by `ГП ТГ` / `ТГ` for business binding."""
    spans: list[tuple[int, int]] = []
    for index, token in enumerate(tokens):
        if token != "тг" or index + 1 >= len(tokens):
            continue
        start = index + 1
        end = start
        while end < len(tokens) and tokens[end] not in _BUSINESS_ENTITY_BOUNDARIES:
            end += 1
        if end > start:
            spans.append((start, end))
    return spans


def _metadata_numeric_id(value: Any) -> int:
    match = re.search(r"(\d+)$", str(value).strip())
    return int(match.group(1)) if match else -1


def _deterministic_extremum_comparison(state, message: str, turn_id: str):
    """Compare max/min over the exact active operand and canonical period."""
    match = _COMPARE_WITH_EXTREMUM.search(str(message))
    scope = state.active_dialog_scope
    if match is None or scope is None or len(scope.intent.operands) != 1:
        return None
    baseline = scope.intent.operands[0]
    if baseline.aggregate_type not in {"max", "min"}:
        return None
    target_aggregate = (
        "min"
        if match.group("extremum").casefold().startswith("миним")
        else "max"
    )
    if target_aggregate == baseline.aggregate_type:
        return None
    baseline = baseline.model_copy(update={"operand_id": "extremum_baseline"}, deep=True)
    target = baseline.model_copy(
        update={
            "operand_id": "extremum_target",
            "aggregate_type": target_aggregate,
        },
        deep=True,
    )
    labels = {"max": "максимум", "min": "минимум"}
    normalized = (
        f"Сравни {labels[baseline.aggregate_type]} и {labels[target_aggregate]} "
        "для сохранённых сущностей и периода"
    )
    intent = AnalysisIntent(
        operation=Operation.COMPARE,
        operands=[baseline, target],
        periods=[item.model_copy(deep=True) for item in scope.intent.periods],
        grain=scope.intent.grain,
        comparison=ComparisonSpec(
            baseline_operand_id=baseline.operand_id,
            target_operand_id=target.operand_id,
        ),
    )
    return ContextMutation(
        turn_id=turn_id,
        user_message=message,
        normalized_message=normalized,
        replace_intent=intent,
    )


def _balance_article_entities(balance, article) -> list[OperandEntityRef]:
    return [
        OperandEntityRef(
            role="balance",
            entity=CanonicalEntityRef(
                entity_id=str(balance.balance_id),
                entity_type="balance",
                display_name=balance.canonical_name,
            ),
        ),
        OperandEntityRef(
            role="article",
            entity=CanonicalEntityRef(
                entity_id=str(article.article_id),
                entity_type="article",
                display_name=article.canonical_name,
            ),
        ),
    ]


def _deterministic_grouping_query(state, message: str) -> str | None:
    scope = state.active_dialog_scope
    if scope is None or len(scope.intent.periods) != 1:
        return None
    text = _normalize_text(message)
    if not re.search(r"^(?:суммируй|сгруппируй|объедини)\b", text):
        return None
    if not re.search(r"\bпо\s+(?:областям|регионам|краям)\b", text):
        return None
    metrics = {operand.metric for operand in scope.intent.operands}
    if len(metrics) != 1:
        return None
    metric = next(iter(metrics))
    metric_labels = {
        "distribution": "поставки газа",
        "incoming": "поступление газа",
        "export": "экспорт газа",
        "stock": "запасы газа",
    }
    metric_label = metric_labels.get(metric)
    if metric_label is None:
        return None
    period = scope.intent.periods[0]
    inclusive_to = period.date_to - timedelta(days=1)
    return (
        f"Суммируй {metric_label} по областям за период с "
        f"{period.date_from.isoformat()} по {inclusive_to.isoformat()}"
    )


def _distribution_own_consumers_mentions(message: str) -> tuple[str, str] | None:
    match = re.search(
        r"\bсравн\w*\b.*?\bпостав\w*\b(?:\s+газ\w*)?\s+в\s+"
        r"(?P<destination>.+?)\s+и\s+(?:объем\w*\s+)?"
        r"собственн\w+\s+потребител\w*\s+"
        r"(?P<balance>.+?)(?=\s+\b(?:за|на)\b|[?.!]*$)",
        str(message),
        re.IGNORECASE,
    )
    if match is None:
        return None
    destination = match.group("destination").strip(" ,.;:?!")
    balance = match.group("balance").strip(" ,.;:?!")
    return (destination, balance) if destination and balance else None


def _peer_destination_mentions(message: str) -> list[str] | None:
    text = str(message).lower().replace("ё", "е")
    if not re.search(r"\bсравн\w*\b", text):
        return None
    if re.search(r"\bиз\b.+\b(?:в|до)\b", text):
        return None
    match = re.search(
        r"\bв\s+(.+?)(?=\s+\b(?:за|на)\b|[?.!]*$)",
        str(message),
        re.IGNORECASE,
    )
    if match is None:
        return None
    parts = [item.strip(" ,.;:?!") for item in re.split(r"\s+(?:и|с)\s+|,", match.group(1), flags=re.IGNORECASE)]
    return parts if len(parts) == 2 and all(parts) else None
