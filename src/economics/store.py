"""Analytical store operating over precomputed dice.duckdb."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import duckdb

from .domain import DIM_SQL

_DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data"
_DATA_DIR = Path(os.environ.get("CC_DATA_DIR", _DEFAULT_DATA_DIR))
DICE_DB_PATH = _DATA_DIR / "dice.duckdb"
LINES_PARQUET_PATH = _DATA_DIR / "lines.parquet"

USAGE_METRICS = ["amortized_cost", "on_demand_cost", "usage_amount"]
IMPACT_METRICS = ["committed_cost", "covered_on_demand_cost", "gross_savings"]
ALL_METRICS = USAGE_METRICS + IMPACT_METRICS
DIMENSIONS = sorted(DIM_SQL)


class Store:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or DICE_DB_PATH
        self._con: duckdb.DuckDBPyConnection | None = None

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            if not self.db_path.exists():
                raise FileNotFoundError(f"Dice database not found at {self.db_path}")
            # Connect in read-only mode so multiple workers/threads can read safely
            self._con = duckdb.connect(str(self.db_path), read_only=True)
        return self._con

    def vocab(self) -> dict[str, Any]:
        rows = self.con.execute(
            "SELECT billing_period, partial, first_ts, last_ts FROM metadata_periods ORDER BY billing_period"
        ).fetchall()
        periods = [
            {
                "period": r[0],
                "partial": bool(r[1]),
                "first_ts": r[2],
                "last_ts": r[3],
            }
            for r in rows
        ]
        complete = [p["period"] for p in periods if not p["partial"]]
        return {
            "dims": DIMENSIONS,
            "metrics": ALL_METRICS,
            "periods": periods,
            "complete_periods": complete,
        }

    def slice(
        self,
        group_by: str,
        metric: str,
        window: tuple[str, str] | None = None,
        top_n: int = 12,
    ) -> list[dict[str, Any]]:
        if group_by not in DIM_SQL:
            raise ValueError(f"Unknown dimension: {group_by}")
        table = (
            f"dice_{group_by}_savings"
            if metric in IMPACT_METRICS
            else f"dice_{group_by}"
        )

        where, args = "", []
        if window:
            where = "WHERE billing_period BETWEEN ? AND ?"
            args = [window[0], window[1]]

        sql = f"""
        SELECT dim_value, SUM({metric}) AS value
        FROM {table} {where}
        GROUP BY 1 ORDER BY value DESC LIMIT {int(top_n)}
        """
        rows = self.con.execute(sql, args).fetchall()
        return [{group_by: r[0], "value": r[1]} for r in rows]

    def delta(
        self,
        group_by: str,
        metric: str,
        window_a: tuple[str, str],
        window_b: tuple[str, str],
        top_n: int = 12,
    ) -> list[dict[str, Any]]:
        if group_by not in DIM_SQL:
            raise ValueError(f"Unknown dimension: {group_by}")
        table = (
            f"dice_{group_by}_savings"
            if metric in IMPACT_METRICS
            else f"dice_{group_by}"
        )

        sql = f"""
        WITH a AS (
          SELECT dim_value, SUM({metric}) AS va FROM {table}
          WHERE billing_period BETWEEN ? AND ? GROUP BY 1
        ),
        b AS (
          SELECT dim_value, SUM({metric}) AS vb FROM {table}
          WHERE billing_period BETWEEN ? AND ? GROUP BY 1
        )
        SELECT COALESCE(a.dim_value, b.dim_value) AS dim_value,
               COALESCE(a.va, 0) AS value_a,
               COALESCE(b.vb, 0) AS value_b,
               COALESCE(b.vb, 0) - COALESCE(a.va, 0) AS delta
        FROM a FULL OUTER JOIN b ON a.dim_value = b.dim_value
        ORDER BY ABS(delta) DESC
        LIMIT {int(top_n)}
        """
        rows = self.con.execute(
            sql, [window_a[0], window_a[1], window_b[0], window_b[1]]
        ).fetchall()
        return [
            {
                group_by: r[0],
                "value_a": r[1],
                "value_b": r[2],
                "delta": r[3],
                "pct": (r[3] / r[1] * 100) if r[1] else None,
            }
            for r in rows
        ]

    def explore(
        self,
        group_by: list[str],
        metrics: list[str],
        window: tuple[str, str] | None = None,
        top_n: int = 50,
    ) -> list[dict[str, Any]]:
        dim = group_by[0] if group_by else "service"
        metric = metrics[0] if metrics else "on_demand_cost"
        table = f"dice_{dim}_savings" if metric in IMPACT_METRICS else f"dice_{dim}"

        where, args = "", []
        if window:
            where = "WHERE billing_period BETWEEN ? AND ?"
            args = [window[0], window[1]]

        facts = ", ".join(f"SUM({m}) AS {m}" for m in metrics)
        order_col = metrics[0]
        sql = f"""
        SELECT dim_value AS {dim}, billing_period, {facts}
        FROM {table} {where}
        GROUP BY 1, 2
        ORDER BY {order_col} DESC LIMIT {int(top_n)}
        """
        rows = self.con.execute(sql, args).fetchall()
        cols = [dim, "billing_period", *metrics]
        return [dict(zip(cols, r)) for r in rows]

    def drill(
        self,
        group_by: str,
        value: str,
        window: tuple[str, str] | None = None,
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        if not LINES_PARQUET_PATH.exists():
            return []
        con = duckdb.connect(":memory:")
        cols = [
            "timestamp",
            "account_id",
            "product_code",
            "usage_type",
            "instance_type",
            "commitment_key",
            "usage_amount",
            "on_demand_cost",
        ]
        dim_col = DIM_SQL.get(group_by, group_by)
        where = f"WHERE {dim_col} = ?"
        args: list[Any] = [value]
        if window:
            where += " AND strftime(timestamp, '%Y-%m') BETWEEN ? AND ?"
            args.extend([window[0], window[1]])

        sql = f"""
        SELECT {", ".join(cols)}
        FROM read_parquet('{LINES_PARQUET_PATH.as_posix()}')
        {where}
        ORDER BY on_demand_cost DESC LIMIT {int(limit)}
        """
        rows = con.execute(sql, args).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def investigate(
        self,
        group_by: str,
        metric: str,
        period: str,
        comparison_period: str | None = None,
        selected_dimension: str | None = None,
        selected_value: str | None = None,
        top_n: int = 10,
    ) -> tuple[list[dict[str, Any]], str]:
        top_n = min(max(int(top_n), 1), 12)
        table = f"dice_{group_by}"
        if comparison_period is None:
            sql = f"""
            SELECT dim_value AS {group_by}, SUM({metric}) AS value
            FROM {table}
            WHERE billing_period = ?
            GROUP BY 1 ORDER BY value DESC LIMIT {top_n}
            """
            rows = self.con.execute(sql, [period]).fetchall()
            display_sql = f"SELECT {group_by}, SUM({metric}) FROM {table} WHERE billing_period = '{period}' GROUP BY 1 ORDER BY 2 DESC LIMIT {top_n};"
            return [{group_by: r[0], "value": r[1]} for r in rows], display_sql

        sql = f"""
        WITH earlier AS (
          SELECT dim_value, SUM({metric}) AS earlier
          FROM {table} WHERE billing_period = ? GROUP BY 1
        ), later AS (
          SELECT dim_value, SUM({metric}) AS later
          FROM {table} WHERE billing_period = ? GROUP BY 1
        )
        SELECT COALESCE(earlier.dim_value, later.dim_value) AS dim_value,
               COALESCE(earlier.earlier, 0) AS earlier,
               COALESCE(later.later, 0) AS later,
               COALESCE(later.later, 0) - COALESCE(earlier.earlier, 0) AS change
        FROM earlier FULL OUTER JOIN later ON earlier.dim_value = later.dim_value
        ORDER BY ABS(change) DESC LIMIT {top_n}
        """
        rows = self.con.execute(sql, [comparison_period, period]).fetchall()
        display_sql = f"WITH earlier AS (SELECT dim_value, SUM({metric}) FROM {table} WHERE billing_period = '{comparison_period}' GROUP BY 1), later AS (SELECT dim_value, SUM({metric}) FROM {table} WHERE billing_period = '{period}' GROUP BY 1) SELECT dim_value, later - earlier AS change FROM earlier FULL OUTER JOIN later ON earlier.dim_value = later.dim_value ORDER BY ABS(change) DESC LIMIT {top_n};"
        return [
            {group_by: r[0], "earlier": r[1], "later": r[2], "change": r[3]}
            for r in rows
        ], display_sql

    def impact_ready(self) -> bool:
        try:
            self.con.execute("SELECT 1 FROM dice_service_savings LIMIT 1").fetchone()
            return True
        except Exception:
            return False
