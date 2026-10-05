"""Prove step: run every proposed rule against the full data in a sandbox.

The sandbox is a fresh in-memory DuckDB connection that can read only the
source files, with all other file, network, and extension access disabled and
its configuration locked, plus a memory limit and a per-rule time limit (spec:
Security, layer 5). Rules that fail to run or flag too much data are dropped.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import duckdb

from dqtool.config import Config
from dqtool.profile.source import Source, quote, register_source
from dqtool.safety.sql_guard import check_predicate


def sandbox(
    config: Config, names: list[str]
) -> tuple[duckdb.DuckDBPyConnection, dict[str, Source]]:
    """A locked-down connection with the named sources registered as views."""
    con = duckdb.connect()
    sources = {n: register_source(con, n, config.sources[n], config.detect) for n in names}
    paths = [str(Path(config.sources[n].path).resolve()) for n in names]
    paths += [str(config.sources[n].path) for n in names]
    con.execute("SET allowed_paths = $p", {"p": paths})
    con.execute(f"SET memory_limit = '{config.prove.memory_limit}'")
    con.execute("SET autoinstall_known_extensions = false")
    con.execute("SET autoload_known_extensions = false")
    con.execute("SET enable_external_access = false")
    con.execute("SET lock_configuration = true")
    return con, sources


def _timed(con: duckdb.DuckDBPyConnection, sql: str, timeout: float) -> list:
    timer = threading.Timer(timeout, con.interrupt)
    timer.start()
    try:
        return con.execute(sql).fetchall()
    finally:
        timer.cancel()


def backtest_rule(
    con: duckdb.DuckDBPyConnection, source: Source, profile: dict, rule: dict, config: Config
) -> dict:
    """Backtest one rule; returns the evidence and the resulting status."""
    cfg = config.prove
    # Re-check: the rule may have been edited since it was proposed.
    guard = check_predicate(rule["violation_when"], [c.name for c in source.columns])
    if not guard.ok:
        return {"status": "rejected_by_sql_guard", "error": "; ".join(guard.errors)}

    pred = rule["violation_when"]
    started = time.perf_counter()
    try:
        (count,) = _timed(
            con, f"SELECT count(*) FROM {source.view} WHERE {pred}", cfg.timeout_seconds
        )[0]
        samples = []
        if source.row_id and count:
            rid = quote(source.row_id)
            samples = [
                r[0]
                for r in _timed(
                    con,
                    f"SELECT {rid} FROM {source.view} WHERE {pred}"
                    f" ORDER BY hash({rid}), {rid} LIMIT {config.detect.sample_rows}",
                    cfg.timeout_seconds,
                )
            ]
        periods = {}
        time_col = source.config.time_column
        if time_col and count:
            unit = source.config.period
            periods = {
                p: n
                for p, n in _timed(
                    con,
                    f"SELECT strftime(date_trunc('{unit}', {quote(time_col)}), '%Y-%m-%d'),"
                    f" count(*) FROM {source.view} WHERE {pred} GROUP BY 1 ORDER BY 1",
                    cfg.timeout_seconds,
                )
            }
    except duckdb.InterruptException:
        return {"status": "dropped_failed", "error": f"timed out after {cfg.timeout_seconds}s"}
    except duckdb.Error as e:
        return {
            "status": "dropped_failed",
            "error": f"{type(e).__name__}: {str(e).splitlines()[0]}",
        }

    rate = count / source.row_count if source.row_count else 0.0
    evidence = {
        "backtest_violation_rate": rate,
        "backtest_violation_count": count,
        "sample_row_ids": samples,
        "violations_by_period": periods,
        "backtest_seconds": round(time.perf_counter() - started, 3),
    }
    top_period, top = max(periods.items(), key=lambda kv: kv[1]) if periods else (None, 0)
    core = {p[:10] for p in (profile.get("time_range") or {}).get("core_periods", [])}
    is_period_rule = (
        count > 0
        and top_period in core
        and top / count >= cfg.period_rule_min_share
        and time_col in guard.columns
    )
    evidence["kind"] = "period" if is_period_rule else "row"
    if is_period_rule:
        evidence["period"] = top_period
        status = "survived"
    else:
        max_rate = rule.get("max_violation_rate", cfg.max_violation_rate)
        noisy = rate > max_rate and count > cfg.noise_min_rows
        status = "dropped_noisy" if noisy else "survived"
    return {"status": status, **evidence}


def prove(config: Config, proposals: list[dict], profiles: dict[str, dict]) -> list[dict]:
    """Backtest every rule in the proposals. Returns the rules with evidence and final status."""
    names = [p["source"] for p in proposals]
    con, sources = sandbox(config, names)
    rules = []
    try:
        for p in proposals:
            source = sources[p["source"]]
            for rule in p["rules"]:
                if rule["status"] == "rejected_by_sql_guard":
                    rules.append(rule)
                    continue
                outcome = backtest_rule(con, source, profiles[p["source"]], rule, config)
                status = outcome.pop("status")
                rules.append(
                    {
                        **rule,
                        "status": status,
                        "evidence": {**rule["evidence"], **outcome},
                    }
                )
    finally:
        con.close()
    return rules
