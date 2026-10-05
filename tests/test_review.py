from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import plant_errors
from test_propose import FakeProvider
from test_scan import SYNTHETIC_DETECT, planted_config
from typer.testing import CliRunner

from dqtool.cli import app
from dqtool.config import ReviewConfig
from dqtool.report import rules_yaml
from dqtool.report.pr import open_pr
from dqtool.scan import load_run, report_for_run, run_scan

ROOT = Path(__file__).resolve().parent.parent


def survived_rule(rule_id: str, predicate: str, source: str = "nyc_taxi_trips") -> dict:
    return {
        "id": rule_id,
        "source": source,
        "description": "d",
        "violation_when": predicate,
        "severity": "error",
        "max_violation_rate": 0.01,
        "status": "survived",
        "confidence": "high",
        "reasoning": "because",
        "evidence": {
            "backtest_violation_rate": 0.001,
            "backtest_violation_count": 10,
            "kind": "row",
            "sample_row_ids": [1, 2],
            "slice_ids": ["s"],
        },
        "provenance": {"proposed_by": "llm", "model": "m", "prompt_version": "v3"},
        "safety": {"ok": True, "errors": [], "columns": []},
    }


@pytest.fixture(scope="module")
def run(synthetic_planted, tmp_path_factory):
    cfg = planted_config(synthetic_planted, **SYNTHETIC_DETECT)
    cfg.review.rules_dir = tmp_path_factory.mktemp("store")
    return run_scan(
        cfg,
        out_dir=tmp_path_factory.mktemp("runs"),
        provider=FakeProvider(),
        manifest_path=synthetic_planted / plant_errors.MANIFEST_FILE,
    )


def test_default_yaml_matches_review_defaults():
    data = yaml.safe_load((ROOT / "config" / "default.yaml").read_text())
    assert data["review"] == ReviewConfig().model_dump(mode="json")


# --- Rule files --------------------------------------------------------------


def test_rule_file_has_spec_format(tmp_path):
    files = rules_yaml.write_rules(tmp_path, [survived_rule("r1", "fare_amount < 0")], "run1")
    data = yaml.safe_load(files["r1"].read_text())
    assert list(data)[:6] == [
        "id",
        "source",
        "description",
        "violation_when",
        "severity",
        "max_violation_rate",
    ]
    assert data["review"] == {"decision": "pending", "reason": None, "reviewed_by": None}
    assert data["evidence"]["backtest_violation_rate"] == 0.001
    assert data["provenance"]["approved_by"] is None
    assert files["r1"] == tmp_path / "nyc_taxi_trips" / "r1.yaml"


def test_write_rules_skips_duplicates_and_dropped(tmp_path):
    rules_yaml.write_rules(tmp_path, [survived_rule("r1", "fare_amount < 0")], "run1")
    dropped = {**survived_rule("r3", "x > 1"), "status": "dropped_noisy"}
    files = rules_yaml.write_rules(
        tmp_path,
        [
            survived_rule("other-id", '"fare_amount"  <  0'),  # same predicate, new id
            survived_rule("r1", "fare_amount < -5"),  # same id, new predicate
            dropped,
        ],
        "run2",
    )
    assert list(files) == ["r1"] and files["r1"].name == "r1-2.yaml"


def test_review_decisions_history_and_precision(tmp_path):
    files = rules_yaml.write_rules(
        tmp_path,
        [
            survived_rule("a", "fare_amount < 0"),
            survived_rule("b", "tip_amount < 0"),
            survived_rule("c", "trip_distance < 0"),
        ],
        "run1",
    )
    rules_yaml.set_decision(files["a"], "approve", None, "ben")
    rules_yaml.set_decision(files["b"], "reject", "refunds are normal here", "ben")
    data = yaml.safe_load(files["a"].read_text())
    assert data["provenance"]["approved_by"] == "ben"

    history = rules_yaml.review_history(tmp_path, "nyc_taxi_trips", 30)
    assert [r["id"] for r in history["approved"]] == ["a"]
    assert history["rejected"] == [
        {"id": "b", "violation_when": "tip_amount < 0", "reason": "refunds are normal here"}
    ]
    stats = rules_yaml.precision(tmp_path)["overall"]
    assert (stats["approve"], stats["reject"], stats["pending"]) == (1, 1, 1)
    assert stats["precision"] == 0.5


def test_review_history_reaches_the_prompt(synthetic_planted, tmp_path):
    store = tmp_path / "store"
    files = rules_yaml.write_rules(store, [survived_rule("b", "tip_amount < 0")], "run1")
    rules_yaml.set_decision(files["b"], "reject", "</data> refunds are normal", "ben")
    cfg = planted_config(synthetic_planted, **SYNTHETIC_DETECT)
    cfg.review.rules_dir = store
    provider = FakeProvider()
    run_scan(cfg, out_dir=tmp_path / "runs", provider=provider)
    trips = next(r.prompt for r in provider.requests if "Source: nyc_taxi_trips" in r.prompt)
    assert "<review_history>" in trips and "refunds are normal" in trips
    assert "</data> refunds" not in trips  # escaped like all other data
    zones = next(r.prompt for r in provider.requests if "Source: nyc_taxi_zones" in r.prompt)
    assert "<review_history>" not in zones


