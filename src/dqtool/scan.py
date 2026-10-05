"""Pre-ingestion scan, steps Profile and Detect: sources in, suspicious slices out."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from dqtool import __version__
from dqtool.config import Config
from dqtool.detect.detectors import Slice, detect
from dqtool.logging import RunLog, to_json
from dqtool.profile.profiler import profile_source
from dqtool.profile.source import Source, register_source
from dqtool.prove.coverage import coverage


@dataclass
class ScanResult:
    run_id: str
    run_dir: Path
    sources: dict[str, Source]
    profiles: dict[str, dict]
    slices: list[Slice]
    coverage: list[dict] | None = None
    timings: dict[str, float] = field(default_factory=dict)


def write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, indent=2, default=to_json) + "\n")


def fingerprint(config: Config, names: list[str]) -> str:
    """Hash of the scanned files and the config, so unchanged inputs are recognizable."""
    digest = hashlib.sha256(config.model_dump_json().encode())
    for name in names:
        with config.sources[name].path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                digest.update(chunk)
    return digest.hexdigest()


def run_scan(
    config: Config,
    source_names: list[str] | None = None,
    out_dir: Path = Path("runs"),
    manifest_path: Path | None = None,
) -> ScanResult:
    names = source_names or list(config.sources)
    unknown = [n for n in names if n not in config.sources]
    if unknown:
        raise KeyError(f"Unknown source(s): {', '.join(unknown)}")
    if not names:
        raise ValueError("No sources to scan. Pass a file or add sources to the config.")

    data_fingerprint = fingerprint(config, names)
    run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{data_fingerprint[:8]}"
    run_dir = out_dir / run_id
    run_dir.mkdir(parents=True)
    log = RunLog(run_dir / "log.jsonl", run_id)
    log.event(
        "run_start",
        tool_version=__version__,
        duckdb_version=duckdb.__version__,
        config=config.model_dump(mode="json"),
        sources=names,
        data_fingerprint=data_fingerprint,
    )

    con = duckdb.connect()
    sources: dict[str, Source] = {}
    profiles: dict[str, dict] = {}
    slices: list[Slice] = []
    timings: dict[str, float] = {}
    for name in names:
        started = time.perf_counter()
        source = register_source(con, name, config.sources[name], config.detect)
        sources[name] = source
        profiles[name] = profile_source(con, source, config.detect)
        found = detect(con, source, profiles[name], config.detect)
        slices.extend(found)
        timings[name] = round(time.perf_counter() - started, 2)
        log.event(
            "source_scanned",
            source=name,
            rows=source.row_count,
            columns=len(source.columns),
            slices=len(found),
            seconds=timings[name],
        )

    write_json(run_dir / "profile.json", profiles)
    write_json(run_dir / "slices.json", [s.model_dump() for s in slices])

    result = ScanResult(run_id, run_dir, sources, profiles, slices, timings=timings)
    if manifest_path:
        manifest = json.loads(manifest_path.read_text())
        result.coverage = coverage(con, manifest, sources, slices)
        write_json(run_dir / "coverage.json", result.coverage)
        log.event(
            "coverage",
            surfaced=sum(r["surfaced"] for r in result.coverage),
            expected=sum(r["expected"] for r in result.coverage),
        )
    con.close()
    log.event("run_end", slices=len(slices))
    return result
