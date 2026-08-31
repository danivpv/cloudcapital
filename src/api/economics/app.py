"""HTTP adapter for the economics bounded context, mounted at ``/econ``.

``EconomicsService`` is the in-process facade used by both the router and the
surface. Its pure domain dependency lives in ``domain.py``; its HTTP adapter is
this router. A remote HTTP client can replace this facade later without changing
callers.
"""

from __future__ import annotations

import threading
from functools import lru_cache

from fastapi import APIRouter
from pydantic import BaseModel

from ..economics import domain

router = APIRouter(prefix="/econ", tags=["economics"])

DEFAULT = domain.Proposal(
    scope="AWS#Compute",
    commitment_per_hour=16.0,
    term_months=12,
    payment_option="no_upfront",
)


class ProposalBody(BaseModel):
    scope: str = DEFAULT.scope
    commitment_per_hour: float = DEFAULT.commitment_per_hour
    term_months: int = DEFAULT.term_months
    payment_option: str = DEFAULT.payment_option

    def to_proposal(self) -> domain.Proposal:
        return domain.Proposal(
            self.scope, self.commitment_per_hour, self.term_months, self.payment_option
        )


class ImpactDiceBody(ProposalBody):
    dims: list[str] | None = None


class EconomicsService:
    """Thread-safe facade over the settled economics domain functions."""

    def __init__(self) -> None:
        self._con = None
        self._lock = threading.Lock()

    def _connection(self):
        if self._con is None:
            self._con = domain.connect()
        return self._con

    @staticmethod
    def _key(p: domain.Proposal) -> tuple:
        return (p.scope, p.commitment_per_hour, p.term_months, p.payment_option)

    @lru_cache(maxsize=16)
    def _economics_cached(self, key: tuple) -> dict:
        return domain.economics(self._connection(), domain.Proposal(*key))

    @lru_cache(maxsize=16)
    def _impact_dice_cached(
        self, key: tuple, dims: tuple[str, ...]
    ) -> dict[str, list[dict]]:
        return domain.impact_dice(self._connection(), domain.Proposal(*key), list(dims))

    def proposal(self) -> dict:
        p = DEFAULT
        with self._lock:
            rate = self._economics_cached(self._key(p))["customer_savings_rate"]
        return {
            "instrument": "compute_savings_plan",
            "scope": p.scope,
            "commitment_per_hour": p.commitment_per_hour,
            "term_months": p.term_months,
            "payment_option": p.payment_option,
            "guaranteed_savings_rate": rate,
        }

    def economics(self, p: domain.Proposal = DEFAULT) -> dict:
        with self._lock:
            return self._economics_cached(self._key(p))

    def cost_of_risk(self, p: domain.Proposal) -> dict:
        with self._lock:
            return domain.cost_of_risk(self._connection(), p)

    def impact_dice(
        self, p: domain.Proposal, dims: list[str] | None = None
    ) -> dict[str, list[dict]]:
        dims_key = tuple(dims or domain.DIM_SQL)
        with self._lock:
            return self._impact_dice_cached(self._key(p), dims_key)


service = EconomicsService()


@router.get("/proposal")
def proposal():
    return service.proposal()


@router.post("/impact-dice")
def impact_dice(body: ImpactDiceBody):
    """Per-dimension monthly savings aggregates, never the per-line firehose."""
    return {
        "dims": body.dims or list(domain.DIM_SQL),
        "dice": service.impact_dice(body.to_proposal(), body.dims),
    }


@router.post("/cost-of-risk")
def cost_of_risk(body: ProposalBody):
    return service.cost_of_risk(body.to_proposal())


@router.api_route("/economics", methods=["GET", "POST"])
def economics_endpoint(body: ProposalBody | None = None):
    return service.economics(body.to_proposal() if body else DEFAULT)
