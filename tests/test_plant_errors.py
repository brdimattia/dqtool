from __future__ import annotations

import json

import duckdb
import pytest
from conftest import plant_errors

SPEC_TYPES = {"single_column", "cross_column", "statistical", "semantic", "injection_canary"}


def row_plants(manifest: dict) -> list[dict]:
    return [p for p in manifest["plants"] if "row_ids" in p]


def zone_plants(manifest: dict) -> list[dict]:
    return [p for p in manifest["plants"] if "zone_ids" in p]


def statistical_plant(manifest: dict) -> dict:
    (plant,) = [p for p in manifest["plants"] if p["type"] == "statistical"]
    return plant


def ids_matching(con: duckdb.DuckDBPyConnection, table: str, key: str, predicate: str) -> set:
    return {r[0] for r in con.execute(f"SELECT {key} FROM {table} WHERE {predicate}").fetchall()}


# --- Determinism -------------------------------------------------------------


def test_same_seed_gives_identical_output(synthetic_raw, synthetic_planted, tmp_path):
    again = tmp_path / "again"
    plant_errors.plant(synthetic_raw, again, seed=42)

    for name in (plant_errors.MANIFEST_FILE, plant_errors.ZONES_FILE):
        assert (again / name).read_bytes() == (synthetic_planted / name).read_bytes(), name

    con = duckdb.connect()
    a, b = synthetic_planted / plant_errors.TRIPS_FILE, again / plant_errors.TRIPS_FILE
    assert (
        con.execute(f"SELECT count(*) FROM '{a}'").fetchone()
        == con.execute(f"SELECT count(*) FROM '{b}'").fetchone()
    )
    diff = con.execute(
        f"SELECT count(*) FROM ((FROM '{a}' EXCEPT ALL FROM '{b}')"
        f" UNION ALL (FROM '{b}' EXCEPT ALL FROM '{a}'))"
    ).fetchone()[0]
    assert diff == 0


def test_different_seed_gives_different_plants(synthetic_raw, synthetic_planted, tmp_path):
    other = plant_errors.plant(synthetic_raw, tmp_path / "other", seed=7)
    base = json.loads((synthetic_planted / plant_errors.MANIFEST_FILE).read_text())
    assert [p.get("row_ids") for p in other["plants"]] != [p.get("row_ids") for p in base["plants"]]


def test_raw_data_is_not_modified(synthetic_raw, tmp_path):
    before = {f.name: plant_errors.sha256(f) for f in synthetic_raw.iterdir()}
    plant_errors.plant(synthetic_raw, tmp_path / "out", seed=1)
    after = {f.name: plant_errors.sha256(f) for f in synthetic_raw.iterdir()}
    assert before == after


def test_refuses_to_write_into_raw_dir(synthetic_raw):
    with pytest.raises(SystemExit):
        plant_errors.plant(synthetic_raw, synthetic_raw / "planted", seed=42)


# --- Manifest shape ----------------------------------------------------------


def test_manifest_covers_spec_catalog(dataset):
    _, _, manifest = dataset
    assert {p["type"] for p in manifest["plants"]} == SPEC_TYPES
    ids = [p["plant_id"] for p in manifest["plants"]]
    assert len(ids) == len(set(ids))
    for p in manifest["plants"]:
        assert {"plant_id", "type", "description", "predicate", "table"} <= p.keys()
        assert sum(k in p for k in ("row_ids", "month", "zone_ids")) == 1


def test_row_plants_are_small_and_disjoint(dataset):
    _, _, manifest = dataset
    seen: set[int] = set()
    for p in row_plants(manifest):
        low, high = plant_errors.ROW_PLANT_SIZE
        assert low <= len(p["row_ids"]) <= high
        assert p["row_count"] == len(p["row_ids"])
        assert not seen & set(p["row_ids"]), p["plant_id"]
        seen |= set(p["row_ids"])


def test_trip_row_count_and_ids_preserved(db, dataset):
    _, _, manifest = dataset
    count, distinct, lo, hi = db.execute(
        "SELECT count(*), count(DISTINCT row_id), min(row_id), max(row_id) FROM trips"
    ).fetchone()
    assert count == distinct == hi == manifest["trip_rows"]
    assert lo == 1


# --- Every manifest entry is present in the planted data ---------------------