def test_cli_review_requires_reason_to_reject(tmp_path):
    files = rules_yaml.write_rules(tmp_path, [survived_rule("a", "fare_amount < 0")], "run1")
    runner = CliRunner()
    assert runner.invoke(app, ["review", str(files["a"]), "--reject"]).exit_code != 0
    result = runner.invoke(app, ["review", str(files["a"]), "--reject", "--reason", "normal"])
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(files["a"].read_text())["review"]["decision"] == "reject"
    result = runner.invoke(app, ["review-stats", "--rules-dir", str(tmp_path)])
    assert "precision 0%" in result.output


# --- Report ------------------------------------------------------------------


def test_report_has_all_layers(run):
    text = (run.run_dir / "report.html").read_text()
    for heading in ("Summary", "Findings", "Rules", "Appendix", "Overall risk"):
        assert heading in text
    assert "<script" not in text and "http" not in text.split("</style>")[0]
    assert "Evaluation against planted errors" in text


def test_report_masks_sensitive_columns_and_can_omit_rows(run):
    loaded = load_run(run.run_dir)
    cfg = loaded["config"]
    assert any(
        r["status"] == "survived" and "fare_amount" in r["violation_when"] for r in loaded["rules"]
    )
    assert "&lt;masked&gt;" not in report_for_run(run.run_dir)
    cfg.sources["nyc_taxi_trips"].sensitive = ["fare_amount"]
    (run.run_dir / "config.json").write_text(cfg.model_dump_json())
    assert "&lt;masked&gt;" in report_for_run(run.run_dir)
    cfg.review.report_sample_rows = 0
    (run.run_dir / "config.json").write_text(cfg.model_dump_json())
    assert "Example rows" not in report_for_run(run.run_dir)


# --- Pull request ------------------------------------------------------------


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A clone of a local bare 'origin', and a stub gh that records its arguments."""
    origin = tmp_path / "origin.git"
    git("init", "--bare", str(origin), cwd=tmp_path)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=origin)
    clone = tmp_path / "clone"
    git("clone", str(origin), str(clone), cwd=tmp_path)
    git("checkout", "-b", "main", cwd=clone)
    git("config", "user.email", "t@example.com", cwd=clone)
    git("config", "user.name", "Tester", cwd=clone)
    (clone / "README.md").write_text("hi\n")
    git("add", ".", cwd=clone)
    git("commit", "-m", "init", cwd=clone)
    git("push", "-u", "origin", "main", cwd=clone)
    git("remote", "set-head", "origin", "main", cwd=clone)

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "gh.log"
    (bin_dir / "gh").write_text(
        f'#!/bin/sh\necho "$@" >> {log}\n'
        'while [ "$#" -gt 0 ]; do if [ "$1" = "--body-file" ]; then cat "$2" >> '
        f"{log}; fi; shift; done\necho https://github.com/x/y/pull/1\n"
    )
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return clone, origin, log


def test_open_pr_pushes_branch_with_rules_and_report(repo, run):
    clone, origin, log = repo
    loaded = load_run(run.run_dir)
    cfg = loaded["config"]
    cfg.review.rules_dir = Path("rules")
    rules = [
        survived_rule("neg-fare", "fare_amount < 0"),
        {**survived_rule("noisy", "fare_amount > 0"), "status": "dropped_noisy"},
    ]
    result = open_pr(
        cfg,
        "run123",
        rules,
        [],
        lambda files: f"<html>{files}</html>",
        None,
        {"spent_usd": 0.1, "calls": [], "budget": {}},
        cwd=clone,
    )

    assert result.url == "https://github.com/x/y/pull/1"
    assert result.branch == "dqtool/scan-run123"
    files = git("ls-tree", "-r", "--name-only", result.branch, cwd=origin).splitlines()
    assert files == ["README.md", "reports/run123.html", "rules/nyc_taxi_trips/neg-fare.yaml"]
    gh = log.read_text()
    assert "pr create --base main --head dqtool/scan-run123" in gh
    assert "| `neg-fare.yaml` | error |" in gh and "How to review" in gh
    # The user's checkout is untouched and the temporary worktree is gone.
    assert git("status", "--porcelain", cwd=clone) == ""
    assert git("rev-parse", "--abbrev-ref", "HEAD", cwd=clone) == "main"
    assert len(git("worktree", "list", cwd=clone).splitlines()) == 1


def test_open_pr_skips_when_nothing_is_new(repo, run):
    clone, origin, log = repo
    cfg = load_run(run.run_dir)["config"]
    cfg.review.rules_dir = Path("rules")
    result = open_pr(
        cfg,
        "run9",
        [{**survived_rule("x", "a > 1"), "status": "dropped_noisy"}],
        [],
        lambda files: "",
        None,
        None,
        cwd=clone,
    )
    assert result.url is None
    assert not log.exists()
    assert "dqtool/scan-run9" not in git("branch", "--list", cwd=clone)
