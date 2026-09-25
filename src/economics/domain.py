"""Settled commitment economics domain math.

Executes over precomputed lines.parquet, retaining exact financial formulas.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import duckdb

_DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
_DATA_DIR = Path(os.environ.get("CC_DATA_DIR", _DEFAULT_DATA_DIR))
LINES_PARQUET = _DATA_DIR / "lines.parquet"

PROFIT_RATE = 0.10
COR_MIN_POINTS = 4.0
COR_MAX_POINTS = 80.0

DIM_SQL: dict[str, str] = {
    "service": "product_code",
    "account": "account_id",
    "usage_kind": "usage_type",
    "instance_type": "COALESCE(instance_type, 'none')",
    "commitment_scope": "COALESCE(commitment_key, 'none')",
    "region": (
        "COALESCE(NULLIF(REGEXP_EXTRACT(usage_type, '^((US|EU|AP|SA|CA|ME|AF|IL)[A-Z0-9]+)-', 1), ''), 'no-prefix')"
    ),
}


@dataclass
class Proposal:
    scope: str = "AWS#Compute"
    commitment_per_hour: float = 16.0
    term_months: int = 12
    payment_option: str = "no_upfront"


DEFAULT_PROPOSAL = Proposal()


def get_connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    if LINES_PARQUET.exists():
        con.execute(
            f"CREATE VIEW lines AS SELECT * FROM read_parquet('{LINES_PARQUET.as_posix()}')"
        )
    return con


def _impact_relation(con: duckdb.DuckDBPyConnection, p: Proposal) -> None:
    L = float(p.commitment_per_hour)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW impact AS
        WITH ranked AS (
          SELECT timestamp, account_id, product_code, usage_type, instance_type,
                 commitment_key, on_demand_cost, line_disc_cost, discount,
                 SUM(line_disc_cost) OVER (PARTITION BY timestamp ORDER BY discount DESC
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS cum
          FROM lines
          WHERE term_months = {int(p.term_months)}
        )
        SELECT timestamp, account_id, product_code, usage_type, instance_type, commitment_key,
          GREATEST(0, LEAST(line_disc_cost, {L} - (cum - line_disc_cost))) AS committed_cost,
          CASE WHEN line_disc_cost > 0
               THEN GREATEST(0, LEAST(line_disc_cost, {L} - (cum - line_disc_cost))) / line_disc_cost
               ELSE 0 END * on_demand_cost AS covered_on_demand_cost
        FROM ranked
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TEMP VIEW impact_rows AS
        SELECT timestamp, account_id, product_code, usage_type, instance_type, commitment_key,
               covered_on_demand_cost, committed_cost,
               covered_on_demand_cost - committed_cost AS gross_savings
        FROM impact
        """
    )


def impact_dice(
    con: duckdb.DuckDBPyConnection, p: Proposal, dims: list[str] | None = None
) -> dict[str, list[dict[str, Any]]]:
    dims = dims or list(DIM_SQL)
    _impact_relation(con, p)
    keys = [
        "dim_value",
        "billing_period",
        "covered_on_demand_cost",
        "committed_cost",
        "gross_savings",
    ]
    out: dict[str, list[dict[str, Any]]] = {}
    for d in dims:
        expr = DIM_SQL[d]
        rows = con.execute(
            f"""
            SELECT {expr} AS dim_value,
                   strftime(timestamp, '%Y-%m') AS billing_period,
                   SUM(covered_on_demand_cost) AS covered_on_demand_cost,
                   SUM(committed_cost) AS committed_cost,
                   SUM(gross_savings) AS gross_savings
            FROM impact_rows
            GROUP BY 1, 2
            """
        ).fetchall()
        out[d] = [dict(zip(keys, r)) for r in rows]
    return out


def _aggregate_economics(
    con: duckdb.DuckDBPyConnection, p: Proposal
) -> dict[str, float]:
    _impact_relation(con, p)
    L = float(p.commitment_per_hour)
    covered, committed, gross = con.execute(
        "SELECT sum(covered_on_demand_cost), sum(committed_cost), sum(gross_savings) FROM impact_rows"
    ).fetchone()
    waste = (
        con.execute(
            f"""WITH h AS (SELECT timestamp, sum(committed_cost) used FROM impact GROUP BY 1)
            SELECT sum(GREATEST(0, {L} - used)) FROM h"""
        ).fetchone()[0]
        or 0.0
    )
    net = (gross or 0.0) - waste
    return {
        "covered_on_demand_cost": covered or 0.0,
        "committed_cost": committed or 0.0,
        "gross_savings": gross or 0.0,
        "wasted_commitment": waste,
        "net_savings": net,
    }


def cost_of_risk(con: duckdb.DuckDBPyConnection, p: Proposal) -> dict[str, Any]:
    _impact_relation(con, p)
    L = float(p.commitment_per_hour)
    protection = (
        con.execute(
            f"""WITH h AS (SELECT timestamp, sum(line_disc_cost) hr FROM lines
                       WHERE term_months = {int(p.term_months)} GROUP BY 1)
            SELECT avg(CASE WHEN hr >= {L} THEN 1.0 ELSE 0.0 END) FROM h"""
        ).fetchone()[0]
        or 0.0
    )
    trend = (
        con.execute(
            f"""WITH m AS (
              SELECT billing_period_idx, total FROM (
                SELECT row_number() OVER (ORDER BY mo) AS billing_period_idx,
                       total, cnt
                FROM (
                  SELECT date_trunc('month', timestamp) mo, sum(line_disc_cost) total,
                         count(distinct timestamp) cnt
                  FROM lines WHERE term_months = {int(p.term_months)} GROUP BY 1
                )
              ) WHERE cnt >= 672
            )
            SELECT regr_slope(total, billing_period_idx) * count(*) / NULLIF(avg(total),0)
            FROM m"""
        ).fetchone()[0]
        or 0.0
    )
    under = 1.0 - protection
    downtrend = max(0.0, -trend)
    uptrend = max(0.0, trend)
    combine = (
        under * (0.6 + 0.8 * downtrend) + 0.4 * under**2 - 0.15 * uptrend * protection
    )
    combine = min(1.0, max(0.0, combine))
    points = round(COR_MIN_POINTS + (COR_MAX_POINTS - COR_MIN_POINTS) * combine, 1)
    return {
        "cost_of_risk_points": points,
        "protection": round(protection, 3),
        "trend": round(trend, 3),
        "infeasible": points >= COR_MAX_POINTS,
    }


def economics(con: duckdb.DuckDBPyConnection, p: Proposal) -> dict[str, Any]:
    agg = _aggregate_economics(con, p)
    cor = cost_of_risk(con, p)
    S = agg["net_savings"]
    profit = PROFIT_RATE * S
    reserve = (cor["cost_of_risk_points"] / 100.0) * S
    customer_savings = S - profit - reserve
    rate = (
        (customer_savings / agg["covered_on_demand_cost"])
        if agg["covered_on_demand_cost"]
        else 0.0
    )
    return {
        "proposal": asdict(p),
        **agg,
        **cor,
        "profit": profit,
        "reserve": reserve,
        "customer_savings": customer_savings,
        "customer_savings_rate": rate,
    }
