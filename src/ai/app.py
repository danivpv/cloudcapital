"""FastAPI app for the AI agent, served by the Lambda Web Adapter.

True response streaming: the adapter wraps uvicorn's byte stream in the
InvokeWithResponseStream wire format (JSON metadata + 8-null-byte delimiter +
chunked body) that API Gateway STREAM integrations require — something the
Python Runtime Interface Client cannot produce natively (streaming is
Node.js-only on managed runtimes).

Runs identically outside Lambda (plain uvicorn), which keeps `sam local
start-api` and direct docker testing working in buffered mode.
"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from ..economics.secrets import authorized
from .agent import InvestigationAgent

app = FastAPI(title="CloudCapital AI")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

_agent: InvestigationAgent | None = None


def _agent_instance() -> InvestigationAgent:
    global _agent
    _agent = _agent or InvestigationAgent()
    return _agent


def _sse(event_name: str, data: dict[str, Any]) -> str:
    return f"event: {event_name}\ndata: {json.dumps(data, default=str)}\n\n"


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "ai"}


@app.post("/ask")
@app.post("/assistant")
async def ask(request: Request) -> Response:
    if not authorized(_proxy_event(request), os.environ.get("CC_AUTH_SECRET_ARN") or None):
        return Response(
            content=json.dumps(
                {"error": "Unauthorized: missing or invalid x-demo-auth header"}
            ),
            status_code=401,
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        body = {}
    question = str(body.get("question", ""))[:600]
    context = body.get("context") or {}
    if not question:
        return Response(
            content=json.dumps({"error": "Missing 'question' in body"}),
            status_code=400,
            media_type="application/json",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    agent = _agent_instance()

    async def stream() -> Any:
        for event_name, data in agent.iter_events(question, context):
            yield _sse(event_name, data)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


def _proxy_event(request: Request) -> dict[str, Any]:
    """Rebuild the subset of the API Gateway event the auth check needs."""
    return {"headers": dict(request.headers)}
