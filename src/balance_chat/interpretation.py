from __future__ import annotations

import json
import hashlib
import logging
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence

from .contracts import ContextContractV2, InterpretationDecision
from .conversation import DEFAULT_WINDOW_SIZE, model_window_payload
from .observability import log_event


LOGGER = logging.getLogger("balance_chat.interpretation")


class InterpretationError(RuntimeError):
    pass


class InterpretationBackend(Protocol):
    def invoke(self, payload: Mapping[str, Any]) -> Mapping[str, Any] | str: ...


class UnifiedInterpreter:
    """One strict normalization + context interpretation boundary."""

    def __init__(
        self,
        backend: InterpretationBackend,
        *,
        prompt_path: str | Path | None = None,
        max_capabilities: int = 32,
        max_domain_hints: int = 24,
        conversation_window_size: int = DEFAULT_WINDOW_SIZE,
    ) -> None:
        self.backend = backend
        self.prompt_path = Path(prompt_path or _default_prompt_path()).resolve()
        self.system_prompt = self.prompt_path.read_text(encoding="utf-8")
        self.max_capabilities = max(1, int(max_capabilities))
        self.max_domain_hints = max(1, int(max_domain_hints))
        self.conversation_window_size = max(5, min(int(conversation_window_size), 7))

    def interpret(
        self,
        *,
        message: str,
        state: ContextContractV2,
        capabilities: Sequence[str],
        domain_hints: Sequence[str],
        metadata_bundle_version: str,
        result_references: Sequence[Mapping[str, Any]] = (),
        current_message_tags: Sequence[Mapping[str, Any]] = (),
        clarification_answer: Mapping[str, Any] | None = None,
        request_id: str | None = None,
    ) -> InterpretationDecision:
        clean_message = str(message).strip()
        if not clean_message:
            raise InterpretationError("message is empty")
        payload = {
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "message": clean_message,
                            "context": _bounded_context(
                                state, limit=self.conversation_window_size
                            ),
                            "capabilities": list(capabilities)[: self.max_capabilities],
                            "domain_hints": list(domain_hints)[: self.max_domain_hints],
                            "result_references": list(result_references)[:8],
                            "current_message_tags": list(current_message_tags)[:16],
                            "clarification_answer": clarification_answer,
                            "metadata_bundle_version": metadata_bundle_version,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            ],
            "request_id": request_id,
            "allowed_modes": ["mutation", "clarify", "unsupported"],
        }
        raw: Mapping[str, Any] | str | None = None
        try:
            raw = self.backend.invoke(payload)
            if isinstance(raw, str):
                raw = json.loads(raw)
            raw, canonicalized = _canonicalize_directives(raw)
            if canonicalized:
                log_event(
                    LOGGER,
                    logging.INFO,
                    "interpretation_directives_canonicalized",
                    request_id=request_id,
                    fields=canonicalized,
                )
            decision = InterpretationDecision.model_validate(raw)
        except Exception as exc:
            log_event(
                LOGGER,
                logging.ERROR,
                "interpretation_contract_invalid",
                request_id=request_id,
                error_type=type(exc).__name__,
                model_payload_sha256=(
                    hashlib.sha256(
                        json.dumps(raw, ensure_ascii=False, default=str, sort_keys=True).encode("utf-8")
                    ).hexdigest()
                    if raw is not None else None
                ),
                exc_info=True,
            )
            raise InterpretationError("interpretation contract validation failed") from exc
        if decision.metadata_bundle_version != metadata_bundle_version:
            raise InterpretationError("interpreter changed metadata bundle version")
        _reject_runtime_ids(decision)
        graph = decision.intent_graph
        log_event(
            LOGGER,
            logging.INFO,
            "interpretation_contract_resolved",
            request_id=request_id,
            session_id=state.session_id,
            revision=state.revision,
            conversation_turns=len(
                model_window_payload(state, limit=self.conversation_window_size)
            ),
            mode=decision.mode.value,
            normalized_message=decision.normalized_message,
            confidence=decision.confidence,
            contract_shape="intent_graph" if graph is not None else "legacy_draft",
            operation=(graph.operation.value if graph is not None else None),
            source_operand_handles=(
                [item.source_operand_handle for item in graph.operands]
                if graph is not None else []
            ),
            referenced_entity_handles=(
                [handle for item in graph.operands for handle in item.entity_handles]
                if graph is not None else []
            ),
            new_entity_mentions=(
                [
                    {"text": mention.text, "role": mention.role}
                    for item in graph.operands
                    for mention in item.entity_mentions
                ]
                if graph is not None else []
            ),
            referenced_period_handles=(
                [
                    *graph.period_handles,
                    *(handle for item in graph.operands for handle in item.period_handles),
                ]
                if graph is not None else []
            ),
        )
        return decision


