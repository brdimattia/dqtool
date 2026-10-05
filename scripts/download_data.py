"""Download NYC TLC yellow taxi trip data and the taxi zone lookup into data/raw/.

URLs are the ones linked from the official trip record data page:
https://www.nyc.gov/site/tlc/about/tlc-trip-record-data.page

Months are pinned by default so the dataset (and everything planted into it)
is reproducible. Existing files are skipped.

Usage:
    uv run python scripts/download_data.py
    uv run python scripts/download_data.py --start 2025-01 --months 3
"""

from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
from pathlib import Path

BASE_URL = "https://d37ci6vzurychx.cloudfront.net"
TRIP_URL = BASE_URL + "/trip-data/yellow_tripdata_{month}.parquet"
ZONE_URL = BASE_URL + "/misc/taxi_zone_lookup.csv"

DEFAULT_START = "2025-01"
DEFAULT_MONTHS = 3
RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"


def consecutive_months(start: str, count: int) -> list[str]:
    year, month = (int(part) for part in start.split("-"))
    months = []
    for _ in range(count):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


def download(url: str, dest: Path) -> None:
    if dest.exists():
        print(f"skip   {dest.name} (exists)")
        return
    print(f"fetch  {url}")
    tmp = dest.with_suffix(dest.suffix + ".part")
    # The TLC CDN rejects requests without a browser-like user agent.
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 dqtool"})
    with urllib.request.urlopen(request, timeout=120) as response, tmp.open("wb") as out:
        shutil.copyfileobj(response, out)
    tmp.rename(dest)
    print(f"saved  {dest} ({dest.stat().st_size:,} bytes)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default=DEFAULT_START, help="first month, YYYY-MM")
    parser.add_argument("--months", type=int, default=DEFAULT_MONTHS)
    parser.add_argument("--out", type=Path, default=RAW_DIR)
    args = parser.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    for month in consecutive_months(args.start, args.months):
        name = f"yellow_tripdata_{month}.parquet"
        download(TRIP_URL.format(month=month), args.out / name)
    download(ZONE_URL, args.out / "taxi_zone_lookup.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
