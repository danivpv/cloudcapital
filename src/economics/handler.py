"""Lambda handler for Economics & Data service."""

from __future__ import annotations

import json
import os
from typing import Any

from .domain import (
    DEFAULT_PROPOSAL,
    DIM_SQL,
    Proposal,
    economics,
    get_connection,
    impact_dice,
)
from .secrets import authorized
from .store import Store

_store = Store()


def _json_response(status_code: int, body: Any) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        },
        "body": json.dumps(body, default=str),
    }


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    body = event.get("body")
    if not body:
        return {}
    if isinstance(body, dict):
        return body
    try:
        return json.loads(body)
    except Exception:
        return {}


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    http_method = event.get("httpMethod") or event.get("requestContext", {}).get(
        "http", {}
    ).get("method", "GET")
    raw_path = event.get("path") or event.get("rawPath", "/")
    path = raw_path.rstrip("/") if raw_path != "/" else "/"

    if http_method == "OPTIONS":
        return _json_response(200, {"status": "ok"})

    if not authorized(event, os.environ.get("CC_AUTH_SECRET_ARN") or None):
        return _json_response(
            401, {"error": "Unauthorized: missing or invalid x-demo-auth header"}
        )

    if path == "/health":
        return _json_response(200, {"status": "ok", "ready": _store.impact_ready()})

    if path in ("/proposal", "/econ/proposal"):
        con = get_connection()
        econ = economics(con, DEFAULT_PROPOSAL)
        return _json_response(
            200,
            {
                "instrument": "compute_savings_plan",
                "scope": DEFAULT_PROPOSAL.scope,
                "commitment_per_hour": DEFAULT_PROPOSAL.commitment_per_hour,
                "term_months": DEFAULT_PROPOSAL.term_months,
                "payment_option": DEFAULT_PROPOSAL.payment_option,
                "guaranteed_savings_rate": econ["customer_savings_rate"],
            },
        )

    if path in ("/economics", "/econ/economics"):
        body = _parse_body(event)
        p = Proposal(
            scope=body.get("scope", DEFAULT_PROPOSAL.scope),
            commitment_per_hour=float(
                body.get("commitment_per_hour", DEFAULT_PROPOSAL.commitment_per_hour)
            ),
            term_months=int(body.get("term_months", DEFAULT_PROPOSAL.term_months)),
            payment_option=body.get("payment_option", DEFAULT_PROPOSAL.payment_option),
        )
        con = get_connection()
        return _json_response(200, economics(con, p))

    if path == "/econ/impact-dice":
        body = _parse_body(event)
        p = Proposal(
            scope=body.get("scope", DEFAULT_PROPOSAL.scope),
            commitment_per_hour=float(
                body.get("commitment_per_hour", DEFAULT_PROPOSAL.commitment_per_hour)
            ),
            term_months=int(body.get("term_months", DEFAULT_PROPOSAL.term_months)),
            payment_option=body.get("payment_option", DEFAULT_PROPOSAL.payment_option),
        )
        dims = body.get("dims")
        con = get_connection()
        return _json_response(
            200,
            {"dims": dims or list(DIM_SQL), "dice": impact_dice(con, p, dims)},
        )

    if path == "/vocab":
        return _json_response(200, _store.vocab())

    if path == "/slice":
        body = _parse_body(event)
        group_by = body.get("group_by", "service")
        metric = body.get("metric", "on_demand_cost")
        window = tuple(body["window"]) if body.get("window") else None
        top_n = int(body.get("top_n", 12))
        rows = _store.slice(group_by, metric, window, top_n)
        return _json_response(
            200,
            {"rows": rows, "group_by": group_by, "metric": metric, "window": window},
        )

    if path == "/delta":
        body = _parse_body(event)
        group_by = body.get("group_by", "service")
        metric = body.get("metric", "on_demand_cost")
        window_a = tuple(body["window_a"])
        window_b = tuple(body["window_b"])
        top_n = int(body.get("top_n", 12))
        rows = _store.delta(group_by, metric, window_a, window_b, top_n)
        return _json_response(
            200,
            {
                "rows": rows,
                "group_by": group_by,
                "metric": metric,
                "window_a": window_a,
                "window_b": window_b,
            },
        )

    if path == "/explore":
        body = _parse_body(event)
        group_by = body.get("group_by", ["service"])
        metrics = body.get("metrics", ["on_demand_cost"])
        window = tuple(body["window"]) if body.get("window") else None
        top_n = int(body.get("top_n", 50))
        rows = _store.explore(group_by, metrics, window, top_n)
        return _json_response(200, {"rows": rows, "params": body})

    if path == "/drill":
        body = _parse_body(event)
        group_by = body.get("group_by", "service")
        value = body.get("value", "")
        window = tuple(body["window"]) if body.get("window") else None
        limit = int(body.get("limit", 30))
        rows = _store.drill(group_by, value, window, limit)
        return _json_response(200, {"rows": rows, "group_by": group_by, "value": value})

    if path == "/impact-status":
        return _json_response(200, {"ready": _store.impact_ready()})

    if path == "/warm":
        return _json_response(200, {"warming": False, "ready": _store.impact_ready()})

    if path == "/narration":
        rows = _store.slice("service", "on_demand_cost", top_n=50)
        total = sum(r["value"] for r in rows) if rows else 0
        top = rows[0] if rows else {"service": "N/A", "value": 0}
        items = [
            {
                "title": "Spend by service",
                "body": (
                    f"{top['service']} is the largest service at ${top['value']:,.0f} "
                    f"({top['value'] / total * 100:.0f}% of total spend)."
                    if total
                    else "No data."
                ),
            },
            {
                "title": "Long tail",
                "body": f"{len(rows)} services account for ${total:,.0f} of compute spend.",
            },
        ]
        return _json_response(200, {"items": items})

    return _json_response(404, {"error": f"Endpoint not found: {path}"})
