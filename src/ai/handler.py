"""Lambda handler for AI Agent service."""

from __future__ import annotations

import json
import os
from typing import Any

from ..economics.secrets import authorized
from .agent import InvestigationAgent

_agent = InvestigationAgent()


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


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
    ).get("method", "POST")
    raw_path = event.get("path") or event.get("rawPath", "/")
    path = raw_path.rstrip("/") if raw_path != "/" else "/"

    if http_method == "OPTIONS":
        return {
            "statusCode": 200,
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Methods": "POST,OPTIONS",
            },
            "body": "",
        }

    if path not in ("/assistant", "/ask"):
        return {"statusCode": 404, "body": json.dumps({"error": f"Not found: {path}"})}

    if not authorized(event, os.environ.get("CC_AUTH_SECRET_ARN") or None):
        return {
            "statusCode": 401,
            "headers": {
                "Access-Control-Allow-Origin": "*",
                "Access-Control-Allow-Headers": "*",
            },
            "body": json.dumps(
                {"error": "Unauthorized: missing or invalid x-demo-auth header"}
            ),
        }

    payload = _parse_body(event)
    question = payload.get("question", "What are the primary spend drivers?")
    screen_context = payload.get("context", {})

    chunks: list[str] = []
    for event_name, data in _agent.iter_events(question, screen_context):
        chunks.append(_sse(event_name, data))

    return {
        "statusCode": 200,
        "headers": {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "*",
        },
        "body": "".join(chunks),
    }
