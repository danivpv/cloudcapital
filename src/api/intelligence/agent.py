"""DSPy-powered, bounded investigation agent.

The agent does not receive raw rows and does not write SQL. DSPy plans one or
two typed query requests; :class:`Store` compiles and executes the SQL. The
final answer is streamed as text only, while the exact compiled SQL is emitted
for the drawer's transparency panel.
"""

from __future__ import annotations

import json
import re
import time
from typing import Iterator, Literal  # noqa: UP035

import dspy
from pydantic import BaseModel, Field, ValidationError

from ..config import Settings
from ..data.store import Store
from ..intelligence.llm import (
    Budget,
    BudgetExceeded,
    Ledger,
    LLMClient,
    UsageRecord,
)

Dimension = Literal[
    "service", "account", "region", "usage_kind", "instance_type", "commitment_scope"
]
Metric = Literal["on_demand_cost", "amortized_cost", "usage_amount"]


class QueryStep(BaseModel):
    """One allowed analytical action; values always come from the active screen context."""

    group_by: Dimension
    metric: Metric
    compare_periods: bool = False
    use_current_selection: bool = False
    top_n: int = Field(default=8, ge=1, le=12)


class InvestigationPlan(BaseModel):
    queries: list[QueryStep] = Field(min_length=1, max_length=2)
    intent: str = Field(max_length=160)


class PlanInvestigation(dspy.Signature):
    """Plan a useful FinOps investigation from the active product screen.

    Return strict JSON only. The plan may contain one or two queries. It cannot
    invent a filter value or time period: it may only use the current screen's
    selected dimension/value and current period(s). Prefer a normal breakdown
    for simple questions; use comparison only when the customer asks what
    changed or why something moved.
    """

    screen_context: str = dspy.InputField(
        desc="Current screen, metric, period(s), and optional selected row"
    )
    question: str = dspy.InputField(desc="Customer's plain-language investigation")
    plan_json: str = dspy.OutputField(
        desc="JSON: {intent: string, queries: [{group_by, metric, compare_periods, use_current_selection, top_n}]}"
    )


