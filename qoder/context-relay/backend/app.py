from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from powercontext.client import ClientError, PowerContextClient
from powercontext.http import (
    CreateScopeRequest,
    PrepareContextRequest,
    RememberMemoryRequest,
    SearchMemoryRequest,
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


class CheckpointRequest(BaseModel):
    project: str = Field(min_length=1, max_length=120)
    progress: str = Field(min_length=1, max_length=2000)
    decision: str = Field(default="", max_length=2000)
    blocker: str = Field(default="", max_length=2000)
    next_step: str = Field(min_length=1, max_length=2000)


class ResumeRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.client = PowerContextClient(
        os.getenv("POWERCONTEXT_CLIENT_SERVER_URL", "http://127.0.0.1:8000"),
        token=os.getenv("POWERCONTEXT_CLIENT_API_TOKEN"),
    )
    app.state.scope_id = os.getenv("CONTEXT_RELAY_SCOPE_ID")
    app.state.scope_lock = asyncio.Lock()
    yield
    await app.state.client.aclose()


app = FastAPI(title="ContextRelay", version="1.0.0", lifespan=lifespan)


async def get_scope_id() -> str:
    if app.state.scope_id:
        return app.state.scope_id

    async with app.state.scope_lock:
        if app.state.scope_id:
            return app.state.scope_id
        scope = await app.state.client.create_scope(
            CreateScopeRequest(
                title="ContextRelay research workspace",
                summary="Research progress, decisions, blockers, and next actions captured by ContextRelay.",
                idempotency_key=os.getenv("CONTEXT_RELAY_WORKSPACE_KEY", "context-relay-default-workspace"),
            )
        )
        app.state.scope_id = scope.scope_id
        return scope.scope_id


def memory_items(checkpoint: CheckpointRequest) -> list[tuple[str, str]]:
    prefix = f"项目「{checkpoint.project}」"
    items = [
        ("progress", f"{prefix}进展：{checkpoint.progress}"),
        ("next_step", f"{prefix}下一步：{checkpoint.next_step}"),
    ]
    if checkpoint.decision.strip():
        items.append(("decision", f"{prefix}决策：{checkpoint.decision.strip()}"))
    if checkpoint.blocker.strip():
        items.append(("blocker", f"{prefix}阻塞：{checkpoint.blocker.strip()}"))
    return items


@app.get("/api/health")
async def health() -> dict[str, str]:
    try:
        scope_id = await get_scope_id()
    except (ClientError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="PowerContext is unavailable") from exc
    return {"status": "ready", "scope_id": scope_id}


@app.post("/api/checkpoints")
async def save_checkpoint(checkpoint: CheckpointRequest) -> dict[str, object]:
    try:
        scope_id = await get_scope_id()
        entries = []
        for kind, text in memory_items(checkpoint):
            result = await app.state.client.remember_memory(
                RememberMemoryRequest(
                    scope_id=scope_id,
                    kind=kind,
                    text=text,
                    reason="ContextRelay 用户确认的工作检查点",
                )
            )
            if result.entry is not None:
                entries.append(
                    {
                        "entry_id": result.entry.citation.entry_id,
                        "kind": result.entry.kind,
                        "text": result.entry.text,
                    }
                )
    except (ClientError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="PowerContext memory write failed") from exc

    return {"scope_id": scope_id, "saved": entries}


@app.post("/api/resume")
async def resume_work(request: ResumeRequest) -> dict[str, object]:
    try:
        scope_id = await get_scope_id()
        search, prepared = await asyncio.gather(
            app.state.client.search_memory(
                SearchMemoryRequest(scope_id=scope_id, query=request.query, mode="fts", limit=8)
            ),
            app.state.client.prepare_context(
                PrepareContextRequest(scope_id=scope_id, query=request.query, max_bytes=6000)
            ),
        )
    except (ClientError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="PowerContext memory retrieval failed") from exc

    return {
        "scope_id": scope_id,
        "status": prepared.status,
        "context": prepared.content,
        "context_bytes": prepared.content_bytes,
        "hits": [
            {
                "entry_id": hit.citation.entry_id,
                "text": hit.text,
                "score": hit.score,
                "matched_by": [method.value for method in hit.matched_by],
            }
            for hit in search.hits
        ],
    }


app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
