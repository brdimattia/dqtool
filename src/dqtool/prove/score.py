"""Score surviving rules against the planted-error manifest (spec: Evaluation).

A plant is caught when the surviving rules on its source, taken together, flag
at least CAUGHT_SHARE of its planted rows or zones. The statistical (drift)
plant is caught when one surviving rule concentrates on the planted month:
at least DRIFT_CONCENTRATION of its flags fall in that month, and it flags at
least DRIFT_COVERAGE of that month's rows.

Recall is reported two ways: by type, the spec's headline metric (a type counts
when at least one of its plants is caught), and by plant.
"""

from __future__ import annotations

import duckdb

from dqtool.config import Config
from dqtool.profile.source import quote
from dqtool.prove.backtest import sandbox

CAUGHT_SHARE = 0.9
DRIFT_CONCENTRATION = 0.9
DRIFT_COVERAGE = 0.1
# A rule counts toward a plant (and as hitting plants) when it flags this share of its rows.
CONTRIBUTION_SHARE = 0.1
RECALL_TARGET = 0.9


def _source_for(table: str, manifest: dict, config: Config, names: list[str]) -> str | None:
    filename = manifest["outputs"][table]
    return next((n for n in names if config.sources[n].path.name == filename), None)


def _flagged(
    con: duckdb.DuckDBPyConnection, view: str, key: str, ids: list, preds: list[str]
) -> int:
    if not preds:
        return 0
    union = " OR ".join(f"({p})" for p in preds)
    return con.execute(
        f"SELECT count(*) FROM {view} WHERE {quote(key)} IN (SELECT unnest($ids)) AND ({union})",
        {"ids": ids},
    ).fetchone()[0]


def score(config: Config, manifest: dict, rules: list[dict], cost: dict | None) -> dict:
    names = sorted({r["source"] for r in rules} | set(config.sources))
    names = [n for n in names if config.sources[n].path.exists()]
    con, sources = sandbox(config, names)
    survived = [r for r in rules if r["status"] == "survived"]
    plants = []
    hit_rules: set[str] = set()
    try:
        for plant in manifest["plants"]:
            name = _source_for(plant["table"], manifest, config, names)
            entry = {
                "plant_id": plant["plant_id"],
                "type": plant["type"],
                "source": name,
                "caught": False,
                "recall": 0.0,
                "rules": [],
            }
            plants.append(entry)
            if name is None:
                continue
            source = sources[name]
            mine = [r for r in survived if r["source"] == name]

            if "month" in plant:
                time_col = quote(source.config.time_column)
                month_sql = f"strftime({time_col}, '%Y-%m') = '{plant['month']}'"
                month_rows = con.execute(
                    f"SELECT count(*) FROM {source.view} WHERE {month_sql}"
                ).fetchone()[0]
                best = 0.0
                for r in mine:
                    total = r["evidence"].get("backtest_violation_count") or 0
                    if not total or not month_rows:
                        continue
                    in_month = con.execute(
                        f"SELECT count(*) FROM {source.view}"
                        f" WHERE {month_sql} AND ({r['violation_when']})"
                    ).fetchone()[0]
                    concentration, coverage = in_month / total, in_month / month_rows
                    if concentration >= DRIFT_CONCENTRATION and coverage >= DRIFT_COVERAGE:
                        entry["rules"].append(r["id"])
                        hit_rules.add(r["id"])
                        best = max(best, coverage)
                entry["caught"] = bool(entry["rules"])
                entry["recall"] = 1.0 if entry["caught"] else 0.0
                entry["month_coverage"] = best
                continue

            ids = plant.get("row_ids") or plant.get("zone_ids")
            key = source.row_id
            for r in mine:
                share = _flagged(con, source.view, key, ids, [r["violation_when"]]) / len(ids)
                if share >= CONTRIBUTION_SHARE:
                    entry["rules"].append(r["id"])
                    hit_rules.add(r["id"])
            n = _flagged(con, source.view, key, ids, [r["violation_when"] for r in mine])
            entry["recall"] = n / len(ids)
            entry["caught"] = entry["recall"] >= CAUGHT_SHARE
    finally:
        con.close()

    types: dict[str, bool] = {}
    for p in plants:
        types[p["type"]] = types.get(p["type"], False) or p["caught"]
    type_recall = sum(types.values()) / len(types) if types else 0.0
    plant_recall = sum(p["caught"] for p in plants) / len(plants) if plants else 0.0
    budget = config.budget.max_usd_per_scan
    spent = cost["spent_usd"] if cost else 0.0
    by_status: dict[str, int] = {}
    for r in rules:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {
        "metrics": {
            "planted_recall_by_type": type_recall,
            "planted_recall_by_plant": plant_recall,
            "mean_plant_recall": sum(p["recall"] for p in plants) / len(plants) if plants else 0.0,
            "cost_usd": spent,
            "rules_total": len(rules),
            "rules_by_status": by_status,
            "rules_surviving": len(survived),
            "rules_hitting_plants": len(hit_rules),
            # Surviving rules that hit no plant: real-data findings or false alarms, for the
            # reviewer to sort out (spec metric 4, novel findings).
            "rules_novel_candidates": len(survived) - len(hit_rules),
        },
        "targets": {
            "planted_recall_by_type": {
                "target": RECALL_TARGET,
                "met": type_recall >= RECALL_TARGET,
            },
            "cost_under_budget": {"target": budget, "met": spent <= budget},
            "reviewer_precision": {"target": 0.7, "met": None, "note": "measured in step 5 review"},
        },
        "types": types,
        "plants": plants,
    }
