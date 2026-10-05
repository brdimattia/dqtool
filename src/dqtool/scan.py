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
from dqtool.config import Config, load_config
from dqtool.detect.detectors import Slice, detect
from dqtool.logging import RunLog, to_json
from dqtool.profile.profiler import profile_source
from dqtool.profile.source import Source, register_source
from dqtool.propose.engine import (
    Caller,
    Ledger,
    ResultCache,
    cost_summary,
    month_spend,
    propose_source,
)
from dqtool.propose.providers.base import Provider
from dqtool.prove.backtest import prove
from dqtool.prove.coverage import coverage
from dqtool.prove.score import score


@dataclass
class ScanResult:
    run_id: str
    run_dir: Path
    sources: dict[str, Source]
    profiles: dict[str, dict]
    slices: list[Slice]
    coverage: list[dict] | None = None
    proposals: list[dict] | None = None
    cost: dict | None = None
    rules: list[dict] | None = None
    results: dict | None = None
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
    provider: Provider | None = None,
) -> ScanResult:
    """Profile and detect every source; with a provider, also run Propose (LLM)."""
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
    write_json(run_dir / "config.json", config.model_dump(mode="json"))
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
    if provider is not None:
        ledger = Ledger(config, month_spent=month_spend(out_dir, run_id[:6]))
        cache = ResultCache(out_dir / ".llm_cache" if config.propose.cache else None)
        caller = Caller(provider, ledger, cache, log)
        result.proposals = [
            propose_source(con, sources[name], profiles[name], slices, sources, config, caller)
            for name in names
        ]
        result.cost = cost_summary(ledger)
        write_json(run_dir / "proposals.json", result.proposals)
        write_json(run_dir / "cost.json", result.cost)
        log.event("budget", spent_usd=ledger.spent, skipped=len(ledger.skipped))
    con.close()

    if result.proposals is not None:
        manifest = json.loads(manifest_path.read_text()) if manifest_path else None
        result.rules, result.results = prove_and_score(
            config, run_dir, result.proposals, profiles, result.cost, manifest, log, out_dir
        )
    log.event("run_end", slices=len(slices))
    return result


def prove_and_score(
    config: Config,
    run_dir: Path,
    proposals: list[dict],
    profiles: dict[str, dict],
    cost: dict | None,
    manifest: dict | None,
    log: RunLog,
    out_dir: Path,
) -> tuple[list[dict], dict | None]:
    """Backtest every proposed rule, then score against the manifest when there is one."""
    rules = prove(config, proposals, profiles)
    for r in rules:
        log.event(
            "rule_lifecycle",
            rule_id=r["id"],
            source=r["source"],
            status=r["status"],
            violation_rate=r["evidence"].get("backtest_violation_rate"),
            kind=r["evidence"].get("kind"),
            error=r["evidence"].get("error"),
        )
    write_json(run_dir / "rules.json", rules)
    if manifest is None:
        return rules, None

    results = score(config, manifest, rules, cost)
    results = {
        "run_id": run_dir.name,
        "prompt_version": next((r["provenance"]["prompt_version"] for r in rules), None),
        "models": sorted({r["provenance"]["model"] for r in rules}),
        **results,
    }
    write_json(run_dir / "results.json", results)
    with (out_dir / "results.jsonl").open("a") as f:
        f.write(
            json.dumps(
                {k: results[k] for k in ("run_id", "prompt_version", "models", "metrics")},
                default=to_json,
            )
            + "\n"
        )
    log.event("results", **results["metrics"])
    return rules, results


def reprove(run_dir: Path, manifest_path: Path | None) -> tuple[list[dict], dict | None]:
    """Re-run Prove and scoring on an earlier run's proposals, with no LLM calls."""
    config_path = run_dir / "config.json"
    # Runs from before config snapshots fall back to the current default config.
    config = (
        Config.model_validate_json(config_path.read_text())
        if config_path.exists()
        else load_config()
    )
    proposals = json.loads((run_dir / "proposals.json").read_text())
    profiles = json.loads((run_dir / "profile.json").read_text())
    cost_path = run_dir / "cost.json"
    cost = json.loads(cost_path.read_text()) if cost_path.exists() else None
    manifest = json.loads(manifest_path.read_text()) if manifest_path else None
    log = RunLog(run_dir / "log.jsonl", run_dir.name)
    log.event("reprove_start", manifest=str(manifest_path) if manifest_path else None)
    return prove_and_score(
        config, run_dir, proposals, profiles, cost, manifest, log, run_dir.parent
    )
