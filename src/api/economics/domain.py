"""Pure commitment-economics math — ported verbatim from the provided service.

This is the settled logic: the hourly allocation, the cost-of-risk stand-in, and
the customer/reserve/profit split. It is intentionally free of HTTP and I/O
concerns (those live in ``app.py``) so it can be unit-tested and reasoned about
in isolation.

One addition over the original: ``impact_dice``, which collapses the per-line
impact into per-dimension monthly aggregates *here*, on the correct side of the
boundary, so the surface never transfers the firehose.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

import duckdb

# Data directory resolution (supports local dev and container paths)
_DATA_DIR_ENV = os.environ.get("CC_DATA_DIR")
if _DATA_DIR_ENV:
    _DATA = Path(_DATA_DIR_ENV)
else:
    _SRC_DIR = Path(__file__).resolve().parents[2]
    if (_SRC_DIR / "data.parquet").exists():
        _DATA = _SRC_DIR / "data.parquet"
    elif (_SRC_DIR / "data").exists():
        _DATA = _SRC_DIR / "data"
    else:
        _DATA = _SRC_DIR / "data.parquet"

USAGE_PATH = os.environ.get("USAGE_PARQUET", str(_DATA / "candidate_dataset.parquet"))
PRICING_PATH = os.environ.get(
    "PRICING_PARQUET", str(_DATA / "pricing_options_filtered.parquet")
)

PROFIT_RATE = 0.10
COR_MIN_POINTS = 4.0
COR_MAX_POINTS = 80.0

# Canonical dimension SQL: single source of truth, shared with the store so the
# savings dice and the usage dice key on identical expressions.
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


def connect() -> duckdb.DuckDBPyConnection:
    """A connection with the eligible Compute usage lines pre-loaded."""
    con = duckdb.connect()
    con.execute(
        f"""
        CREATE TABLE lines AS
        WITH elig AS (
          SELECT u.timestamp, u.account_id, u.product_code, u.usage_type,
                 u.instance_type, u.commitment_key, u.price_list_key,
                 u.on_demand_cost, u.usage_amount, p.rate AS sp_rate, p.term_months
          FROM '{USAGE_PATH}' u
          JOIN '{PRICING_PATH}' p
            ON u.price_list_key = p.price_list_key
           AND p.instrument_type = 'compute_savings_plan'
           AND p.payment_option = 'no_upfront'
          WHERE u.commitment_key LIKE 'AWS#Compute%'
            AND u.usage_amount > 0 AND u.on_demand_cost > 0
        )
        SELECT *,
          usage_amount * sp_rate AS line_disc_cost,
          1 - sp_rate / (on_demand_cost / usage_amount) AS discount
        FROM elig
        WHERE 1 - sp_rate / (on_demand_cost / usage_amount) BETWEEN 0 AND 0.95
        """
    )
    return con


def _impact_relation(con, p: Proposal):
    """Per-line hourly impact of the proposed commitment, as an unmaterialized relation."""
    L = float(p.commitment_per_hour)
    con.execute(
        "CREATE OR REPLACE TEMP VIEW impact AS "
        + f"""
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
        "CREATE OR REPLACE TEMP VIEW impact_rows AS "
        """
        SELECT timestamp, account_id, product_code, usage_type, instance_type, commitment_key,
               covered_on_demand_cost, committed_cost,
               covered_on_demand_cost - committed_cost AS gross_savings
        FROM impact
        """
    )


def impact_dice(
    con, p: Proposal, dims: list[str] | None = None
) -> dict[str, list[dict]]:
    """Per-(dim, month) savings aggregates, collapsed here rather than at the surface.

    One allocation pass, then a cheap GROUP BY per requested dimension — the
    ~700-row-per-dim shape the surface needs, never the ~1M-row firehose.
    """
    dims = dims or list(DIM_SQL)
    for d in dims:
        if d not in DIM_SQL:
            raise ValueError(f"unknown dimension: {d}")
    _impact_relation(con, p)
    keys = [
        "dim_value",
        "billing_period",
        "covered_on_demand_cost",
        "committed_cost",
        "gross_savings",
    ]
    out: dict[str, list[dict]] = {}
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


def _aggregate_economics(con, p: Proposal) -> dict:
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


def cost_of_risk(con, p: Proposal) -> dict:
    """A plausible stand-in for our proprietary cost-of-risk model."""
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


def economics(con, p: Proposal) -> dict:
    """Headline economics: aggregate impact, cost of risk, and the split."""
    agg = _aggregate_economics(con, p)
    cor = cost_of_risk(con, p)
    S = agg["net_savings"]
    profit = PROFIT_RATE * S
    reserve = (cor["cost_of_risk_points"] / 100.0) * S
    customer_savings = S - profit - reserve
    return {
        "proposal": asdict(p),
        **agg,
        **cor,
        "profit": profit,
        "reserve": reserve,
        "customer_savings": customer_savings,
        "customer_savings_rate": (customer_savings / agg["covered_on_demand_cost"])
        if agg["covered_on_demand_cost"]
        else 0.0,
    }
