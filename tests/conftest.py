from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plant_errors = _load_script("plant_errors")

SYNTHETIC_ROWS_PER_MONTH = 20_000

# A slice of the real zone lookup, including the non-borough rows.
SYNTHETIC_ZONES = """LocationID,Borough,Zone,service_zone
1,EWR,Newark Airport,EWR
3,Bronx,Allerton/Pelham Gardens,Boro Zone
4,Manhattan,Alphabet City,Yellow Zone
7,Queens,Astoria,Boro Zone
11,Brooklyn,Bath Beach,Boro Zone
13,Manhattan,Battery Park City,Yellow Zone
14,Brooklyn,Bay Ridge,Boro Zone
23,Staten Island,Bloomfield/Emerson Hill,Boro Zone
25,Brooklyn,Boerum Hill,Boro Zone
42,Manhattan,Central Harlem North,Boro Zone
48,Manhattan,Clinton East,Yellow Zone
61,Brooklyn,Crown Heights North,Boro Zone
69,Bronx,East Concourse/Concourse Village,Boro Zone
79,Manhattan,East Village,Yellow Zone
95,Queens,Forest Hills,Boro Zone
132,Queens,JFK Airport,Airports
138,Queens,LaGuardia Airport,Airports
156,Staten Island,Mariners Harbor,Boro Zone
161,Manhattan,Midtown Center,Yellow Zone
168,Bronx,Mott Haven/Port Morris,Boro Zone
181,Brooklyn,Park Slope,Boro Zone
226,Queens,Sunnyside,Boro Zone
230,Manhattan,Times Sq/Theatre District,Yellow Zone
245,Staten Island,West Brighton,Boro Zone
264,Unknown,N/A,N/A
265,N/A,Outside of NYC,N/A
"""


def write_synthetic_raw(raw_dir: Path) -> None:
    """Three months of deterministic trips with the real TLC yellow schema."""
    raw_dir.mkdir(parents=True)
    (raw_dir / "taxi_zone_lookup.csv").write_text(SYNTHETIC_ZONES)
    con = duckdb.connect()
    for month in ("2025-01", "2025-02", "2025-03"):
        n = SYNTHETIC_ROWS_PER_MONTH
        con.execute(
            f"""
            COPY (
              SELECT
                (1 + i % 2)::INTEGER AS VendorID,
                TIMESTAMP '{month}-01' + to_seconds((i * 127) % (27 * 86400))
                  AS tpep_pickup_datetime,
                TIMESTAMP '{month}-01' + to_seconds((i * 127) % (27 * 86400) + 300 + i % 2400)
                  AS tpep_dropoff_datetime,
                CASE WHEN i % 50 = 0 THEN NULL ELSE 1 + i % 4 END::BIGINT AS passenger_count,
                round(0.3 + (i % 97) / 7.0, 2) AS trip_distance,
                1::BIGINT AS RatecodeID,
                'N' AS store_and_fwd_flag,
                (1 + i % 265)::INTEGER AS PULocationID,
                (1 + (i * 7) % 265)::INTEGER AS DOLocationID,
                (1 + i % 3)::BIGINT AS payment_type,
                round(4 + (i % 97) * 2.1, 2) AS fare_amount,
                1.0 AS extra,
                0.5 AS mta_tax,
                round((i % 5) * 1.25, 2) AS tip_amount,
                0.0 AS tolls_amount,
                1.0 AS improvement_surcharge,
                round(4 + (i % 97) * 2.1 + 2.5 + (i % 5) * 1.25, 2) AS total_amount,
                2.5 AS congestion_surcharge,
                0.0 AS Airport_fee,
                0.0 AS cbd_congestion_fee
              FROM range({n}) AS r(i)
            ) TO '{raw_dir / f"yellow_tripdata_{month}.parquet"}' (FORMAT parquet)
            """
        )
    con.close()


@pytest.fixture(scope="session")
def synthetic_raw(tmp_path_factory) -> Path:
    raw = tmp_path_factory.mktemp("synthetic") / "raw"
    write_synthetic_raw(raw)
    return raw


@pytest.fixture(scope="session")
def synthetic_planted(synthetic_raw, tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("planted_a")
    plant_errors.plant(synthetic_raw, out, seed=42)
    return out


REAL_RAW = ROOT / "data" / "raw"
REAL_PLANTED = ROOT / "data" / "planted"


@pytest.fixture(scope="session", params=["synthetic", "real"])
def dataset(request, synthetic_raw, synthetic_planted):
    """(raw_dir, planted_dir, manifest) for the synthetic fixture and, if present, the real data."""
    if request.param == "synthetic":
        raw, planted = synthetic_raw, synthetic_planted
    else:
        if not (REAL_PLANTED / plant_errors.MANIFEST_FILE).exists():
            pytest.skip("real planted data not found; run download_data.py and plant_errors.py")
        raw, planted = REAL_RAW, REAL_PLANTED
    manifest = json.loads((planted / plant_errors.MANIFEST_FILE).read_text())
    return raw, planted, manifest


@pytest.fixture(scope="session")
def db(dataset):
    """DuckDB connection with views: trips, zones (planted) and raw_trips, raw_zones."""
    raw, planted, _ = dataset
    con = duckdb.connect()
    con.execute(f"CREATE VIEW trips AS SELECT * FROM '{planted / plant_errors.TRIPS_FILE}'")
    con.execute(
        f"CREATE VIEW zones AS SELECT * FROM read_csv('{planted / plant_errors.ZONES_FILE}',"
        " header = true)"
    )
    files = plant_errors.raw_trip_files(raw)
    con.execute(f"CREATE TABLE raw_trips AS {plant_errors.raw_trips_sql(files)}")
    con.execute(
        f"CREATE VIEW raw_zones AS SELECT * FROM read_csv('{raw / plant_errors.ZONES_FILE}',"
        " header = true)"
    )
    yield con
    con.close()
