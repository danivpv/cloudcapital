"""LLM access with a hard budget envelope around every call.

Single responsibility: one thin adapter over LiteLLM that (a) enforces
per-step token caps, (b) records usage into the ledger, and (c) degrades to
None when no API key is configured so the surface runs without a model.

Open/closed: the pipeline depends on :class:`LLMClient`, not on LiteLLM.
A mock client plugs into tests with zero change elsewhere.
"""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator  # noqa: UP035

from ..config import Settings

try:
    import litellm as _litellm
    from litellm import (
        completion as _litellm_completion,
        stream_chunk_builder as _litellm_stream_chunk_builder,
    )

    _litellm.suppress_debug_info = True  # silence OpenRouter provider-resolution noise
    litellm: Any = _litellm
    completion: Any = _litellm_completion
    stream_chunk_builder: Any = _litellm_stream_chunk_builder
    _LITELLM_AVAILABLE = True
except ImportError:  # pragma: no cover
    litellm = None
    completion = None
    stream_chunk_builder = None
    _LITELLM_AVAILABLE = False


@dataclass
class Budget:
    """The envelope every step must stay inside.

    Modeled on the sezzle agent's limits: a hard step count and per-step
    token caps, plus a total-token cap and a cost ceiling so a looping or
    rambling call cannot blow the budget for the whole question.
    """

    max_steps: int = 4
    router_max_tokens: int = 200
    query_max_tokens: int = 600
    prose_max_tokens: int = 400
    max_total_tokens: int = 1_500
    max_cost_usd: float = 0.01


@dataclass
class UsageRecord:
    step: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0


class BudgetExceeded(Exception):
    """Raised when a step or the question exceeds its envelope."""


@dataclass
class Ledger:
    """Thread-safe per-question usage ledger (drives /metrics and the cost note)."""

    records: list[UsageRecord] = field(default_factory=list)
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, record: UsageRecord) -> None:
        with self._lock:
            self.records.append(record)
            self.total_tokens += record.prompt_tokens + record.completion_tokens
            self.total_cost_usd += record.cost_usd

    def summary(self) -> dict:
        return {
            "steps": [r.__dict__ for r in self.records],
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
        }


class LLMClient(ABC):
    @abstractmethod
    def complete(
        self,
        step: str,
        messages: list[dict],
        budget: Budget,
        ledger: Ledger,
        response_schema: dict | None = None,
    ) -> str | None: ...

    @abstractmethod
    def stream(
        self, step: str, messages: list[dict], budget: Budget, ledger: Ledger
    ) -> Iterator[str]: ...


class LiteLLMClient(LLMClient):
    def __init__(self, settings: Settings) -> None:
        self._model = settings.llm_model
        self._router_model = settings.llm_router_model
        self._api_key = settings.openrouter_api_key

    def _configured(self) -> bool:
        return _LITELLM_AVAILABLE and bool(self._api_key)

    def _pick_model(self, step: str) -> str:
        return self._router_model if step == "route" else self._model

    def complete(
        self,
        step: str,
        messages: list[dict],
        budget: Budget,
        ledger: Ledger,
        response_schema: dict | None = None,
    ) -> str | None:
        if not self._configured() or completion is None:
            return None
        if len(ledger.records) >= budget.max_steps:
            raise BudgetExceeded(f"step cap reached before step {step}")
        if ledger.total_tokens >= budget.max_total_tokens:
            raise BudgetExceeded(f"total token cap reached before step {step}")
        cap = budget.router_max_tokens if step == "route" else budget.query_max_tokens
        kwargs: dict = {
            "model": self._pick_model(step),
            "api_key": self._api_key,
            "messages": messages,
            "max_tokens": min(cap, budget.max_total_tokens - ledger.total_tokens),
        }
        if response_schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": response_schema,
            }
        started = time.perf_counter()
        resp = completion(**kwargs)
        latency_ms = (time.perf_counter() - started) * 1000
        usage = (
            resp.get("usage", {})
            if isinstance(resp, dict)
            else getattr(resp, "usage", {})
        )
        prompt_tokens = (
            usage.get("prompt_tokens", 0)
            if isinstance(usage, dict)
            else getattr(usage, "prompt_tokens", 0)
        )
        completion_tokens = (
            usage.get("completion_tokens", 0)
            if isinstance(usage, dict)
            else getattr(usage, "completion_tokens", 0)
        )
        try:
            cost = (
                float(litellm.completion_cost(completion_response=resp))
                if litellm is not None
                else 0.0
            )
        except Exception:
            cost = 0.0
        ledger.add(
            UsageRecord(
                step,
                self._pick_model(step),
                int(prompt_tokens or 0),
                int(completion_tokens or 0),
                cost,
                latency_ms,
            )
        )
        if ledger.total_cost_usd > budget.max_cost_usd:
            raise BudgetExceeded(f"cost ceiling exceeded at step {step}")
        return resp["choices"][0]["message"]["content"]

    def stream(
        self, step: str, messages: list[dict], budget: Budget, ledger: Ledger
    ) -> Iterator[str]:
        if not self._configured() or completion is None:
            return
        if len(ledger.records) >= budget.max_steps:
            raise BudgetExceeded(f"step cap reached before step {step}")
        model = self._pick_model(step)
        kwargs: dict = {
            "model": model,
            "api_key": self._api_key,
            "messages": messages,
            "max_tokens": min(
                budget.prose_max_tokens,
                budget.max_total_tokens - ledger.total_tokens,
            ),
            "stream": True,
        }
        chunks: list = []
        started = time.perf_counter()
        try:
            response = completion(**kwargs)
            for chunk in response:
                chunks.append(chunk)
                delta = (
                    chunk["choices"][0]["delta"]
                    if isinstance(chunk, dict)
                    else chunk.choices[0].delta
                )
                text = (
                    delta.get("content")
                    if isinstance(delta, dict)
                    else getattr(delta, "content", "")
                ) or ""
                if text:
                    yield text
        finally:
            if chunks and stream_chunk_builder is not None:
                try:
                    whole = stream_chunk_builder(chunks)
                    usage = (
                        whole.get("usage", {})
                        if isinstance(whole, dict)
                        else getattr(whole, "usage", {})
                    )
                    prompt_tokens = (
                        usage.get("prompt_tokens", 0)
                        if isinstance(usage, dict)
                        else getattr(usage, "prompt_tokens", 0)
                    )
                    completion_tokens = (
                        usage.get("completion_tokens", 0)
                        if isinstance(usage, dict)
                        else getattr(usage, "completion_tokens", 0)
                    )
                    cost = (
                        float(litellm.completion_cost(completion_response=whole))
                        if litellm is not None
                        else 0.0
                    )
                except Exception:
                    prompt_tokens = 0
                    completion_tokens = 0
                    cost = 0.0
                ledger.add(
                    UsageRecord(
                        step,
                        model,
                        int(prompt_tokens or 0),
                        int(completion_tokens or 0),
                        cost,
                        (time.perf_counter() - started) * 1000,
                    )
                )
