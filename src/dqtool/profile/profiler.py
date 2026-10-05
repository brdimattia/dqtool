"""Column statistics over the full data, overall and per time period. No LLM."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import duckdb

from dqtool.config import DetectConfig
from dqtool.profile.source import Column, Source, quote

TOP_VALUES = 10


def _num(value: Any) -> Any:
    """Plain floats for JSON (DuckDB returns Decimal for some aggregates)."""
    if value is None or isinstance(value, bool | int | str | datetime | date):
        return value
    return float(value)


def _period_expr(source: Source) -> str:
    return f"date_trunc({source.config.period!r}, {quote(source.config.time_column)})"


def _numeric_stats(con: duckdb.DuckDBPyConnection, source: Source, col: Column) -> dict:
    c = quote(col.name)
    row = con.execute(
        f"""
        SELECT count({c}), min({c}), max({c}), avg({c}),
               quantile_cont({c}, [0.01, 0.25, 0.5, 0.75, 0.99]),
               mad({c}),
               avg(({c} < 0)::INT), avg(({c} = 0)::INT),
               mode({c}),
               median(ln(1 + greatest({c}, 0))) FILTER ({c} >= 0),
               mad(ln(1 + greatest({c}, 0))) FILTER ({c} >= 0)
        FROM {source.view}
        """
    ).fetchone()
    non_null, lo, hi, mean, q, mad, neg, zero, mode, log_med, log_mad = row
    stats = {
        "non_null": non_null,
        "null_rate": 1 - non_null / source.row_count if source.row_count else 0.0,
        "min": _num(lo),
        "max": _num(hi),
        "mean": _num(mean),
        "p01": _num(q[0]) if q else None,
        "p25": _num(q[1]) if q else None,
        "median": _num(q[2]) if q else None,
        "p75": _num(q[3]) if q else None,
        "p99": _num(q[4]) if q else None,
        "mad": _num(mad),
        "negative_share": _num(neg),
        "zero_share": _num(zero),
        "mode": _num(mode),
        "log1p_median": _num(log_med),
        "log1p_mad": _num(log_mad),
    }
    mode_count = (
        con.execute(f"SELECT count(*) FROM {source.view} WHERE {c} = ?", [mode]).fetchone()[0]
        if mode is not None
        else 0
    )
    stats["mode_share"] = mode_count / non_null if non_null else 0.0
    return stats


def _value_stats(con: duckdb.DuckDBPyConnection, source: Source, col: Column) -> dict:
    c = quote(col.name)
    non_null, distinct = con.execute(
        f"SELECT count({c}), count(DISTINCT {c}) FROM {source.view}"
    ).fetchone()
    top = con.execute(
        f"SELECT {c}, count(*) AS n FROM {source.view} WHERE {c} IS NOT NULL"
        f" GROUP BY 1 ORDER BY n DESC, 1 LIMIT {TOP_VALUES}"
    ).fetchall()
    stats = {
        "non_null": non_null,
        "null_rate": 1 - non_null / source.row_count if source.row_count else 0.0,
        "distinct": distinct,
        "top_values": [{"value": _num(v), "count": n} for v, n in top],
    }
    if col.is_text_type:
        length = con.execute(
            f"SELECT min(length({c})), median(length({c})), mad(length({c})), max(length({c}))"
            f" FROM {source.view}"
        ).fetchone()
        stats["length"] = dict(zip(("min", "median", "mad", "max"), map(_num, length), strict=True))
    if col.is_numeric_type:
        stats["min"], stats["max"] = map(
            _num, con.execute(f"SELECT min({c}), max({c}) FROM {source.view}").fetchone()
        )
    return stats


def _temporal_stats(con: duckdb.DuckDBPyConnection, source: Source, col: Column) -> dict:
    c = quote(col.name)
    non_null, lo, hi = con.execute(
        f"SELECT count({c}), min({c}), max({c}) FROM {source.view}"
    ).fetchone()
    return {
        "non_null": non_null,
        "null_rate": 1 - non_null / source.row_count if source.row_count else 0.0,
        "min": lo,
        "max": hi,
    }


def _time_range(con: duckdb.DuckDBPyConnection, source: Source, detect: DetectConfig) -> dict:
    """Periods holding at least min_period_share of rows form the data's main time range."""
    period = _period_expr(source)
    counts = con.execute(
        f"SELECT {period} AS p, count(*) FROM {source.view} WHERE p IS NOT NULL"
        " GROUP BY 1 ORDER BY 1"
    ).fetchall()
    core = [p for p, n in counts if n >= detect.min_period_share * source.row_count]
    if not core:
        return {"periods": {}, "core_periods": [], "start": None, "end": None}
    end = con.execute(
        f"SELECT ?::TIMESTAMP + INTERVAL 1 {source.config.period}", [core[-1]]
    ).fetchone()[0]
    return {
        "periods": {p.isoformat(): n for p, n in counts},
        "core_periods": [p.isoformat() for p in core],
        "start": core[0],
        "end": end,
    }


def _by_period(con: duckdb.DuckDBPyConnection, source: Source, core: list[str]) -> dict:
    """Per core period: row count, null rate per column, median and MAD per numeric column."""
    period = _period_expr(source)
    numeric = source.of_kind("numeric")
    parts = ["count(*)"]
    for col in source.columns:
        parts.append(f"avg(({quote(col.name)} IS NULL)::INT)")
    for col in numeric:
        parts += [f"median({quote(col.name)})", f"mad({quote(col.name)})"]
    rows = con.execute(
        f"SELECT strftime({period}, '%Y-%m-%dT%H:%M:%S') AS p, {', '.join(parts)}"
        f" FROM {source.view} WHERE p IN (SELECT unnest($core)) GROUP BY 1 ORDER BY 1",
        {"core": core},
    ).fetchall()
    out = {}
    for p, n, *rest in rows:
        nulls = rest[: len(source.columns)]
        medians = rest[len(source.columns) :]
        out[p] = {
            "rows": n,
            "null_rate": {c.name: _num(v) for c, v in zip(source.columns, nulls, strict=True)},
            "median": {c.name: _num(medians[2 * i]) for i, c in enumerate(numeric)},
            "mad": {c.name: _num(medians[2 * i + 1]) for i, c in enumerate(numeric)},
        }
    return out


def profile_source(con: duckdb.DuckDBPyConnection, source: Source, detect: DetectConfig) -> dict:
    columns = {}
    for col in source.columns:
        if col.kind == "numeric":
            stats = _numeric_stats(con, source, col)
        elif col.kind == "temporal":
            stats = _temporal_stats(con, source, col)
        elif col.kind == "id":
            stats = _temporal_stats(con, source, col)  # null rate, min, max
        else:
            stats = _value_stats(con, source, col)
        columns[col.name] = {
            "type": col.sql_type,
            "kind": col.kind,
            "derived": col.derived,
            **stats,
        }

    profile: dict = {
        "source": source.name,
        "path": str(source.config.path),
        "row_count": source.row_count,
        "row_id": source.row_id,
        "derived": source.config.derived,
        "columns": columns,
    }
    if source.config.time_column:
        time_range = _time_range(con, source, detect)
        profile["time_column"] = source.config.time_column
        profile["period"] = source.config.period
        profile["time_range"] = time_range
        profile["by_period"] = _by_period(con, source, [p for p in time_range["core_periods"]])
    return profile
