from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping, Protocol, Sequence


class SemanticShadowTimeout(RuntimeError):
    pass


class SemanticShadowUnavailable(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ShadowModelResponse:
    content: str
    model: str | None = None
    latency_ms: int | None = None


class SemanticShadowBackend(Protocol):
    def invoke(
        self,
        messages: Sequence[Mapping[str, Any]],
        schema: Mapping[str, Any],
        *,
        request_id: str,
    ) -> ShadowModelResponse: ...


class PipelineSemanticShadowBackend:
    """Bounded adapter over the existing configured OpenAI-compatible client."""

    def __init__(
        self,
        runtime: Any,
        *,
        profile: str = "context",
        timeout_s: float = 12.0,
        max_tokens: int = 512,
    ) -> None:
        self.profile = str(profile)
        self.max_tokens = max(128, min(int(max_tokens), 1024))
        client_module = runtime._import_pipeline_module("inference.client")
        models = runtime._import_pipeline_module("inference.models")
        configured = runtime.pipeline_runtime().inference_client
        selected = configured.profiles[self.profile]
        bounded_timeout = max(1.0, min(float(timeout_s), selected.read_timeout_s))
        bounded_profile = replace(
            selected,
            connect_timeout_s=min(selected.connect_timeout_s, bounded_timeout),
            read_timeout_s=bounded_timeout,
            connect_retries=0,
            max_tokens=min(selected.max_tokens, self.max_tokens),
        )
        self._client = client_module.InferenceClient({self.profile: bounded_profile})
        self._models = models

    def invoke(
        self,
        messages: Sequence[Mapping[str, Any]],
        schema: Mapping[str, Any],
        *,
        request_id: str,
    ) -> ShadowModelResponse:
        try:
            response = self._client.chat(
                self.profile,
                messages,
                self._models.OutputContract.json_schema(
                    "semantic_transition_proposal_v1", schema
                ),
                self._models.SamplingConfig(
                    temperature=0.0,
                    top_p=1.0,
                    max_tokens=self.max_tokens,
                ),
                request_id=request_id,
            )
        except Exception as exc:
            name = type(exc).__name__.casefold()
            if "timeout" in name:
                raise SemanticShadowTimeout("semantic shadow timed out") from exc
            raise SemanticShadowUnavailable("semantic shadow unavailable") from exc
        return ShadowModelResponse(
            content=response.content,
            model=response.model,
            latency_ms=response.latency_ms,
        )
