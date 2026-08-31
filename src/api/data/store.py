"""Data layer: DuckDB store for the usage firehose and the impact detail.

Single responsibility: own the connections, the precomputed dice, and the
materialized impact rows. Everything else (query building, economics client,
intelligence) talks to this class through narrow methods.

The precompute/on-demand line:
- One dice table per dimension is built lazily on first use (a single scan
  of the firehose per dimension, aggregating all three facts at once), then
  reused for every subsequent query.
- Multi-dimension cuts or filters fall back to a raw scan, capped by LIMIT.
- Impact ingestion runs on its own connection so a long materialization can
  never block an interactive query.
"""

from __future__ import annotations

import calendar
import threading
from dataclasses import dataclass

import duckdb

from ..config import Settings
from ..economics.domain import DIM_SQL as _DIM_SQL

_DICE_FACTS: dict[str, str] = {
    "on_demand_cost": "SUM(on_demand_cost)",
    "usage_amount": "SUM(usage_amount)",
    "amortized_cost": "SUM(amortized_cost)",
}

_IMPACT_FACTS: dict[str, str] = {
    "covered_on_demand_cost": "SUM(covered_on_demand_cost)",
    "committed_cost": "SUM(committed_cost)",
    "gross_savings": "SUM(gross_savings)",
}

# Public vocabulary: the whitelist the explorer UI and the LLM query spec share.
DIMENSIONS = sorted(_DIM_SQL)
USAGE_METRICS = sorted(_DICE_FACTS)
IMPACT_METRICS = sorted(_IMPACT_FACTS)
ALL_METRICS = USAGE_METRICS + IMPACT_METRICS

# Bump to invalidate the persisted dice when the dice SQL changes.
_SCHEMA_VERSION = 5


@dataclass
class ExploreParams:
    group_by: list[str]
    metrics: list[str]
    window: tuple[str, str] | None = None
    filters: dict[str, list[str]] | None = None
    top_n: int = 50
    order_by: str | None = None
    granularity: str = "month"


