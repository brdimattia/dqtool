from __future__ import annotations

import json

import duckdb
import pytest
from conftest import plant_errors
from test_propose import FakeProvider, scan
from test_scan import SYNTHETIC_DETECT, planted_config

from dqtool.prove.backtest import _timed, backtest_rule, sandbox
from dqtool.prove.score import score
from dqtool.scan import reprove


def make_rule(predicate: str, source: str = "nyc_taxi_trips", rule_id: str = "r") -> dict:
    return {
        "id": rule_id,
        "source": source,
        "violation_when": predicate,
        "status": "proposed",
        "max_violation_rate": 0.01,
        "evidence": {},
        "provenance": {},
    }


@pytest.fixture(scope="module")
def setup(synthetic_planted, tmp_path_factory):
    cfg = planted_config(synthetic_planted, **SYNTHETIC_DETECT)
    result = scan(synthetic_planted, tmp_path_factory.mktemp("runs"), FakeProvider())
    con, sources = sandbox(cfg, list(cfg.sources))
    yield cfg, con, sources, result
    con.close()


def bt(setup, predicate, source="nyc_taxi_trips"):
    cfg, con, sources, result = setup
    return backtest_rule(
        con, sources[source], result.profiles[source], make_rule(predicate, source), cfg
    )


# --- Sandbox -----------------------------------------------------------------


def test_sandbox_reads_sources_only(setup, synthetic_planted):
    _, con, _, _ = setup
    assert con.execute("SELECT count(*) FROM nyc_taxi_trips").fetchone()[0] > 0
    with pytest.raises(duckdb.PermissionException):
        con.execute(f"SELECT * FROM read_json('{synthetic_planted / plant_errors.MANIFEST_FILE}')")
    with pytest.raises(duckdb.Error):
        con.execute("SET enable_external_access = true")


def test_timeout_interrupts_long_queries(setup):
    _, con, _, _ = setup
    with pytest.raises(duckdb.InterruptException):
        _timed(con, "SELECT count(*) FROM range(100000000000) a", 0.2)


# --- Backtest outcomes -------------------------------------------------------


def test_precise_rule_survives_with_evidence(setup):
    out = bt(setup, "passenger_count = 87")
    assert out["status"] == "survived" and out["kind"] == "row"
    assert 0 < out["backtest_violation_rate"] <= 0.01
    assert out["sample_row_ids"] and out["violations_by_period"]


def test_noisy_rule_is_dropped(setup):
    assert bt(setup, "fare_amount > 0")["status"] == "dropped_noisy"


def test_small_table_rule_is_not_noisy(setup):
    # 3 of 26 synthetic zones is 11.5%, but only 3 rows.
    out = bt(setup, "LocationID IN (3, 4, 7)", source="nyc_taxi_zones")
    assert out["status"] == "survived"


def test_failing_rule_is_dropped(setup):
    out = bt(setup, "fare_amount > CAST('abc' AS DOUBLE)")
    assert out["status"] == "dropped_failed" and "Conversion" in out["error"]


def test_guard_is_rechecked(setup):
    assert bt(setup, "fare_amount > 0; DROP TABLE x")["status"] == "rejected_by_sql_guard"


def test_period_rule_is_exempt_from_noise_threshold(setup):
    out = bt(
        setup,
        "tpep_pickup_datetime >= TIMESTAMP '2025-02-01'"
        " AND tpep_pickup_datetime < TIMESTAMP '2025-03-01'",
    )
    assert out["status"] == "survived" and out["kind"] == "period"
    assert out["period"] == "2025-02-01" and out["backtest_violation_rate"] > 0.3


# --- Scoring -----------------------------------------------------------------


def test_score_counts_caught_plants(setup, synthetic_planted):
    cfg, _, _, _ = setup
    manifest = json.loads((synthetic_planted / plant_errors.MANIFEST_FILE).read_text())
    drift_month = next(p["month"] for p in manifest["plants"] if p["type"] == "statistical")
    y, m = map(int, drift_month.split("-"))
    nxt = f"{y + (m == 12)}-{m % 12 + 1:02d}"
    rules = []
    for i, pred in enumerate(
        [
            "passenger_count = 87",
            "tpep_dropoff_datetime < tpep_pickup_datetime",
            f"tpep_pickup_datetime >= TIMESTAMP '{drift_month}-01'"
            f" AND tpep_pickup_datetime < TIMESTAMP '{nxt}-01'",
            "fare_amount > 1000000",  # flags nothing planted
        ]
    ):
        r = make_rule(pred, rule_id=f"r{i}")
        r["status"] = "survived"
        r["evidence"]["backtest_violation_count"] = duckdb.execute(
            f"SELECT count(*) FROM '{synthetic_planted / plant_errors.TRIPS_FILE}' WHERE {pred}"
        ).fetchone()[0]
        rules.append(r)
    canary = next(p for p in manifest["plants"] if p["type"] == "injection_canary")
    zone_rule = make_rule(f"LocationID = {canary['zone_ids'][0]}", "nyc_taxi_zones", "z")
    zone_rule["status"] = "survived"
    rules.append(zone_rule)

    results = score(cfg, manifest, rules, {"spent_usd": 0.1})
    caught = {p["plant_id"] for p in results["plants"] if p["caught"]}
    assert caught == {
        "single_passenger_count_87",
        "cross_dropoff_before_pickup",
        "statistical_distance_doubled",
        "injection_canary_zone_name",
    }
    m = results["metrics"]
    assert m["planted_recall_by_type"] == pytest.approx(4 / 5)  # semantic missing
    assert m["planted_recall_by_plant"] == pytest.approx(4 / 10)
    assert m["rules_hitting_plants"] == 4 and m["rules_novel_candidates"] == 1
    assert results["targets"]["cost_under_budget"]["met"]


# --- End to end --------------------------------------------------------------


def test_scan_writes_rules_and_results(synthetic_planted, tmp_path):
    cfg = planted_config(synthetic_planted, **SYNTHETIC_DETECT)
    from dqtool.scan import run_scan

    result = run_scan(
        cfg,
        out_dir=tmp_path,
        provider=FakeProvider(),
        manifest_path=synthetic_planted / plant_errors.MANIFEST_FILE,
    )
    files = {p.name for p in result.run_dir.iterdir()}
    assert {"config.json", "rules.json", "results.json"} <= files
    assert all(r["status"] != "proposed" for r in result.rules)
    history = (tmp_path / "results.jsonl").read_text().splitlines()
    assert len(history) == 1 and json.loads(history[0])["run_id"] == result.run_id

    # Re-proving the same run gives the same results, with no LLM calls.
    rules, results = reprove(result.run_dir, synthetic_planted / plant_errors.MANIFEST_FILE)
    assert results["metrics"] == result.results["metrics"]
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 2
