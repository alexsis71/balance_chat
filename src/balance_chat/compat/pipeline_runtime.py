from __future__ import annotations

import importlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..contracts import AnalysisIntent
from .legacy_projection import project_legacy_context_override


class PipelineRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    pipeline_root: Path
    metadata_manifest: Path

    @classmethod
    def from_environment(cls) -> "RuntimeConfig":
        pipeline_value = os.getenv("BALANCE_CHAT_PIPELINE_ROOT")
        manifest_value = os.getenv("BALANCE_CHAT_METADATA_MANIFEST")
        if not pipeline_value or not manifest_value:
            raise PipelineRuntimeError(
                "BALANCE_CHAT_PIPELINE_ROOT and BALANCE_CHAT_METADATA_MANIFEST are required"
            )
        return cls(Path(pipeline_value), Path(manifest_value)).validated()

    @classmethod
    def from_json(cls, path: str | Path) -> "RuntimeConfig":
        config_path = Path(path).resolve()
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        root = Path(payload["pipeline_root"])
        manifest = Path(payload["metadata_manifest"])
        if not root.is_absolute():
            root = (config_path.parent / root).resolve()
        if not manifest.is_absolute():
            manifest = (config_path.parent / manifest).resolve()
        return cls(root, manifest).validated()

    def validated(self) -> "RuntimeConfig":
        root = self.pipeline_root.resolve()
        manifest = self.metadata_manifest.resolve()
        if not (root / "README.md").is_file() or not (root / "pipeline_api.py").is_file():
            raise PipelineRuntimeError(f"invalid pipeline root: {root}")
        if not manifest.is_file():
            raise PipelineRuntimeError(f"metadata manifest does not exist: {manifest}")
        try:
            manifest.relative_to(root)
        except ValueError as exc:
            raise PipelineRuntimeError("metadata manifest must be inside pipeline root") from exc
        return RuntimeConfig(root, manifest)


class PipelineRuntime:
    """Explicit bridge to the current pipeline; imports happen only on demand."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config.validated()

    def _import_pipeline_module(self, module_name: str) -> Any:
        root = str(self.config.pipeline_root)
        if root not in sys.path:
            sys.path.insert(0, root)
        try:
            return importlib.import_module(module_name)
        except Exception as exc:
            raise PipelineRuntimeError(f"failed to import pipeline module {module_name}") from exc

    def load_metadata_registry(self) -> Any:
        registry_module = self._import_pipeline_module("metadata_bundle.registry")
        return registry_module.MetadataRegistry.load(self.config.metadata_manifest, strict=True)

    def execute(
        self,
        query: str,
        intent: AnalysisIntent,
        *,
        execute_db: bool = False,
        request_id: str | None = None,
        apply_summary: bool = True,
    ) -> dict[str, Any]:
        context_override = project_legacy_context_override(intent)
        pipeline_api = self._import_pipeline_module("pipeline_api")
        return pipeline_api.execute_query(
            query,
            execute_db=execute_db,
            request_id=request_id,
            allow_multi_step=False,
            apply_summary=apply_summary,
            backend_override="unified_strict",
            context_override=context_override,
        )