class DspyInvestigationAgent:
    """One DSPy planning call, at most two safe SQL queries, one text stream."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        narrator: LLMClient,
        budget: Budget | None = None,
    ) -> None:
        self._store = store
        self._narrator = narrator
        self._budget = budget or Budget(
            max_steps=3, query_max_tokens=450, prose_max_tokens=350
        )
        self._planner = dspy.Predict(PlanInvestigation)
        self._planner_lm = (
            dspy.LM(
                settings.llm_router_model,
                api_key=settings.openrouter_api_key,
                max_tokens=self._budget.query_max_tokens,
                temperature=0,
            )
            if settings.openrouter_api_key
            else None
        )

    def iter_events(self, question: str, context: dict) -> Iterator[tuple[str, dict]]:
        """Emit drawer events: status, compiled SQL, text chunks, then usage."""
        ledger = Ledger()
        started = time.perf_counter()
        try:
            yield "status", {"message": "Planning up to two bounded queries…"}
            plan = self._plan(question, context, ledger)
            period, comparison_period, selected_dimension, selected_value = (
                self._context_values(context)
            )
            result_sets: list[list[dict]] = []
            sqls: list[str] = []
            for index, step in enumerate(plan.queries, start=1):
                yield (
                    "status",
                    {"message": f"Running query {index} of {len(plan.queries)}…"},
                )
                rows, sql = self._store.investigate(
                    group_by=step.group_by,
                    metric=step.metric,
                    period=period,
                    comparison_period=comparison_period
                    if step.compare_periods
                    else None,
                    selected_dimension=selected_dimension
                    if step.use_current_selection
                    else None,
                    selected_value=selected_value
                    if step.use_current_selection
                    else None,
                    top_n=step.top_n,
                )
                result_sets.append(rows)
                sqls.append(sql)
                yield "sql", {"sql": sql, "rows": len(rows)}
            yield "status", {"message": "Writing a grounded explanation…"}
            for chunk in self._stream_answer(
                question, context, plan, result_sets, sqls, ledger
            ):
                yield "text", {"delta": chunk}
            yield (
                "meta",
                {
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                    "usage": ledger.summary(),
                    "intent": plan.intent,
                    "query_count": len(plan.queries),
                },
            )
        except (BudgetExceeded, ValidationError, ValueError) as exc:
            yield "error", {"message": str(exc)}

    def _plan(self, question: str, context: dict, ledger: Ledger) -> InvestigationPlan:
        if self._planner_lm is None:
            return self._fallback_plan(context)
        if len(ledger.records) >= self._budget.max_steps:
            raise BudgetExceeded("agent step budget reached")
        started = time.perf_counter()
        with dspy.context(lm=self._planner_lm):
            prediction = self._planner(
                screen_context=json.dumps(context), question=question
            )
        self._record_dspy_usage(ledger, started)
        raw = (
            prediction.plan_json.strip()
            .removeprefix("```json")
            .removeprefix("```")
            .removesuffix("```")
            .strip()
        )
        try:
            match = re.search(r"\{.*\}", raw, re.DOTALL)
            payload = json.loads(match.group(0) if match else raw)
            aliases = {
                "usage_type": "usage_kind",
                "product_code": "service",
                "account_id": "account",
                "commitment_key": "commitment_scope",
            }
            for step in payload.get("queries", []):
                step["group_by"] = aliases.get(
                    step.get("group_by"), step.get("group_by")
                )
            return InvestigationPlan.model_validate(payload)
        except (ValidationError, json.JSONDecodeError):
            return self._fallback_plan(context)

    def _record_dspy_usage(self, ledger: Ledger, started: float) -> None:
        """Capture DSPy's LiteLLM usage when available, safely degrading to latency only."""
        history = getattr(self._planner_lm, "history", [])
        item = history[-1] if history else {}
        usage = item.get("usage", {}) if isinstance(item, dict) else {}
        if not usage and isinstance(item, dict):
            response = item.get("response")
            usage = (
                response.get("usage", {})
                if hasattr(response, "get")
                else getattr(response, "usage", {})
            )

        def value(source, name: str) -> int | float:
            return (
                source.get(name, 0)
                if hasattr(source, "get")
                else getattr(source, name, 0)
            )

        ledger.add(
            UsageRecord(
                "dspy_plan",
                str(getattr(self._planner_lm, "model", "dspy")),
                int(value(usage, "prompt_tokens") or 0),
                int(value(usage, "completion_tokens") or 0),
                float(item.get("cost", value(usage, "cost")) or 0)
                if isinstance(item, dict)
                else 0.0,
                (time.perf_counter() - started) * 1000,
            )
        )

    @staticmethod
    def _context_values(
        context: dict,
    ) -> tuple[str, str | None, str | None, str | None]:
        period = str(context.get("period") or "2026-05")
        comparison = (
            context.get("comparison_period") if context.get("compare") else None
        )
        selected_dimension = context.get("selected_dimension")
        selected_value = context.get("selected_value")
        return (
            period,
            str(comparison) if comparison else None,
            selected_dimension,
            selected_value,
        )

    @staticmethod
    def _fallback_plan(context: dict) -> InvestigationPlan:
        selected_dimension = context.get("selected_dimension")
        next_dimension = {
            "service": "account",
            "account": "usage_kind",
            "region": "service",
            "usage_kind": "account",
            "instance_type": "account",
            "commitment_scope": "service",
        }.get(selected_dimension, context.get("group_by", "service"))
        metric = context.get("metric", "on_demand_cost")
        if metric not in ("on_demand_cost", "amortized_cost", "usage_amount"):
            metric = "on_demand_cost"
        return InvestigationPlan(
            intent="Investigate the current selection with a safe bounded query.",
            queries=[
                QueryStep(
                    group_by=next_dimension,
                    metric=metric,
                    compare_periods=bool(context.get("compare")),
                    use_current_selection=bool(
                        selected_dimension and context.get("selected_value")
                    ),
                )
            ],
        )

    def _stream_answer(
        self,
        question: str,
        context: dict,
        plan: InvestigationPlan,
        result_sets: list[list[dict]],
        sqls: list[str],
        ledger: Ledger,
    ) -> Iterator[str]:
        evidence = json.dumps(result_sets, default=str)[:8_000]
        messages = [
            {
                "role": "system",
                "content": (
                    "You are a FinOps investigation assistant. Answer only from the supplied SQL results. "
                    "Do not invent figures, do not mention hidden reasoning, and do not emit tables or SQL. "
                    "Write 2-4 direct sentences for a finance owner."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Question: {question}\nScreen context: {json.dumps(context)}\n"
                    f"Approved plan: {plan.intent}\nComputed evidence: {evidence}"
                ),
            },
        ]
        yield from self._narrator.stream("prose", messages, self._budget, ledger)
