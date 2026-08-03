from __future__ import annotations

import hashlib
import json
import re
from datetime import date, timedelta
from time import perf_counter
from typing import Any
from uuid import uuid4

from .binding import ContextBindingError, InterpretationMutationCompiler
from .compat.envelope_translation import PipelineEnvelopeTranslator
from .contracts import (
    CanonicalEntityRef,
    ContextContractV2,
    ContextMutation,
    InterpretationMode,
    MetadataVersionRef,
    OperandEntityRef,
    Operation,
    ResultReference,
    TransitionOutcome,
)
from .execution import NativeExecutor
from .interpretation import HybridInterpretationPolicy, InterpretationError, UnifiedInterpreter
from .planning import NativeMultiOperandPlanner, PlanningError
from .result_memory import PipelineResultMemoryAdapter
from .service import TurnProcessResult, TurnProcessingError


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
    ) -> None:
        self.runtime = runtime
        self.registry = registry
        self.interpreter = interpreter
        self.compiler = compiler
        self.executor = executor
        self.result_memory = result_memory
        self.planner = planner or NativeMultiOperandPlanner()
        self.policy = policy or HybridInterpretationPolicy()
        self.translator = PipelineEnvelopeTranslator(registry)
        try:
            self._normalize_lemmas = runtime._import_pipeline_module(
                "pipeline_v2.nlp_ru"
            ).normalize_query_lemmas
        except Exception:
            self._normalize_lemmas = None

    def process(
        self,
        state: ContextContractV2,
        *,
        message: str,
        execute_db: bool,
        clarification: dict[str, Any] | None,
        request_id: str,
    ) -> TurnProcessResult:
        started = perf_counter()
        turn_id = str(uuid4())
        explicit_geos = self._matched_geo_objects(message)
        mixed_metric_operands = self._matched_distribution_own_consumers(message)
        if mixed_metric_operands:
            return self._canonical_entity_comparison(
                state,
                message=message,
                operand_entities=mixed_metric_operands,
                execute_db=execute_db,
                request_id=request_id,
                turn_id=turn_id,
                started=started,
                interpretation_mode="deterministic_distribution_own_consumers",
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
            return self._standalone(
                state,
                message=grouping_query,
                user_message=message,
                execute_db=execute_db,
                request_id=request_id,
                turn_id=turn_id,
                started=started,
                interpretation_mode="deterministic_context_grouping",
            )
        deterministic = _deterministic_period_mutation(state, message, turn_id)
        deterministic_mode = "deterministic_period"
        if deterministic is None:
            deterministic = self._deterministic_geo_mutation(state, message, turn_id)
            deterministic_mode = "deterministic_geo"
        if deterministic is not None:
            return self._execute_mutation(
                state,
                deterministic,
                normalized_message=deterministic.normalized_message or message,
                execute_db=execute_db,
                request_id=request_id,
                started=started,
                interpretation_mode=deterministic_mode,
                memory_chunks=[],
            )
        if not self.policy.should_invoke(
            message,
            state,
            clarification_answer=clarification is not None,
        ):
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
                    *(f"explicit_geo:{item.canonical_name}" for item in explicit_geos),
                    *_domain_hints(self.registry),
                ],
                metadata_bundle_version=self.registry.manifest.bundle_version,
                result_references=(
                    self.result_memory.for_interpretation(memory_chunks)
                    if self.result_memory
                    else []
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
            )
        except ContextBindingError as exc:
            raise TurnProcessingError(
                "interpretation could not be bound to canonical metadata",
                code="interpretation_binding_failed",
            ) from exc
        return self._execute_mutation(
            state,
            mutation,
            normalized_message=decision.normalized_message,
            execute_db=execute_db,
            request_id=request_id,
            started=started,
            interpretation_mode=decision.mode.value,
            memory_chunks=memory_chunks,
            decision=decision,
        )

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

    def _canonical_entity_comparison(
        self,
        state,
        *,
        message: str,
        operand_entities: list[list[OperandEntityRef]],
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
        return self._execute_mutation(
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
        normalized_message,
        execute_db,
        request_id,
        started,
        interpretation_mode,
        memory_chunks,
        decision=None,
    ):
        intent = mutation.replace_intent
        try:
            plan = self.planner.plan(intent)
        except PlanningError as exc:
            raise TurnProcessingError(
                "native intent planning failed", code="native_planning_failed"
            ) from exc
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
        result_ref = None
        memory_diag: dict[str, Any] = self.result_memory.safe_diagnostics(memory_chunks) if self.result_memory else {}
        if outcome == TransitionOutcome.SUCCESS:
            result_ref = _result_reference(mutation.turn_id, native, intent)
            if self.result_memory and execute_db and state.metadata:
                persisted = self.result_memory.persist(
                    session_id=state.session_id,
                    revision=state.revision + 1,
                    turn_id=mutation.turn_id,
                    query=normalized_message,
                    intent=intent,
                    result=native,
                    metadata=state.metadata,
                    result_id=result_ref.result_id,
                )
                memory_diag.update(
                    persisted=bool(persisted.get("persisted")),
                    stored_chunks=int(persisted.get("chunks") or 0),
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
                    "elapsed_ms": int((perf_counter() - started) * 1000),
                },
            }
        )
        diagnostics["result_memory"] = memory_diag
        return TurnProcessResult(
            mutation=mutation,
            outcome=outcome,
            response=_public_native_result(native),
            result_reference=result_ref,
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
        try:
            intent = self.translator.intent(
                envelope,
                canonical_geos=self._matched_geo_objects(user_message or message),
            )
        except Exception as exc:
            code = (
                "resolved_comparison_degraded"
                if str(exc) == "resolved_comparison_degraded"
                else "resolved_plan_translation_failed"
            )
            raise TurnProcessingError(
                "standalone resolved plan translation failed", code=code
            ) from exc
        mutation = ContextMutation(
            turn_id=turn_id,
            user_message=user_message or message,
            normalized_message=message,
            replace_intent=intent,
        )
        return TurnProcessResult(
            mutation=mutation,
            outcome=_outcome(status),
            response=_public_pipeline_result(envelope),
            diagnostics={
                "interpretation": {
                    "mode": interpretation_mode,
                    "source": "pipeline",
                },
                "execution": {
                    "status": status,
                    "task_count": 1,
                    "elapsed_ms": int((perf_counter() - started) * 1000),
                },
            },
        )


def metadata_ref(registry: Any) -> MetadataVersionRef:
    manifest = registry.manifest
    return MetadataVersionRef(
        bundle_id=manifest.bundle_id,
        bundle_version=manifest.bundle_version,
        schema_version=manifest.schema_version,
    )


def _capabilities() -> list[str]:
    return [
        "show", "aggregate", "compare", "compare_periods", "group",
        "distribution", "incoming", "own_needs", "stock", "export",
        "day", "month", "quarter", "year", "geo_group",
    ]


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


def _result_reference(turn_id, native, intent) -> ResultReference:
    facts = [
        item.fact.model_dump(mode="json")
        for item in native.task_results
        if item.fact is not None
    ]
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


def _public_native_result(native) -> dict[str, Any]:
    return {
        "operation": native.operation.value,
        "status": native.status,
        "facts": [
            item.fact.model_dump(mode="json")
            for item in native.task_results
            if item.fact is not None
        ],
        "comparison": (
            native.comparison.model_dump(mode="json") if native.comparison else None
        ),
    }


def _public_pipeline_result(envelope: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": envelope.get("status"),
        "rows": envelope.get("rows") or [],
        "summary": envelope.get("summary") if isinstance(envelope.get("summary"), dict) else None,
        "warnings": envelope.get("warnings") or [],
    }


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


_MONTHS = (
    (r"\bянвар\w*", 1), (r"\bфеврал\w*", 2), (r"\bмарт\w*", 3),
    (r"\bапрел\w*", 4), (r"\bма(?:й|я|е|ю|ем)\b", 5),
    (r"\bиюн\w*", 6), (r"\bиюл\w*", 7), (r"\bавгуст\w*", 8),
    (r"\bсентябр\w*", 9), (r"\bоктябр\w*", 10),
    (r"\bноябр\w*", 11), (r"\bдекабр\w*", 12),
)


def _deterministic_period_mutation(state, message: str, turn_id: str):
    scope = state.active_dialog_scope
    if scope is None or len(scope.intent.operands) != 1:
        return None
    text = str(message).strip().lower().replace("ё", "е")
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
    from .contracts import Operation, PeriodRef

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


def _official_name(value: str) -> str:
    text = str(value).strip()
    return text[:1].upper() + text[1:] if text else text


def _normalize_text(value: str) -> str:
    text = str(value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^0-9a-zа-я]+", " ", text).split())


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
        r"собственн\w+\s+(?:потребител\w*|нужд\w*)\s+"
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
