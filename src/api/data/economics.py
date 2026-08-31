"""Surface-side adapter for the economics bounded context.

The default is an in-process adapter over the migrated service. ``HttpEconomicsClient``
is retained as the swappable deployment adapter: moving economics into its own
container later changes composition, not callers or routes.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Protocol

import httpx

from ..config import Settings
from ..economics import domain
from ..economics.app import DEFAULT, EconomicsService
from .store import Store


class EconomicsGateway(Protocol):
    def proposal(self) -> dict: ...
    def economics(self, body: dict | None = None) -> dict: ...
    def cost_of_risk(self, body: dict | None = None) -> dict: ...
    def fetch_impact_dice(self, dims: list[str] | None = None) -> None: ...


class InProcessEconomicsClient:
    """Local adapter: no loopback HTTP, no port dependency, same service boundary."""

    def __init__(self, store: Store, service: EconomicsService) -> None:
        self._store = store
        self._service = service

    @staticmethod
    def _proposal(body: dict | None) -> domain.Proposal:
        return domain.Proposal(**body) if body else DEFAULT

    def proposal(self) -> dict:
        return self._service.proposal()

    def economics(self, body: dict | None = None) -> dict:
        return self._service.economics(self._proposal(body))

    def cost_of_risk(self, body: dict | None = None) -> dict:
        return self._service.cost_of_risk(self._proposal(body))

    def fetch_impact_dice(self, dims: list[str] | None = None) -> None:
        dice = self._service.impact_dice(DEFAULT, dims)
        for dim, rows in dice.items():
            self._store.materialize_impact_dice(dim, rows)


class HttpEconomicsClient:
    """Remote adapter for a later separate economics deployment."""

    def __init__(self, settings: Settings, store: Store) -> None:
        self._settings = settings
        self._store = store
        self._cache: dict[str, tuple[float, Any]] = {}
        self._lock = threading.Lock()
        self._client = httpx.Client(base_url=settings.economics_url, timeout=30.0)

    def _get(self, path: str) -> Any:
        ttl = self._settings.economics_cache_ttl
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(path)
            if hit and now - hit[0] < ttl:
                return hit[1]
        data = self._client.get(path).json()
        with self._lock:
            self._cache[path] = (now, data)
        return data

    def _post(self, path: str, body: dict) -> Any:
        ttl = self._settings.economics_cache_ttl
        key = path + repr(sorted(body.items()))
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        r = self._client.post(path, json=body)
        r.raise_for_status()
        data = r.json()
        with self._lock:
            self._cache[key] = (now, data)
        return data

    def proposal(self) -> dict:
        return self._get("/proposal")

    def economics(self, body: dict | None = None) -> dict:
        return self._post("/economics", body or {})

    def cost_of_risk(self, body: dict | None = None) -> dict:
        return self._post("/cost-of-risk", body or {})

    def fetch_impact_dice(self, dims: list[str] | None = None) -> None:
        data = self._post("/impact-dice", {"dims": dims} if dims else {})
        for dim, rows in data["dice"].items():
            self._store.materialize_impact_dice(dim, rows)
