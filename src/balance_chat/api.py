from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from .service import BalanceChatService, TurnProcessingError, _context_view, _session_view
from .store import ContextStoreError, RevisionConflict, SessionNotFound
from .contracts import ClarificationAnswer


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatTurnRequest(ApiModel):
    session_id: str
    expected_revision: int = Field(ge=0)
    message: str = Field(min_length=1)
    execute_db: bool = False
    clarification: ClarificationAnswer | None = None
    request_id: str | None = None


def create_app(
    service: BalanceChatService,
    *,
    health_check: Callable[[], dict[str, Any]] | None = None,
) -> FastAPI:
    app = FastAPI(title="AI Balances Context Chat V2", version="0.1.0")

    @app.get("/api/v2/health")
    def health() -> JSONResponse:
        payload = health_check() if health_check is not None else {
            "status": "ok",
            "checks": {"api": {"ready": True}},
        }
        return JSONResponse(
            payload,
            status_code=200 if payload.get("status") == "ok" else 503,
        )

    @app.post("/api/v2/chat/sessions", status_code=201)
    def create_session() -> dict[str, Any]:
        state = service.create_session()
        return {"status": "ok", "session": _session_view(state), "context": _context_view(state)}

    @app.get("/api/v2/chat/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, Any]:
        try:
            state = service.get_session(session_id)
            return {"status": "ok", "session": _session_view(state), "context": _context_view(state)}
        except SessionNotFound as exc:
            raise HTTPException(404, detail={"code": "session_not_found"}) from exc

    @app.delete("/api/v2/chat/sessions/{session_id}")
    def delete_session(session_id: str) -> dict[str, Any]:
        return {"status": "ok", **service.delete_session(session_id)}

    @app.post("/api/v2/chat")
    def execute_turn(request: ChatTurnRequest) -> dict[str, Any]:
        try:
            return service.execute_turn(
                request.session_id,
                expected_revision=request.expected_revision,
                message=request.message,
                execute_db=request.execute_db,
                clarification=request.clarification,
                request_id=request.request_id,
            )
        except RevisionConflict as exc:
            raise HTTPException(
                409,
                detail={
                    "code": "revision_conflict",
                    "expected_revision": exc.expected,
                    "actual_revision": exc.actual,
                },
            ) from exc
        except SessionNotFound as exc:
            raise HTTPException(404, detail={"code": "session_not_found"}) from exc
        except TurnProcessingError as exc:
            raise HTTPException(422, detail={"code": exc.code, "message": str(exc)}) from exc
        except ContextStoreError as exc:
            raise HTTPException(503, detail={"code": "context_store_unavailable"}) from exc

    ui_root = Path(__file__).resolve().parent / "ui"
    app.mount("/assets", StaticFiles(directory=ui_root), name="assets")

    @app.get("/", include_in_schema=False)
    def ui() -> FileResponse:
        return FileResponse(ui_root / "index.html")

    return app
