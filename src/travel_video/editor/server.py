from __future__ import annotations

import json
from pathlib import Path
from typing import Any, AsyncIterator, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from .agent import AgentOrchestrator, CodexBackend, DemoBackend, new_session_id
from .catalog import TimelineCatalog
from .contracts import ToolCatalog, project_root
from .store import EditStore, RevisionConflictError
from .tools import ToolGateway


class CreateEditRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    brief: str = Field(min_length=1, max_length=2000)
    target_duration: float | None = Field(default=None, ge=5, le=7200)


class ApplyOperationsRequest(BaseModel):
    expected_revision_id: str = Field(min_length=1, max_length=80)
    summary: str = Field(min_length=1, max_length=240)
    operations: list[dict[str, Any]] = Field(min_length=1, max_length=100)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: str | None = Field(default=None, max_length=80)
    context: dict[str, Any] = Field(default_factory=dict)


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return (
        f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, RevisionConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail="internal editor service error")


def create_editor_app(
    manifest_path: Path,
    state_dir: Path,
    *,
    agent_backend: Literal["auto", "codex", "demo"] = "auto",
    codex_model: str = "gpt-5.6-terra",
) -> FastAPI:
    manifest_path = manifest_path.expanduser().resolve()
    state_dir = state_dir.expanduser().resolve()
    catalog = TimelineCatalog(manifest_path)
    store = EditStore(state_dir / "editor.sqlite3")
    tool_catalog = ToolCatalog.load()
    gateway = ToolGateway(catalog, store, tool_catalog)
    backend_error: str | None = None
    if agent_backend == "demo":
        backend = DemoBackend()
    else:
        try:
            backend = CodexBackend(
                store=store,
                tools=tool_catalog,
                project_root=project_root(),
                model=codex_model,
            )
        except RuntimeError as exc:
            if agent_backend == "codex":
                raise
            backend_error = str(exc)
            backend = DemoBackend()
    orchestrator = AgentOrchestrator(backend, gateway, store)

    app = FastAPI(
        title="Travel Video Editor API",
        version="0.1.0",
        description="Contract-first local editor API and Codex SDK agent gateway.",
    )
    app.state.catalog = catalog
    app.state.store = store
    app.state.gateway = gateway
    app.state.orchestrator = orchestrator

    @app.get("/health", tags=["system"])
    async def health() -> dict[str, Any]:
        store.ping()
        return {
            "status": "ok",
            "backend": backend.name,
            "backend_fallback_reason": backend_error,
            "catalog": {"assets": catalog.asset_count, "scenes": catalog.scene_count},
            "database": str(store.path),
        }

    @app.get("/api/project", tags=["project"])
    async def project() -> dict[str, Any]:
        return {
            "title": catalog.title,
            "asset_count": catalog.asset_count,
            "scene_count": catalog.scene_count,
            "agent_backend": backend.name,
            "tool_contract": "editor-tool-catalog/v1",
        }

    @app.get("/api/contracts/tools", tags=["contracts"])
    async def contract_tools() -> dict[str, Any]:
        return tool_catalog.public_document()

    @app.get("/api/assets", tags=["catalog"])
    async def assets(
        capture_date: str | None = None,
        limit: int = Query(default=30, ge=1, le=100),
    ) -> dict[str, Any]:
        try:
            items = catalog.list_assets(capture_date=capture_date, limit=limit)
            return {"items": items, "count": len(items)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/scenes/search", tags=["catalog"])
    async def search_scenes(
        query: str = "",
        asset_id: list[str] = Query(default=[]),
        highlight_only: bool = False,
        limit: int = Query(default=12, ge=1, le=30),
    ) -> dict[str, Any]:
        try:
            items = catalog.search_scenes(
                query=query,
                asset_ids=asset_id or None,
                highlight_only=highlight_only,
                limit=limit,
            )
            return {"items": items, "count": len(items)}
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/scenes/{scene_id:path}", tags=["catalog"])
    async def scene_evidence(scene_id: str) -> dict[str, Any]:
        try:
            return catalog.scene_evidence(scene_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/edits", status_code=201, tags=["edits"])
    async def create_edit(payload: CreateEditRequest) -> dict[str, Any]:
        try:
            return store.create_edit(
                title=payload.title,
                brief=payload.brief,
                target_duration=payload.target_duration,
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.get("/api/edits/{edit_id}", tags=["edits"])
    async def get_edit(edit_id: str, revision_id: str | None = None) -> dict[str, Any]:
        try:
            return store.get_edit(edit_id, revision_id)
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/edits/{edit_id}/operations", tags=["edits"])
    async def apply_operations(
        edit_id: str, payload: ApplyOperationsRequest
    ) -> dict[str, Any]:
        try:
            return store.apply_operations(
                catalog=catalog,
                edit_id=edit_id,
                expected_revision_id=payload.expected_revision_id,
                summary=payload.summary,
                operations=payload.operations,
                created_by="user:api",
            )
        except Exception as exc:
            raise _http_error(exc) from exc

    @app.post("/api/agent/chat", tags=["agent"])
    async def agent_chat(payload: ChatRequest, request: Request) -> StreamingResponse:
        session_id = payload.session_id or new_session_id()
        context = dict(payload.context)
        if len(json.dumps(context, ensure_ascii=False)) > 16_000:
            raise HTTPException(status_code=413, detail="chat context exceeds 16 KB")

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for item in orchestrator.stream_turn(
                    session_id=session_id,
                    message=payload.message.strip(),
                    context=context,
                ):
                    if await request.is_disconnected():
                        return
                    yield _sse(item.event, item.data)
            except Exception as exc:
                yield _sse(
                    "error",
                    {
                        "message": str(exc)[:500],
                        "code": type(exc).__name__,
                        "retryable": False,
                    },
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "X-Agent-Backend": backend.name,
            },
        )

    media_dir = catalog.library_dir / "media"

    @app.get("/media/{filename}", include_in_schema=False)
    async def media(filename: str) -> FileResponse:
        if Path(filename).name != filename:
            raise HTTPException(status_code=404)
        candidate = media_dir / filename
        if not candidate.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(candidate, media_type="video/mp4")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(catalog.library_dir / "index.html", media_type="text/html")

    return app


def serve_editor(
    manifest_path: Path,
    state_dir: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    agent_backend: Literal["auto", "codex", "demo"] = "auto",
    codex_model: str = "gpt-5.6-terra",
) -> None:
    import uvicorn

    app = create_editor_app(
        manifest_path,
        state_dir,
        agent_backend=agent_backend,
        codex_model=codex_model,
    )
    uvicorn.run(app, host=host, port=port, log_level="info")