class Store:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = threading.Lock()
        self._built_dice: set[str] = set()
        self._con: duckdb.DuckDBPyConnection | None = None
        self._ingest_con: duckdb.DuckDBPyConnection | None = None

    @property
    def con(self) -> duckdb.DuckDBPyConnection:
        if self._con is None:
            self.connect()
        assert self._con is not None
        return self._con

    def connect(self) -> duckdb.DuckDBPyConnection:
        with self._lock:
            if self._con is not None:
                return self._con
            settings = self._settings
            settings.cache_dir.mkdir(parents=True, exist_ok=True)
            self._con = duckdb.connect(str(settings.cache_dir / "store.duckdb"))
            current = self._con.execute(
                "SELECT * FROM information_schema.tables WHERE table_name = 'schema_version'"
            ).fetchall()
            if not current:
                for t in self._con.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_name LIKE 'dice_%' OR table_name = 'impact_rows'"
                ).fetchall():
                    self._con.execute(f"DROP TABLE IF EXISTS {t[0]}")
                self._con.execute(
                    f"CREATE TABLE schema_version (version INTEGER); INSERT INTO schema_version VALUES ({_SCHEMA_VERSION})"
                )
            else:
                stored = self._con.execute(
                    "SELECT version FROM schema_version"
                ).fetchone()
                if stored and stored[0] != _SCHEMA_VERSION:
                    for t in self._con.execute(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_name LIKE 'dice_%' OR table_name = 'impact_rows'"
                    ).fetchall():
                        self._con.execute(f"DROP TABLE IF EXISTS {t[0]}")
                    self._con.execute(
                        f"UPDATE schema_version SET version = {_SCHEMA_VERSION}"
                    )
            # The source path is configuration, so never retain an old persisted
            # view definition after a data-dir or migration change.
            self._con.execute(
                "CREATE OR REPLACE VIEW usage AS "
                f"SELECT * FROM read_parquet('{settings.data_dir / 'candidate_dataset.parquet'}')"
            )
            return self._con

    def _ingest(self) -> duckdb.DuckDBPyConnection:
        if self._ingest_con is None:
            self.connect()
            self._ingest_con = duckdb.connect(
                str(self._settings.cache_dir / "store.duckdb")
            )
        return self._ingest_con

    def _ensure_dice(self, dim: str) -> str:
        """Build (once) the dice table for a dimension. One scan, all facts.

        Takes the store lock so two concurrent first requests don't race the
        same CREATE TABLE; the build itself holds the lock (seconds on the
        firehose, once per dimension).
        """
        if dim not in _DIM_SQL:
            raise ValueError(f"unknown dimension: {dim}")
        table = f"dice_{dim}"
        if dim in self._built_dice:
            return table
        with self._lock:
            if dim in self._built_dice:
                return table
            con = self.con
            facts = ", ".join(f"{expr} AS {name}" for name, expr in _DICE_FACTS.items())
            con.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} AS
                SELECT {_DIM_SQL[dim]} AS dim_value, billing_period, {facts}
                FROM usage
                GROUP BY 1, 2
                """
            )
            self._built_dice.add(dim)
            return table

    def explore(self, params: ExploreParams) -> list[dict]:
        """Execute an aggregated query: dice when possible, capped scan otherwise."""
        con = self.con
        metrics = set(params.metrics)
        if metrics & set(_IMPACT_FACTS):
            return self._explore_impact(params)
        if not metrics <= set(_DICE_FACTS):
            raise ValueError(f"unknown metrics: {sorted(metrics - set(_DICE_FACTS))}")

        for dim in params.group_by:
            if dim not in _DIM_SQL:
                raise ValueError(f"unknown dimension: {dim}")
        for dim in params.filters or {}:
            if dim not in _DIM_SQL:
                raise ValueError(f"unknown filter dimension: {dim}")

        if (
            len(params.group_by) == 1
            and not params.filters
            and params.granularity == "month"
        ):
            table = self._ensure_dice(params.group_by[0])
            dim = params.group_by[0]
            where = ""
            if params.window:
                where = f"WHERE billing_period BETWEEN '{params.window[0]}' AND '{params.window[1]}'"
            facts = ", ".join(f"{name} AS {name}" for name in sorted(metrics))
            order_by = params.order_by or (
                sorted(metrics)[0] if metrics else "on_demand_cost"
            )
            sql = (
                f"SELECT dim_value AS {dim}, billing_period, {facts} "
                f"FROM {table} {where} ORDER BY {order_by} DESC LIMIT {params.top_n}"
            )
            rows = con.execute(sql).fetchall()
            cols = [dim, "billing_period", *sorted(metrics)]
            return [dict(zip(cols, r)) for r in rows]

        time_expr = (
            "billing_period"
            if params.granularity == "month"
            else "strftime(timestamp, '%Y-%m-%d')"
        )
        facts = ", ".join(
            f"SUM({_DICE_FACTS[m].split('(')[1].removesuffix(')')}) AS {m}"
            for m in sorted(metrics)
        )
        dims = ", ".join(f"{_DIM_SQL[d]} AS {d}" for d in params.group_by)
        select_parts = [p for p in ([dims, time_expr + " AS t", facts]) if p]
        group_parts = [p for p in ([dims, time_expr]) if p]
        where: list[str] = []
        if params.window:
            where.append(
                f"{time_expr} BETWEEN '{params.window[0]}' AND '{params.window[1]}'"
            )
        for dim, values in (params.filters or {}).items():
            lit = ", ".join(f"'{v}'" for v in values)
            where.append(f"{_DIM_SQL[dim]} IN ({lit})")
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        order_by = params.order_by or (
            sorted(metrics)[0] if metrics else "on_demand_cost"
        )
        sql = (
            f"SELECT {', '.join(select_parts)} FROM usage {where_sql} "
            f"GROUP BY {', '.join(group_parts)} ORDER BY {order_by} DESC LIMIT {params.top_n}"
        )
        rows = con.execute(sql).fetchall()
        cols = [c for c in ([*params.group_by, "t", *sorted(metrics)])]
        return [dict(zip(cols, r)) for r in rows]

    def _explore_impact(self, params: ExploreParams) -> list[dict]:
        """Aggregated query over impact savings dice (or capped raw impact scan)."""
        con = self.con
        metrics = sorted(set(params.metrics) & set(_IMPACT_FACTS))
        if (
            len(params.group_by) == 1
            and not params.filters
            and params.granularity == "month"
        ):
            dim = params.group_by[0]
            table = f"dice_{dim}_savings"
            try:
                con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            except Exception:
                raise RuntimeError("impact data not materialized yet; retry shortly")
            where = ""
            if params.window:
                where = f"WHERE billing_period BETWEEN '{params.window[0]}' AND '{params.window[1]}'"
            facts = ", ".join(f"{m} AS {m}" for m in metrics)
            sql = (
                f"SELECT dim_value AS {dim}, billing_period, {facts} "
                f"FROM {table} {where} ORDER BY {metrics[0]} DESC LIMIT {params.top_n}"
            )
            rows = con.execute(sql).fetchall()
            return [dict(zip([dim, "billing_period", *metrics], r)) for r in rows]
        raise NotImplementedError(
            "multi-dim impact cuts land after the explorer milestone"
        )

    # -- explorer-specific surfaces ----------------------------------------

    def periods(self) -> list[dict]:
        """The billing-period vocabulary with a `partial` flag per period."""
        cached = getattr(self, "_periods_cache", None)
        if cached:  # only trust a non-empty cache; empty means not yet populated
            return cached
        con = self.con
        rows = con.execute(
            """
            SELECT billing_period, MIN(timestamp) AS first_ts, MAX(timestamp) AS last_ts
            FROM usage GROUP BY 1 ORDER BY 1
            """
        ).fetchall()
        out = []
        for period, first_ts, last_ts in rows:
            year, month = int(period[:4]), int(period[5:7])
            last_day = calendar.monthrange(year, month)[1]
            partial = last_ts.date().day < last_day
            out.append(
                {
                    "period": period,
                    "partial": partial,
                    "first_ts": first_ts.isoformat(),
                    "last_ts": last_ts.isoformat(),
                }
            )
        if out:  # only cache when we have data
            self._periods_cache = out
        return out

    def complete_periods(self) -> list[str]:
        return [p["period"] for p in self.periods() if not p["partial"]]

    def delta(
        self,
        group_by: str,
        metric: str,
        window_a: tuple[str, str],
        window_b: tuple[str, str],
        top_n: int = 12,
    ) -> list[dict]:
        """Per-dimension sums in two windows, joined, with delta.

        This is the honest "what changed": both windows computed from the same
        dice, a full outer join so new and vanished values both appear, and the
        delta derived from the sums — never a model's guess.
        """
        if group_by not in _DIM_SQL:
            raise ValueError(f"unknown dimension: {group_by}")
        con = self.con
        if metric in _IMPACT_FACTS:
            table, col = f"dice_{group_by}_savings", metric
            try:
                con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            except Exception:
                raise RuntimeError("impact data not materialized yet; retry shortly")
        elif metric in _DICE_FACTS:
            table, col = self._ensure_dice(group_by), metric
        else:
            raise ValueError(f"unknown metric: {metric}")
        sql = f"""
        WITH a AS (
          SELECT dim_value, SUM({col}) AS va FROM {table}
          WHERE billing_period BETWEEN ? AND ? GROUP BY 1
        ),
        b AS (
          SELECT dim_value, SUM({col}) AS vb FROM {table}
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
        rows = con.execute(
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

    def slice(
        self,
        group_by: str,
        metric: str,
        window: tuple[str, str] | None,
        top_n: int = 12,
    ) -> list[dict]:
        """Simple grouped query for the explorer's default view.

        In SQL terms this is ``SELECT dimension, SUM(metric) ... GROUP BY
        dimension ORDER BY SUM(metric) DESC``. It reads the small dice table,
        not the 22M-row source parquet.
        """
        if group_by not in _DIM_SQL:
            raise ValueError(f"unknown dimension: {group_by}")
        con = self.con
        if metric in _IMPACT_FACTS:
            table, column = f"dice_{group_by}_savings", metric
            try:
                con.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            except Exception:
                raise RuntimeError("savings breakdown not ready; retry shortly")
        elif metric in _DICE_FACTS:
            table, column = self._ensure_dice(group_by), metric
        else:
            raise ValueError(f"unknown metric: {metric}")
        where, args = "", []
        if window:
            where = "WHERE billing_period BETWEEN ? AND ?"
            args = [window[0], window[1]]
        rows = con.execute(
            f"""
            SELECT dim_value, SUM({column}) AS value
            FROM {table} {where}
            GROUP BY 1 ORDER BY value DESC LIMIT {int(top_n)}
            """,
            args,
        ).fetchall()
        return [{group_by: r[0], "value": r[1]} for r in rows]

    def investigate(
        self,
        group_by: str,
        metric: str,
        period: str,
        comparison_period: str | None = None,
        selected_dimension: str | None = None,
        selected_value: str | None = None,
        top_n: int = 10,
    ) -> tuple[list[dict], str]:
        """Run one bounded, transparent agent query over usage spend.

        The caller supplies a validated query plan, never raw SQL. Common
        unfiltered questions use the small dice table; a query scoped to the
        current UI selection uses the raw view but stays limited to one period
        and at most 12 returned groups.
        """
        if group_by not in _DIM_SQL or metric not in _DICE_FACTS:
            raise ValueError("unsupported investigation dimension or metric")
        top_n = min(max(int(top_n), 1), 12)
        params: list[str] = []
        filter_sql = ""
        display_filter = ""
        if selected_dimension and selected_value:
            if selected_dimension not in _DIM_SQL:
                raise ValueError("unsupported selection filter")
            filter_sql = f" AND {_DIM_SQL[selected_dimension]} = ?"
            params.append(selected_value)
            escaped = selected_value.replace("'", "''")
            display_filter = f" AND {_DIM_SQL[selected_dimension]} = '{escaped}'"

        # A selection filter requires the detailed table. Without one, the
        # per-dimension dice is the exact same aggregation at lower cost.
        table = "usage" if filter_sql else self._ensure_dice(group_by)
        dim_expr = _DIM_SQL[group_by] if filter_sql else "dim_value"
        value_expr = f"SUM({metric})" if filter_sql else f"SUM({metric})"
        where_period = "billing_period = ?"

        if comparison_period is None:
            sql = (
                f"SELECT {dim_expr} AS {group_by}, {value_expr} AS value "
                f"FROM {table} WHERE {where_period}{filter_sql} "
                f"GROUP BY 1 ORDER BY value DESC LIMIT {top_n}"
            )
            rows = self.con.execute(sql, [period, *params]).fetchall()
            display_sql = (
                f"SELECT {dim_expr} AS {group_by}, {value_expr} AS value\n"
                f"FROM {table}\nWHERE billing_period = '{period}'{display_filter}\n"
                f"GROUP BY 1\nORDER BY value DESC\nLIMIT {top_n};"
            )
            return [{group_by: r[0], "value": r[1]} for r in rows], display_sql

        sql = f"""
        WITH earlier AS (
          SELECT {dim_expr} AS dim_value, {value_expr} AS earlier
          FROM {table} WHERE {where_period}{filter_sql} GROUP BY 1
        ), later AS (
          SELECT {dim_expr} AS dim_value, {value_expr} AS later
          FROM {table} WHERE {where_period}{filter_sql} GROUP BY 1
        )
        SELECT COALESCE(earlier.dim_value, later.dim_value) AS dim_value,
               COALESCE(earlier.earlier, 0) AS earlier,
               COALESCE(later.later, 0) AS later,
               COALESCE(later.later, 0) - COALESCE(earlier.earlier, 0) AS change
        FROM earlier FULL OUTER JOIN later ON earlier.dim_value = later.dim_value
        ORDER BY ABS(change) DESC LIMIT {top_n}
        """
        bound = [comparison_period, *params, period, *params]
        rows = self.con.execute(sql, bound).fetchall()
        display_sql = (
            f"WITH earlier AS (SELECT {dim_expr} AS dim_value, {value_expr} AS earlier\n"
            f"  FROM {table} WHERE billing_period = '{comparison_period}'{display_filter} GROUP BY 1),\n"
            f"later AS (SELECT {dim_expr} AS dim_value, {value_expr} AS later\n"
            f"  FROM {table} WHERE billing_period = '{period}'{display_filter} GROUP BY 1)\n"
            "SELECT COALESCE(earlier.dim_value, later.dim_value), earlier, later, later - earlier AS change\n"
            "FROM earlier FULL OUTER JOIN later ON earlier.dim_value = later.dim_value\n"
            f"ORDER BY ABS(change) DESC\nLIMIT {top_n};"
        )
        return [
            {group_by: r[0], "earlier": r[1], "later": r[2], "change": r[3]}
            for r in rows
        ], display_sql

    def materialize_impact_dice(self, group_by: str, rows: list[dict]) -> None:
        """Load one dimension's savings dice (already aggregated by the service)."""
        if group_by not in _DIM_SQL:
            raise ValueError(f"unknown dimension: {group_by}")
        con = self._ingest()
        con.execute(f"DROP TABLE IF EXISTS dice_{group_by}_savings")
        con.execute(
            f"""
            CREATE TABLE dice_{group_by}_savings (
                dim_value VARCHAR, billing_period VARCHAR,
                covered_on_demand_cost DOUBLE, committed_cost DOUBLE, gross_savings DOUBLE
            )
            """
        )
        con.executemany(
            f"INSERT INTO dice_{group_by}_savings VALUES (?,?,?,?,?)",
            [
                (
                    r["dim_value"],
                    r["billing_period"],
                    r["covered_on_demand_cost"],
                    r["committed_cost"],
                    r["gross_savings"],
                )
                for r in rows
            ],
        )

    def impact_ready(self) -> bool:
        """Whether the savings dice exist (i.e. /impact has been materialized)."""
        try:
            self.con.execute("SELECT 1 FROM dice_service_savings LIMIT 1").fetchone()
            return True
        except Exception:
            return False

    def drill(
        self, group_by: str, value: str, window: tuple[str, str] | None, limit: int = 30
    ) -> list[dict]:
        """Raw rows behind one aggregated value — bounded by LIMIT."""
        if group_by not in _DIM_SQL:
            raise ValueError(f"unknown dimension: {group_by}")
        limit = min(int(limit), self._settings.max_query_rows)
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
        where = f"WHERE {_DIM_SQL[group_by]} = ?"
        args: list = [value]
        if window:
            where += " AND billing_period BETWEEN ? AND ?"
            args += [window[0], window[1]]
        sql = f"SELECT {', '.join(cols)} FROM usage {where} ORDER BY on_demand_cost DESC LIMIT {limit}"
        rows = self.con.execute(sql, args).fetchall()
        return [dict(zip(cols, r)) for r in rows]
