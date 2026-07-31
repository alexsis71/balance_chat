from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .api import create_app
from .binding import InterpretationMutationCompiler, RegistryEntityBinder
from .compat import PipelineInterpretationBackend, PipelineRuntime, RuntimeConfig
from .compat.envelope_translation import PipelineEnvelopeTranslator
from .execution import NativeExecutor, PipelineScalarTaskRunner
from .interpretation import UnifiedInterpreter
from .processor import PipelineV2TurnProcessor, metadata_ref
from .result_memory import PipelineResultMemoryAdapter
from .service import BalanceChatService
from .store import PostgresContextStore, SQLiteContextStore


def build_application(config_path: str | Path):
    path = Path(config_path).resolve()
    config = json.loads(path.read_text(encoding="utf-8"))
    runtime_config = RuntimeConfig.from_json(path)
    runtime = PipelineRuntime(runtime_config)
    registry = runtime.load_metadata_registry()
    store = _context_store(config, path, runtime)
    memory = _result_memory(config, runtime)
    translator = PipelineEnvelopeTranslator()
    executor = NativeExecutor(
        PipelineScalarTaskRunner(runtime),
        translator.fact,
    )
    processor = PipelineV2TurnProcessor(
        runtime=runtime,
        registry=registry,
        interpreter=UnifiedInterpreter(PipelineInterpretationBackend(runtime)),
        compiler=InterpretationMutationCompiler(RegistryEntityBinder(registry)),
        executor=executor,
        result_memory=memory,
    )
    service = BalanceChatService(
        store,
        processor,
        metadata=metadata_ref(registry),
        delete_result_memory=(
            memory.store.delete_session
            if memory is not None and callable(getattr(memory.store, "delete_session", None))
            else None
        ),
    )
    return create_app(
        service,
        health_check=lambda: _health(runtime, registry, store),
    )


def _health(runtime: PipelineRuntime, registry: Any, store: Any) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    try:
        validate = getattr(store, "validate", None)
        if callable(validate):
            validate()
        checks["context_store"] = {"ready": True}
    except Exception:
        checks["context_store"] = {"ready": False}
    manifest = registry.manifest
    checks["metadata"] = {
        "ready": str(getattr(manifest, "state", "")) == "ready",
        "bundle_version": manifest.bundle_version,
        "schema_version": manifest.schema_version,
    }
    try:
        inference = runtime.pipeline_runtime().inference_client.health("context")
        checks["context_model"] = {
            "ready": bool(inference.get("reachable")),
            "model": inference.get("model"),
            "structured_outputs": bool(inference.get("structured_outputs")),
            "latency_ms": int(inference.get("latency_ms") or 0),
        }
    except Exception:
        checks["context_model"] = {"ready": False}
    return {
        "status": "ok" if all(item["ready"] for item in checks.values()) else "degraded",
        "checks": checks,
    }


def _context_store(config: dict[str, Any], path: Path, runtime: PipelineRuntime):
    section = config.get("context_store") or {}
    store_type = str(section.get("type") or "").strip().lower()
    if store_type == "sqlite":
        raw = str(section.get("sqlite_path") or "").strip()
        if not raw:
            raise ValueError("context_store.sqlite_path is required")
        store_path = Path(raw)
        if not store_path.is_absolute():
            store_path = (path.parent / store_path).resolve()
        return SQLiteContextStore(store_path)
    if store_type == "postgresql":
        dsn = str(section.get("dsn") or "").strip()
        if not dsn and bool(section.get("use_pipeline_database_dsn")):
            pipeline_config = runtime.pipeline_runtime().cfg
            dsn = str((pipeline_config.get("database") or {}).get("dsn") or "").strip()
        if not dsn:
            raise ValueError("PostgreSQL context store requires an explicit DSN")
        store = PostgresContextStore(dsn, schema=str(section.get("schema") or "chat_rag"))
        store.validate()
        return store
    raise ValueError("context_store.type must be sqlite or postgresql")


def _result_memory(config: dict[str, Any], runtime: PipelineRuntime):
    section = config.get("result_memory") or {}
    if not bool(section.get("enabled", False)):
        return None
    store = runtime.pipeline_runtime().chat_service.rag_store
    if store is None:
        raise ValueError("result_memory is enabled but pipeline pgvector store is unavailable")
    return PipelineResultMemoryAdapter(
        store,
        max_chunks=int(section.get("retrieval_limit") or 5),
        max_chunk_chars=int(section.get("max_chunk_chars") or 4000),
        max_total_chars=int(section.get("max_total_chars") or 12000),
    )
