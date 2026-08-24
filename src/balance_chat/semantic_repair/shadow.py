from __future__ import annotations

import json
from pathlib import Path
from time import perf_counter
from typing import Any, Mapping

from .backend import (
    SemanticShadowBackend,
    SemanticShadowTimeout,
    SemanticShadowUnavailable,
)
from .context import SemanticShadowContext, build_semantic_shadow_context
from .contracts import (
    SemanticShadowResult,
    ShadowValidationStatus,
    semantic_transition_proposal_json_schema,
)
from .validator import ProposalValidator
from ..contracts import ContextContractV2


DETERMINISTIC_PATCH_MODES = frozenset(
    {
        "deterministic_period_patch",
        "deterministic_geo_patch",
        "deterministic_business_entity_patch",
    }
)

SHADOW_ELIGIBLE_MODES = frozenset({"conversation_graph"})


class SemanticShadowRunner:
    def __init__(
        self,
        backend: SemanticShadowBackend | None,
        *,
        enabled: bool = False,
        validator: ProposalValidator | None = None,
        prompt_path: str | Path | None = None,
        max_turns: int = 4,
        max_results: int = 4,
    ) -> None:
        self.backend = backend
        self.enabled = bool(enabled)
        self.validator = validator or ProposalValidator()
        self.max_turns = max(1, min(int(max_turns), 4))
        self.max_results = max(1, min(int(max_results), 4))
        path = Path(prompt_path or _default_prompt_path()).resolve()
        self.system_prompt = path.read_text(encoding="utf-8")

    def run(
        self,
        *,
        state: ContextContractV2,
        message: str,
        interpretation_mode: str | None,
        request_id: str,
    ) -> SemanticShadowResult:
        eligible = _eligible(state, message, interpretation_mode)
        if not self.enabled or not eligible:
            return SemanticShadowResult(
                eligible=eligible,
                invoked=False,
                validation_status=ShadowValidationStatus.SKIPPED,
            )
        if self.backend is None:
            return SemanticShadowResult(
                eligible=True,
                invoked=False,
                validation_status=ShadowValidationStatus.UNAVAILABLE,
                validation_errors=["backend_unavailable"],
            )

        context = build_semantic_shadow_context(
            state,
            message,
            max_turns=self.max_turns,
            max_results=self.max_results,
        )
        return self.run_context(context, request_id=request_id)

    def run_context(
        self,
        context: SemanticShadowContext,
        *,
        request_id: str,
    ) -> SemanticShadowResult:
        """Invoke the same bounded contract for the dedicated offline evaluator."""

        if not self.enabled:
            return SemanticShadowResult(
                eligible=True,
                invoked=False,
                validation_status=ShadowValidationStatus.SKIPPED,
            )
        if self.backend is None:
            return SemanticShadowResult(
                eligible=True,
                invoked=False,
                validation_status=ShadowValidationStatus.UNAVAILABLE,
                validation_errors=["backend_unavailable"],
            )
        context_payload = context.model_dump(mode="json")
        serialized_context = json.dumps(
            context_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        common = {
            "eligible": True,
            "invoked": True,
            "context_turn_count": len(context.recent_semantic_turns),
            "context_result_count": len(context.recent_addressable_results),
            "context_size_chars": len(serialized_context),
        }
        started = perf_counter()
        try:
            response = self.backend.invoke(
                [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": serialized_context},
                ],
                semantic_transition_proposal_json_schema(),
                request_id=f"{request_id}:semantic-shadow",
            )
        except SemanticShadowTimeout:
            return SemanticShadowResult(
                **common,
                validation_status=ShadowValidationStatus.TIMEOUT,
                validation_errors=["timeout"],
                latency_ms=_elapsed_ms(started),
            )
        except SemanticShadowUnavailable:
            return SemanticShadowResult(
                **common,
                validation_status=ShadowValidationStatus.UNAVAILABLE,
                validation_errors=["unavailable"],
                latency_ms=_elapsed_ms(started),
            )
        except Exception as exc:
            return SemanticShadowResult(
                **common,
                validation_status=ShadowValidationStatus.UNAVAILABLE,
                validation_errors=[f"backend_error:{type(exc).__name__}"],
                latency_ms=_elapsed_ms(started),
            )
        latency_ms = _elapsed_ms(started)
        try:
            payload = json.loads(response.content)
            if not isinstance(payload, Mapping):
                raise TypeError("proposal must be an object")
        except (json.JSONDecodeError, TypeError):
            return SemanticShadowResult(
                **common,
                validation_status=ShadowValidationStatus.MALFORMED,
                validation_errors=["malformed_json"],
                latency_ms=latency_ms,
                model=response.model,
            )
        validation = self.validator.validate(
            payload,
            output_chars=len(response.content),
        )
        return SemanticShadowResult(
            **common,
            proposal=validation.proposal,
            validation_status=(
                ShadowValidationStatus.VALID
                if validation.valid
                else ShadowValidationStatus.REJECTED
            ),
            validation_errors=list(validation.errors),
            latency_ms=latency_ms,
            model=response.model,
        )


def _eligible(
    state: ContextContractV2,
    message: str,
    interpretation_mode: str | None,
) -> bool:
    return bool(
        state.active_dialog_scope is not None
        and str(message).strip()
        and interpretation_mode in SHADOW_ELIGIBLE_MODES
    )


def _elapsed_ms(started: float) -> int:
    return max(0, int((perf_counter() - started) * 1000))


def _default_prompt_path() -> Path:
    return Path(__file__).resolve().parents[1] / "prompts" / "semantic_transition_shadow.md"
