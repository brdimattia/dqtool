"""Which planted errors the detector's slices surface. A light precursor to step 4 scoring.

A row plant counts as surfaced when one slice's predicate matches at least
ROW_COVERAGE of its planted rows. A zone plant counts when one slice matches all
of its zones. The statistical plant counts when a drift slice names its month
and one of its columns. Semantic plants are listed but not expected: they are
left to the LLM in step 3.
"""

from __future__ import annotations

import duckdb

from dqtool.detect.detectors import Slice
from dqtool.profile.source import Source, quote

ROW_COVERAGE = 0.9
NOT_EXPECTED = {"semantic"}


def _source_for(table: str, manifest: dict, sources: dict[str, Source]) -> Source | None:
    """Match a manifest table (trips, zones) to the configured source reading that file."""
    filename = manifest["outputs"][table]
    for source in sources.values():
        if source.config.path.name == filename:
            return source
    return None


def coverage(
    con: duckdb.DuckDBPyConnection,
    manifest: dict,
    sources: dict[str, Source],
    slices: list[Slice],
) -> list[dict]:
    results = []
    for plant in manifest["plants"]:
        source = _source_for(plant["table"], manifest, sources)
        result = {
            "plant_id": plant["plant_id"],
            "type": plant["type"],
            "expected": plant["type"] not in NOT_EXPECTED,
            "source": source.name if source else None,
            "surfaced": False,
            "best_fraction": 0.0,
            "slices": [],
        }
        results.append(result)
        if source is None:
            continue
        own = [s for s in slices if s.source == source.name]

        if "month" in plant:
            hits = [
                s
                for s in own
                if s.detector.startswith("drift")
                and s.stats.get("period", "").startswith(plant["month"])
                and set(s.columns) & set(plant["columns"])
            ]
            result["slices"] = [s.slice_id for s in hits]
            result["surfaced"] = bool(hits)
            result["best_fraction"] = 1.0 if hits else 0.0
            continue

        ids = plant.get("row_ids") or plant.get("zone_ids")
        rid = quote(source.row_id)
        con.execute(
            f"CREATE OR REPLACE TEMP TABLE planted_rows AS SELECT * FROM {source.view}"
            f" WHERE {rid} IN (SELECT unnest($ids))",
            {"ids": ids},
        )
        needed = ROW_COVERAGE if "row_ids" in plant else 1.0
        scored = []
        for s in own:
            matched = con.execute(
                f"SELECT count(*) FROM planted_rows WHERE {s.predicate}"
            ).fetchone()[0]
            if matched:
                scored.append((matched / len(ids), s.flag_count, s.slice_id))
        # Best coverage first; among equals, the most specific slice (fewest rows flagged).
        scored.sort()
        scored.sort(key=lambda x: -x[0])
        result["best_fraction"] = scored[0][0] if scored else 0.0
        result["surfaced"] = result["best_fraction"] >= needed
        result["slices"] = [sid for frac, _, sid in scored if frac >= needed] or [
            sid for _, _, sid in scored[:3]
        ]
    return results