def test_row_plants_changed_exactly_their_rows(db, dataset):
    """Planted columns differ from raw on the planted rows and nowhere else."""
    _, _, manifest = dataset
    stat = statistical_plant(manifest)
    for p in row_plants(manifest):
        differs = " OR ".join(f"t.{c} IS DISTINCT FROM r.{c}" for c in p["columns"])
        # The statistical plant also doubled trip_distance; compare against that.
        if "trip_distance" in p["columns"]:
            differs = differs.replace(
                "r.trip_distance",
                f"(CASE WHEN {stat['predicate'].replace('tpep', 'r.tpep')}"
                " THEN r.trip_distance * 2 ELSE r.trip_distance END)",
            )
        changed = {
            r[0]
            for r in db.execute(
                f"SELECT t.row_id FROM trips t JOIN raw_trips r USING (row_id) WHERE {differs}"
            ).fetchall()
        }
        others = set().union(*(set(o["row_ids"]) for o in row_plants(manifest) if o is not p))
        assert set(p["row_ids"]) <= changed, p["plant_id"]
        assert changed - others <= set(p["row_ids"]), p["plant_id"]


def test_statistical_plant_doubled_its_month(db, dataset):
    _, _, manifest = dataset
    stat = statistical_plant(manifest)
    planted_rows = {r for p in row_plants(manifest) for r in p["row_ids"]}
    raw_pred = stat["predicate"].replace("tpep", "r.tpep")
    in_month = db.execute(
        f"""
        SELECT count(*),
               count(*) FILTER (t.trip_distance IS NOT DISTINCT FROM r.trip_distance * 2)
        FROM trips t JOIN raw_trips r USING (row_id)
        WHERE {raw_pred} AND row_id NOT IN (SELECT unnest($ids))
        """,
        {"ids": sorted(planted_rows)},
    ).fetchone()
    assert in_month[0] > 0 and in_month[0] == in_month[1]
    assert (
        db.execute(f"SELECT count(*) FROM raw_trips r WHERE {raw_pred}").fetchone()[0]
        == stat["row_count"]
    )

    month = stat["month"]
    raw_avg = stat["avg_trip_distance_by_month_raw"][month]
    planted_avg = stat["avg_trip_distance_by_month_planted"][month]
    assert planted_avg == pytest.approx(2 * raw_avg, rel=1e-3)
    for other, avg in stat["avg_trip_distance_by_month_raw"].items():
        if other != month:
            assert stat["avg_trip_distance_by_month_planted"][other] == avg


def test_zone_plants_present(db, dataset):
    _, _, manifest = dataset
    changed_ids = set()
    for p in zone_plants(manifest):
        for c in p["changes"]:
            field = c["field"]
            raw = db.execute(
                f"SELECT {field} FROM raw_zones WHERE LocationID = ?", [c["LocationID"]]
            ).fetchone()[0]
            planted = db.execute(
                f"SELECT {field} FROM zones WHERE LocationID = ?", [c["LocationID"]]
            ).fetchone()[0]
            assert raw == c["original"] != planted == c["planted"], p["plant_id"]
            changed_ids.add(c["LocationID"])
    unchanged = db.execute(
        "SELECT count(*) FROM (FROM raw_zones WHERE LocationID NOT IN (SELECT unnest($ids))"
        " EXCEPT ALL FROM zones)",
        {"ids": sorted(changed_ids)},
    ).fetchone()[0]
    assert unchanged == 0
    assert (
        db.execute("SELECT count(*) FROM zones").fetchone()
        == db.execute("SELECT count(*) FROM raw_zones").fetchone()
    )


# --- Each manifest predicate flags its planted rows --------------------------


def test_row_predicates_flag_planted_rows(db, dataset):
    _, _, manifest = dataset
    for p in row_plants(manifest):
        flagged = ids_matching(db, "trips", "row_id", p["predicate"])
        assert set(p["row_ids"]) <= flagged, p["plant_id"]
        assert len(flagged) == p["predicate_matches_total"]


def test_statistical_predicate_selects_drift_month(db, dataset):
    _, _, manifest = dataset
    stat = statistical_plant(manifest)
    planted_rows = {r for p in row_plants(manifest) for r in p["row_ids"]}
    flagged = ids_matching(db, "trips", "row_id", stat["predicate"]) - planted_rows
    expected = (
        ids_matching(db, "raw_trips r", "row_id", stat["predicate"].replace("tpep", "r.tpep"))
        - planted_rows
    )
    assert flagged == expected


def test_zone_predicates_flag_planted_zones(db, dataset):
    _, _, manifest = dataset
    for p in zone_plants(manifest):
        flagged = ids_matching(db, "zones", "LocationID", p["predicate"])
        assert flagged == set(p["zone_ids"]), p["plant_id"]
        raw_flagged = ids_matching(db, "raw_zones", "LocationID", p["predicate"])
        assert not raw_flagged, p["plant_id"]
