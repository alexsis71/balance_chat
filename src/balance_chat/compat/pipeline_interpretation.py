from __future__ import annotations

from typing import Any, Mapping

from ..contracts import interpretation_decision_json_schema


class PipelineInterpretationBackend:
    """Use pipeline's configured Qwen context profile with the V2 JSON Schema."""

    def __init__(self, runtime: Any, *, max_tokens: int = 1024) -> None:
        self.runtime = runtime
        self.max_tokens = max(128, int(max_tokens))

    def invoke(self, payload: Mapping[str, Any]) -> str:
        models = self.runtime._import_pipeline_module("inference.models")
        client = self.runtime.pipeline_runtime().inference_client
        response = client.chat(
            "context",
            payload["messages"],
            models.OutputContract.json_schema(
                "balance_chat_interpretation_v2",
                interpretation_decision_json_schema(
                    list(payload.get("allowed_modes") or [])
                ),
            ),
            models.SamplingConfig(
                temperature=0.0,
                top_p=1.0,
                max_tokens=self.max_tokens,
            ),
            request_id=payload.get("request_id"),
        )
        return response.content
