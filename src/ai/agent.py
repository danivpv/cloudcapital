"""DSPy-powered FinOps investigation agent.

Plans 1-2 strictly typed queries over precomputed analytical dice, executes them,
and streams a grounded prose explanation.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Iterator, Literal

import dspy
from pydantic import BaseModel, Field, ValidationError

from ..economics.secrets import resolve_secret
from ..economics.store import Store

Dimension = Literal[
    "service", "account", "region", "usage_kind", "instance_type", "commitment_scope"
]
Metric = Literal["on_demand_cost", "amortized_cost", "usage_amount"]


class QueryStep(BaseModel):
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
    selected dimension/value and current period(s).
    """

    screen_context: str = dspy.InputField(
        desc="Current screen, metric, period(s), and optional selected row"
    )
    question: str = dspy.InputField(desc="Customer's plain-language question")
    plan_json: str = dspy.OutputField(
        desc="JSON: {intent: string, queries: [{group_by, metric, compare_periods, use_current_selection, top_n}]}"
    )


class InvestigationAgent:
    def __init__(self, store: Store | None = None) -> None:
        self.store = store or Store()
        self.api_key = os.environ.get("CC_OPENROUTER_API_KEY") or resolve_secret(
            os.environ.get("CC_OPENROUTER_SECRET_ARN"),
            "api_key",
            "CC_OPENROUTER_API_KEY",
        )
        self.model_name = os.environ.get(
            "CC_LLM_MODEL", "openrouter/google/gemini-3.5-flash"
        )
        self.planner = dspy.Predict(PlanInvestigation)
        self.planner_lm = (
            dspy.LM(
                self.model_name,
                api_key=self.api_key,
                max_tokens=600,
                temperature=0,
                num_retries=0,
            )
            if self.api_key
            else None
        )

    def iter_events(
        self, question: str, context: dict[str, Any]
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        started = time.perf_counter()
        yield "status", {"message": "Planning investigation..."}

        plan = self._plan(question, context)
        period = str(context.get("period") or "2026-05")
        comparison = (
            str(context.get("comparison_period")) if context.get("compare") else None
        )

        result_sets: list[list[dict[str, Any]]] = []
        sqls: list[str] = []
        for index, step in enumerate(plan.queries, start=1):
            yield (
                "status",
                {"message": f"Running query {index} of {len(plan.queries)}..."},
            )
            rows, sql = self.store.investigate(
                group_by=step.group_by,
                metric=step.metric,
                period=period,
                comparison_period=comparison if step.compare_periods else None,
                top_n=step.top_n,
            )
            result_sets.append(rows)
            sqls.append(sql)
            yield "sql", {"sql": sql, "rows": len(rows)}

        yield "status", {"message": "Writing grounded explanation..."}
        llm_error: str | None = None
        if self.api_key:
            import litellm

            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are a FinOps financial analyst. Answer only from the supplied SQL results. "
                        "Do not invent figures. Write 2-3 concise sentences for a finance owner."
                    ),
                },
                {
                    "role": "user",
                    "content": f"Question: {question}\nApproved Plan: {plan.intent}\nData: {json.dumps(result_sets)}",
                },
            ]
            try:
                response = litellm.completion(
                    model=self.model_name,
                    messages=messages,
                    api_key=self.api_key,
                    stream=True,
                    max_tokens=300,
                    num_retries=0,
                )
                streamed = False
                for chunk in response:
                    delta = chunk.choices[0].delta.content or ""
                    if delta:
                        streamed = True
                        yield "text", {"delta": delta}
                if not streamed:
                    llm_error = "empty LLM response"
            except Exception as exc:  # noqa: BLE001 — degrade, never fail the request
                llm_error = f"{type(exc).__name__}: {exc}"[:300]

        if not self.api_key or llm_error:
            # Deterministic fallback: no key, or the LLM call failed (e.g.
            # invalid key, provider outage). The data answer still ships.
            top_row = result_sets[0][0] if (result_sets and result_sets[0]) else {}
            group_key = plan.queries[0].group_by
            top_val = top_row.get(group_key, "N/A")
            cost_val = top_row.get("value") or top_row.get("later") or 0.0
            yield (
                "text",
                {
                    "delta": (
                        f"Investigation plan '{plan.intent}' identified {top_val} as the leading driver "
                        f"(${cost_val:,.0f}) for {period}."
                    )
                },
            )

        elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        yield (
            "meta",
            {
                "latency_ms": elapsed_ms,
                "intent": plan.intent,
                "query_count": len(plan.queries),
                **({"llm_error": llm_error} if llm_error else {}),
            },
        )

    def _plan(self, question: str, context: dict[str, Any]) -> InvestigationPlan:
        if self.planner_lm:
            try:
                with dspy.context(lm=self.planner_lm):
                    prediction = self.planner(
                        screen_context=json.dumps(context), question=question
                    )
                raw = (
                    prediction.plan_json.strip()
                    .removeprefix("```json")
                    .removeprefix("```")
                    .removesuffix("```")
                    .strip()
                )
                match = re.search(r"\{.*\}", raw, re.DOTALL)
                payload = json.loads(match.group(0) if match else raw)
                return InvestigationPlan.model_validate(payload)
            except (ValidationError, Exception):
                pass

        # Safe fallback plan
        group_by = context.get("group_by", "service")
        if group_by not in (
            "service",
            "account",
            "region",
            "usage_kind",
            "instance_type",
            "commitment_scope",
        ):
            group_by = "service"
        return InvestigationPlan(
            intent=f"Analyze spend distribution across {group_by}",
            queries=[
                QueryStep(
                    group_by=group_by,
                    metric="on_demand_cost",
                    compare_periods=bool(context.get("compare")),
                    top_n=8,
                )
            ],
        )