class HybridInterpretationPolicy:
    """Every turn in an active dialogue uses the conversational interpreter."""

    _CONTEXT_MARKERS = re.compile(
        r"^\s*(?:а\b|и\b|теперь\b|сравни\b|суммируй\b|покажи\s+(?:их|это|тоже)\b)",
        re.IGNORECASE,
    )
    _REFERENCES = re.compile(
        r"\b(?:их|это|этот|эта|эти|предыдущ\w*|перв\w*|втор\w*|обратно|наоборот)\b",
        re.IGNORECASE,
    )

    def should_invoke(
        self,
        message: str,
        state: ContextContractV2,
        *,
        likely_typo: bool = False,
        clarification_answer: bool = False,
    ) -> bool:
        if likely_typo or clarification_answer:
            return True
        return state.active_dialog_scope is not None


def _bounded_context(
    state: ContextContractV2,
    *,
    limit: int = DEFAULT_WINDOW_SIZE,
) -> dict[str, Any]:
    def scope_payload(name: str) -> Any:
        scope = getattr(state, name)
        return scope.model_dump(mode="json") if scope is not None else None

    return {
        "contract_version": state.contract_version,
        "revision": state.revision,
        "conversation_window": model_window_payload(state, limit=limit),
        "active_dialog_scope": scope_payload("active_dialog_scope"),
        "last_attempted_scope": scope_payload("last_attempted_scope"),
        "last_successful_scope": scope_payload("last_successful_scope"),
        "entity_memory": [
            item.model_dump(mode="json") for item in state.entity_memory[-24:]
        ],
        "recent_turns": [
            item.model_dump(mode="json") for item in state.recent_turns[-limit:]
        ],
        "pending_clarification": (
            state.pending_clarification.model_dump(mode="json")
            if state.pending_clarification is not None else None
        ),
    }


def _reject_runtime_ids(decision: InterpretationDecision) -> None:
    mentions = list(decision.draft.entities.mentions) if decision.draft else []
    if decision.intent_graph is not None:
        mentions.extend(
            mention
            for operand in decision.intent_graph.operands
            for mention in operand.entity_mentions
        )
    for mention in mentions:
        if re.search(r"\b(?:entity_id|balance_id|article_id|route_id)\b", mention.text, re.I):
            raise InterpretationError("interpreter returned a runtime identifier")


def _canonicalize_directives(
    raw: Mapping[str, Any] | Any,
) -> tuple[Mapping[str, Any] | Any, list[str]]:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("draft"), Mapping):
        return raw, []
    payload = json.loads(json.dumps(raw, ensure_ascii=False, default=str))
    draft = payload["draft"]
    changed: list[str] = []
    for field in ("operation", "aggregate_type", "grain"):
        directive = draft.get(field)
        if isinstance(directive, dict) and directive.get("action") == "set" and directive.get("value") is None:
            directive.update(action="clear", value=None)
            changed.append(field)
    for field, values_key in (
        ("metrics", "values"),
        ("periods", "values"),
        ("grouping", "values"),
        ("entities", "mentions"),
    ):
        directive = draft.get(field)
        if (
            isinstance(directive, dict)
            and directive.get("action") in {"set", "add", "remove"}
            and not directive.get(values_key)
        ):
            directive["action"] = "clear"
            changed.append(field)
    return payload, changed


def _default_prompt_path() -> Path:
    return Path(__file__).resolve().parent / "prompts" / "interpretation_system_prompt.md"
