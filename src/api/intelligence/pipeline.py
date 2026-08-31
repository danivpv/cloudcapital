"""The ask pipeline: question in, structured answer out.

The shape is deliberate and mirrors the sezzle agent's cost discipline:

1. Route (cheap first): deterministic templates cover the questions we know
   — zero LLM. The model only sees questions the templates miss.
2. Query synthesis (bounded): the model emits a structured query spec in
   JSON over a whitelisted vocabulary. The model authors JSON, never SQL,
   and the spec is validated against the vocabulary before anything runs.
3. Execute: the query layer answers. This is the only thing touching data.
4. Explain: numbers are template-interpolated into the headline; the model
   writes prose only if configured, streamed and token-capped, over bounded
   result rows (≤8).

Structural isolation: the model sees question + bounded result rows only.
It never sees raw rows, and the headline numbers never come from the model.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Iterator  # noqa: UP035

from ..data.store import ExploreParams, Store
from ..intelligence.llm import Budget, BudgetExceeded, Ledger, LLMClient

VALID_DIMS = {
    "service",
    "account",
    "usage_kind",
    "instance_type",
    "commitment_scope",
    "region",
}
VALID_METRICS = {"on_demand_cost", "usage_amount", "amortized_cost"}
DEFAULT_SPEC: dict = {
    "group_by": ["service"],
    "metrics": ["on_demand_cost"],
    "window": None,
    "top_n": 8,
}

_ROUTE_SCHEMA = {
    "name": "route",
    "schema": {
        "type": "object",
        "properties": {
            "route": {
                "type": "string",
                "enum": ["breakdown", "the_split", "headline", "what_changed"],
            },
            "group_by": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(VALID_DIMS)},
            },
            "metrics": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(VALID_METRICS)},
            },
            "window": {
                "anyOf": [
                    {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 2,
                    },
                    {"type": "null"},
                ]
            },
            "top_n": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        "required": ["route"],
        "additionalProperties": False,
    },
}

_SYSTEM_PROMPT = (
    "You answer questions about a cloud cost-and-usage dataset and a Compute Savings Plan "
    "proposal. You NEVER answer with numbers you weren't given. Choose the route: "
    "'breakdown' for a slice of spend (pick group_by from service, account, usage_kind, "
    "instance_type, commitment_scope, region), 'the_split' for the customer/reserve/profit "
    "split, 'headline' for the overall savings headline, 'what_changed' for month-over-month "
    "changes (always group_by=['service'])."
)


@dataclass
class AskStep:
    """One stage of the pipeline, emitting an SSE-ready event."""

    event: str
    data: dict


@dataclass
class AskResult:
    steps: list[AskStep] = field(default_factory=list)
    answer: dict = field(default_factory=dict)

    def add(self, event: str, data: dict) -> None:
        self.steps.append(AskStep(event, data))


# ---------------------------------------------------------------------------
# Deterministic templates (tier 0: no LLM at all)
# ---------------------------------------------------------------------------

_TEMPLATES: list[tuple[re.Pattern[str], str, dict]] = [
    (
        re.compile(r"what changed|change|moved", re.I),
        "what_changed",
        {
            "group_by": ["service"],
            "metrics": ["on_demand_cost"],
            "window": None,
            "top_n": 8,
        },
    ),
    (re.compile(r"split|keep|profit|reserve", re.I), "the_split", {}),
    (
        re.compile(r"driving|drive|biggest|largest|top", re.I),
        "what_drives",
        {
            "group_by": ["service"],
            "metrics": ["on_demand_cost"],
            "window": None,
            "top_n": 8,
        },
    ),
    (re.compile(r"saving|save|discount|guarantee", re.I), "headline", {}),
]


class AskPipeline:
    def __init__(
        self, store: Store, llm: LLMClient, economics, budget: Budget | None = None
    ):
        self._store = store
        self._llm = llm
        self._economics = economics
        self._budget = budget or Budget()

    # -- routing -----------------------------------------------------------

    def route(self, question: str) -> tuple[str, dict | None]:
        """Deterministic templates first; unknown questions go to the model."""
        for pattern, name, params in _TEMPLATES:
            if pattern.search(question):
                return name, params
        return "novel", None

    def _llm_route(self, question: str, ledger: Ledger) -> tuple[str, dict]:
        """Ask the model to route + shape the query. Degrades to a default."""
        try:
            content = self._llm.complete(
                "route",
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": question},
                ],
                self._budget,
                ledger,
                response_schema=_ROUTE_SCHEMA,
            )
        except BudgetExceeded:
            return "breakdown", dict(DEFAULT_SPEC)
        if content is None:
            return "breakdown", dict(DEFAULT_SPEC)
        try:
            spec = json.loads(content)
        except json.JSONDecodeError:
            return "breakdown", dict(DEFAULT_SPEC)
        route = spec.get("route", "breakdown")
        if route not in ("breakdown", "the_split", "headline", "what_changed"):
            route = "breakdown"
        params = dict(DEFAULT_SPEC)
        gb = spec.get("group_by") or []
        if gb and all(d in VALID_DIMS for d in gb):
            params["group_by"] = gb
        mx = spec.get("metrics") or []
        if mx and all(m in VALID_METRICS for m in mx):
            params["metrics"] = mx
        if spec.get("window"):
            params["window"] = tuple(spec["window"][:2])
        if isinstance(spec.get("top_n"), int):
            params["top_n"] = min(max(spec["top_n"], 1), 20)
        return route, params

    # -- execution ---------------------------------------------------------

    def run(self, question: str) -> AskResult:
        result = AskResult()
        ledger = Ledger()
        started = time.perf_counter()
        try:
            name, params = self.route(question)
            llm_steps = 0
            if name == "novel":
                name, params = self._llm_route(question, ledger)
                llm_steps = 1
            result.add("query_spec", {"route": name, "params": params})
            self._execute_route(name, params, question, result, ledger)
            result.answer["_ledger"] = ledger
            result.answer["_started"] = started
            result.answer["_pipeline_steps"] = 1 + llm_steps
        except BudgetExceeded as exc:
            result.add(
                "meta",
                {
                    "steps": 1,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "usage": ledger.summary(),
                    "degraded": str(exc),
                },
            )
        return result

    def _execute_route(
        self,
        name: str,
        params: dict | None,
        question: str,
        result: AskResult,
        ledger: Ledger,
    ) -> None:
        if name == "the_split":
            econ = self._economics.economics()
            result.add("chart_data", {"kind": "split", "data": econ})
            result.add("headline", self._split_headline(econ))
            result.answer["prose_rows"] = []
            result.answer["route"] = name
            return

        if name == "headline":
            econ = self._economics.economics()
            result.add("chart_data", {"kind": "headline", "data": econ})
            result.add("headline", self._headline_text(econ))
            result.answer["prose_rows"] = []
            result.answer["route"] = name
            return

        if name == "what_changed":
            params = params or {}
            gb = params.get("group_by") or []
            dim = gb[0] if gb and gb[0] in VALID_DIMS else "service"
            window = params.get("window")
            complete = self._store.complete_periods()
            if window and len(window) == 2:
                wa, wb = window[0][:7], window[1][:7]
                if wa in complete and wb in complete:
                    window_a, window_b = wa, wb
                else:
                    window_a, window_b = complete[-2], complete[-1]
            elif len(complete) >= 2:
                window_a, window_b = complete[-2], complete[-1]
            else:
                window_a, window_b = None, None
            if window_a is not None and window_b is not None:
                rows = self._store.delta(
                    group_by=dim,
                    metric="on_demand_cost",
                    window_a=(window_a, window_a),
                    window_b=(window_b, window_b),
                    top_n=8,
                )
                result.add(
                    "chart_data",
                    {
                        "kind": "delta",
                        "data": rows,
                        "window_a": window_a,
                        "window_b": window_b,
                    },
                )
                result.add(
                    "headline", self._delta_headline(rows, dim, window_a, window_b)
                )
                result.answer["prose_rows"] = _rows_from_delta(rows, dim)
                result.answer["route"] = "what_changed"
                return
            # Fall through to a plain ranking when only one complete month exists.

        params = params or dict(DEFAULT_SPEC)
        try:
            rows = self._store.explore(ExploreParams(**params))
        except (ValueError, NotImplementedError):
            params = dict(DEFAULT_SPEC)
            rows = self._store.explore(ExploreParams(**params))
        result.add("chart_data", {"kind": "bars", "data": rows})
        result.add("headline", self._table_headline(rows, name))
        result.answer["prose_rows"] = rows
        result.answer["route"] = name

    def _stream_prose(
        self, question: str, rows: list[dict], route: str, ledger: Ledger
    ) -> Iterator[str]:
        """Yield a short model-written explanation over the bounded rows."""
        if not rows:
            return
        table = _compact_table(rows[:8])
        try:
            chunks = self._llm.stream(
                "prose",
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"Question: {question}\nQuery result ({route}):\n{table}\n"
                            "Explain in 2-3 sentences. Use ONLY numbers present in the table."
                        ),
                    },
                ],
                self._budget,
                ledger,
            )
            for chunk in chunks:
                yield chunk
        except BudgetExceeded:
            return

    # -- headlines (template-interpolated, no model) ------------------------

    @staticmethod
    def _split_headline(econ: dict) -> dict:
        return {
            "text": (
                f"Of ${econ['net_savings']:,.0f} in net savings, you keep "
                f"${econ['customer_savings']:,.0f}. "
                f"${econ['reserve']:,.0f} is held as risk reserve and "
                f"${econ['profit']:,.0f} is our profit."
            ),
            "numbers": {
                "customer": econ["customer_savings"],
                "reserve": econ["reserve"],
                "profit": econ["profit"],
                "net": econ["net_savings"],
            },
        }

    @staticmethod
    def _headline_text(econ: dict) -> dict:
        return {
            "text": (
                f"The proposal saves a guaranteed {econ['customer_savings_rate'] * 100:.1f}% "
                f"of covered on-demand spend, or ${econ['customer_savings']:,.0f} across the window."
            ),
            "numbers": {
                "rate": econ["customer_savings_rate"],
                "customer": econ["customer_savings"],
            },
        }

    @staticmethod
    def _table_headline(rows: list[dict], route: str) -> dict:
        if not rows:
            return {"text": "No data matched.", "numbers": {}}
        top = rows[0]
        service = top.get("service", top.get("usage_kind", "the top line"))
        if route == "what_changed":
            return {"text": f"The largest spend today is {service}.", "numbers": {}}
        return {
            "text": f"{service} leads at ${top.get('on_demand_cost', 0):,.0f}.",
            "numbers": {},
        }

    @staticmethod
    def _delta_headline(
        rows: list[dict], dim: str, window_a: str, window_b: str
    ) -> dict:
        if not rows:
            return {
                "text": f"No change between {window_a} and {window_b}.",
                "numbers": {},
            }
        top = rows[0]
        name = top.get(dim, "the top line")
        direction = "rose" if top["delta"] >= 0 else "fell"
        pct = top["pct"]
        pct_s = f" ({pct:+.0f}%)" if pct is not None else ""
        return {
            "text": (
                f"{name} {direction} ${abs(top['delta']):,.0f}{pct_s} "
                f"from {window_a} to {window_b}."
            ),
            "numbers": {"delta": top["delta"]},
        }

    # -- streaming interface --------------------------------------------------

    def iter_events(self, question: str) -> Iterator[tuple[str, str]]:
        """Yield SSE events as work completes; prose is genuinely streamed."""
        result = self.run(question)
        for step in result.steps:
            yield step.event, json.dumps(step.data)
        ledger: Ledger | None = result.answer.get("_ledger")
        if ledger is None:
            return
        for chunk in self._stream_prose(
            question,
            result.answer.get("prose_rows", []),
            result.answer.get("route", "breakdown"),
            ledger,
        ):
            yield "prose", json.dumps({"delta": chunk})
        yield (
            "meta",
            json.dumps(
                {
                    "steps": result.answer.get("_pipeline_steps", 1)
                    + len(ledger.records),
                    "latency_ms": round(
                        (time.perf_counter() - result.answer["_started"]) * 1000, 1
                    ),
                    "usage": ledger.summary(),
                }
            ),
        )


def _compact_table(rows: list[dict], max_cells: int = 40) -> str:
    keys = [k for k in rows[0].keys() if k != "billing_period"]
    lines = [" | ".join(keys)]
    for r in rows:
        cells = []
        for k in keys:
            v = r.get(k)
            cells.append(f"{v:,.0f}" if isinstance(v, (int, float)) else str(v))
        lines.append(" | ".join(cells))
    return "\n".join(lines)


def _rows_from_delta(rows: list[dict], dim: str) -> list[dict]:
    return [
        {
            dim: r.get(dim, "?"),
            "value_a": r["value_a"],
            "value_b": r["value_b"],
            "delta": r["delta"],
        }
        for r in rows
    ]


def build_default_pipeline(store: Store, economics, llm: LLMClient) -> AskPipeline:
    return AskPipeline(store, llm, economics)
