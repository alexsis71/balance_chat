from __future__ import annotations

import json
import hashlib
import logging
from pathlib import Path
import re
from typing import Any, Mapping, Protocol, Sequence

from .contracts import ContextContractV2, InterpretationDecision
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
    ) -> None:
        self.backend = backend
        self.prompt_path = Path(prompt_path or _default_prompt_path()).resolve()
        self.system_prompt = self.prompt_path.read_text(encoding="utf-8")
        self.max_capabilities = max(1, int(max_capabilities))
        self.max_domain_hints = max(1, int(max_domain_hints))

    def interpret(
        self,
        *,
        message: str,
        state: ContextContractV2,
        capabilities: Sequence[str],
        domain_hints: Sequence[str],
        metadata_bundle_version: str,
        result_references: Sequence[Mapping[str, Any]] = (),
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
                            "context": _bounded_context(state),
                            "capabilities": list(capabilities)[: self.max_capabilities],
                            "domain_hints": list(domain_hints)[: self.max_domain_hints],
                            "result_references": list(result_references)[:8],
                            "clarification_answer": clarification_answer,
                            "metadata_bundle_version": metadata_bundle_version,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                },
            ],
            "request_id": request_id,
        }
        raw: Mapping[str, Any] | str | None = None
        try:
            raw = self.backend.invoke(payload)
            if isinstance(raw, str):
                raw = json.loads(raw)
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
        return decision


class HybridInterpretationPolicy:
    """Conservative trigger: deterministic standalone turns bypass the LLM."""

    _CONTEXT_MARKERS = re.compile(
        r"^\s*(?:а\b|и\b|теперь\b|сравни\b|суммируй\b|покажи\s+(?:их|это|тоже)\b)",
        re.IGNORECASE,
    )
    _REFERENCES = re.compile(
        r"\b(?:их|это|этот|эта|эти|предыдущ\w*|перв\w*|втор\w*|обратно)\b",
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
        if state.active_dialog_scope is None:
            return False
        text = str(message).strip()
        return bool(self._CONTEXT_MARKERS.search(text) or self._REFERENCES.search(text))


def _bounded_context(state: ContextContractV2) -> dict[str, Any]:
    def scope_payload(name: str) -> Any:
        scope = getattr(state, name)
        return scope.model_dump(mode="json") if scope is not None else None

    return {
        "contract_version": state.contract_version,
        "revision": state.revision,
        "active_dialog_scope": scope_payload("active_dialog_scope"),
        "last_attempted_scope": scope_payload("last_attempted_scope"),
        "last_successful_scope": scope_payload("last_successful_scope"),
        "entity_memory": [
            item.model_dump(mode="json") for item in state.entity_memory[-24:]
        ],
        "recent_turns": [
            item.model_dump(mode="json") for item in state.recent_turns[-8:]
        ],
        "pending_clarification": (
            state.pending_clarification.model_dump(mode="json")
            if state.pending_clarification is not None else None
        ),
    }


def _reject_runtime_ids(decision: InterpretationDecision) -> None:
    if decision.draft is None:
        return
    for mention in decision.draft.entities.mentions:
        if re.search(r"\b(?:entity_id|balance_id|article_id|route_id)\b", mention.text, re.I):
            raise InterpretationError("interpreter returned a runtime identifier")


def _default_prompt_path() -> Path:
    return Path(__file__).resolve().parent / "prompts" / "interpretation_system_prompt.md"
