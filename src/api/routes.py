"""HTTP API: the surface the frontend consumes.

Endpoints map one-to-one onto the plan:

  GET  /health       liveness
  GET  /proposal     the proposed commitment (proxied from the economics service)
  GET  /economics    headline economics + split (proxied)
  POST /explore      slice the firehose via the query layer
  POST /ask          SSE stream: query_spec -> chart_data -> headline -> meta
  GET  /narration    the tier-0 narration feed (no LLM involved)
  GET  /metrics      LLM usage ledger summary
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .data.store import ALL_METRICS, DIMENSIONS, ExploreParams

router = APIRouter()


class ExploreBody(BaseModel):
    group_by: list[str] = Field(default_factory=lambda: ["service"])
    metrics: list[str] = Field(default_factory=lambda: ["on_demand_cost"])
    window: tuple[str, str] | None = None
    filters: dict[str, list[str]] | None = None
    top_n: int = 50
    order_by: str | None = None
    granularity: str = "month"


class SliceBody(BaseModel):
    group_by: str = "service"
    metric: str = "on_demand_cost"
    window: tuple[str, str] | None = None
    top_n: int = 12


class AskBody(BaseModel):
    question: str


class AssistantBody(BaseModel):
    question: str
    context: dict[str, Any] = Field(default_factory=dict)


class DeltaBody(BaseModel):
    group_by: str = "service"
    metric: str = "on_demand_cost"
    window_a: tuple[str, str]
    window_b: tuple[str, str]
    top_n: int = 12


class DrillBody(BaseModel):
    group_by: str = "service"
    value: str
    window: tuple[str, str] | None = None
    limit: int = 30


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.get("/health")
def health(request: Request):
    return {"status": "ok", "store": request.app.state.store.con is not None}


@router.get("/impact-status")
def impact_status(request: Request):
    """Whether the savings dice are materialized (impact fetch done)."""
    return {"ready": request.app.state.store.impact_ready()}


@router.post("/warm")
def warm(request: Request):
    """Materialize /impact once, in the background (idempotent)."""
    store = request.app.state.store
    economics = request.app.state.economics
    if not store.impact_ready():
        if getattr(request.app.state, "_warming", False):
            return {"warming": True}
        request.app.state._warming = True

        def _do() -> None:
            try:
                economics.fetch_impact_dice()
            finally:
                request.app.state._warming = False

        import threading

        threading.Thread(target=_do, name="impact-warm", daemon=True).start()
        return {"warming": True}
    return {"warming": False, "ready": True}


@router.get("/proposal")
def proposal(request: Request):
    return request.app.state.economics.proposal()


@router.get("/economics")
def economics(request: Request):
    return request.app.state.economics.economics()


@router.post("/explore")
def explore(body: ExploreBody, request: Request):
    params = ExploreParams(**body.model_dump())
    rows = request.app.state.store.explore(params)
    return {"rows": rows, "params": asdict(params)}


@router.post("/slice")
def slice_spend(body: SliceBody, request: Request):
    """The explorer's simple default: one grouped metric for one window."""
    rows = request.app.state.store.slice(
        body.group_by, body.metric, body.window, body.top_n
    )
    return {
        "rows": rows,
        "group_by": body.group_by,
        "metric": body.metric,
        "window": body.window,
    }


@router.get("/vocab")
def vocab(request: Request):
    """The whitelisted vocabulary the explorer (and the LLM) may use."""
    return {
        "dims": DIMENSIONS,
        "metrics": ALL_METRICS,
        "periods": request.app.state.store.periods(),
        "complete_periods": request.app.state.store.complete_periods(),
    }


@router.post("/delta")
def delta(body: DeltaBody, request: Request):
    """Two-window comparison: per-dimension sums joined, with delta and pct."""
    rows = request.app.state.store.delta(
        body.group_by, body.metric, body.window_a, body.window_b, body.top_n
    )
    return {
        "rows": rows,
        "group_by": body.group_by,
        "metric": body.metric,
        "window_a": body.window_a,
        "window_b": body.window_b,
    }


@router.post("/drill")
def drill(body: DrillBody, request: Request):
    """The bounded raw rows behind one aggregated value."""
    rows = request.app.state.store.drill(
        body.group_by, body.value, body.window, body.limit
    )
    return {"rows": rows, "group_by": body.group_by, "value": body.value}


@router.post("/ask")
def ask(body: AskBody, request: Request):
    pipeline = request.app.state.pipeline

    def gen():
        for event, data in pipeline.iter_events(body.question):
            yield _sse(event, json.loads(data))

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.post("/assistant")
def assistant(body: AssistantBody, request: Request):
    """DSPy-planned, bounded investigation with text and transparent SQL only."""
    agent = request.app.state.agent

    def gen():
        for event, data in agent.iter_events(body.question, body.context):
            yield _sse(event, data)

    return StreamingResponse(gen(), media_type="text/event-stream")


@router.get("/narration")
def narration(request: Request):
    """Tier-0 feed: deterministic deltas over the dice. No LLM, no rows."""
    store = request.app.state.store
    rows = store.explore(
        ExploreParams(group_by=["service"], metrics=["on_demand_cost"], top_n=50)
    )
    return {"items": _narrate(rows)}


def _narrate(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    total = sum(r["on_demand_cost"] for r in rows)
    top = rows[0]
    return [
        {
            "title": "Spend by service",
            "body": (
                f"{top['service']} is the largest service at "
                f"${top['on_demand_cost']:,.0f} ({top['on_demand_cost'] / total * 100:.0f}% "
                f"of total on-demand cost)."
            ),
        },
        {
            "title": "Long tail",
            "body": f"{len(rows)} services account for ${total:,.0f} of on-demand spend.",
        },
    ]
