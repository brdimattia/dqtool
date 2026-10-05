"""Rule files: one YAML file per rule under rules/<source>/<id>.yaml (spec: Rule format).

A rule file is the unit of review. A reviewer sets review.decision to approve or
reject (with a reason) in the pull request; approved rules become the source's
baseline, and rejected rules with their reasons feed the next scan's prompt.
"""

from __future__ import annotations

import getpass
import subprocess
from pathlib import Path

import yaml

DECISIONS = ("pending", "approve", "reject")


def to_yaml_rule(rule: dict, run_id: str) -> dict:
    """The spec's rule format, plus a review block and the backtest evidence."""
    ev = rule["evidence"]
    prov = rule["provenance"]
    evidence = {
        "backtest_violation_rate": ev.get("backtest_violation_rate"),
        "backtest_violation_count": ev.get("backtest_violation_count"),
        "kind": ev.get("kind"),
        "sample_row_ids": ev.get("sample_row_ids", []),
        "slice_ids": ev.get("slice_ids", []),
        "run_id": run_id,
    }
    if ev.get("period"):
        evidence["period"] = ev["period"]
    return {
        "id": rule["id"],
        "source": rule["source"],
        "description": rule["description"],
        "violation_when": rule["violation_when"],
        "severity": rule["severity"],
        "max_violation_rate": rule["max_violation_rate"],
        "review": {"decision": "pending", "reason": None, "reviewed_by": None},
        "evidence": evidence,
        "provenance": {
            "proposed_by": prov.get("proposed_by", "llm"),
            "model": prov.get("model"),
            "prompt_version": prov.get("prompt_version"),
            "confidence": rule.get("confidence"),
            "reasoning": rule.get("reasoning"),
            "approved_by": None,
        },
    }


class _Dumper(yaml.SafeDumper):
    pass


def _str(dumper: yaml.SafeDumper, value: str):
    style = "|" if "\n" in value else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style=style)


_Dumper.add_representer(str, _str)


def dump(rule: dict) -> str:
    return yaml.dump(rule, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)


def rule_path(rules_dir: Path, rule: dict) -> Path:
    return rules_dir / rule["source"] / f"{rule['id']}.yaml"


def load_rules(rules_dir: Path) -> list[dict]:
    """Every rule file under rules_dir, each with its path under '_path'."""
    rules = []
    for path in sorted(rules_dir.glob("*/*.yaml")):
        data = yaml.safe_load(path.read_text()) or {}
        if isinstance(data, dict) and "violation_when" in data:
            rules.append({**data, "_path": str(path)})
    return rules


def _norm(predicate: str) -> str:
    return " ".join(predicate.lower().replace('"', "").split())


def write_rules(rules_dir: Path, rules: list[dict], run_id: str) -> dict[str, Path]:
    """Write surviving rules as files, skipping ones already in the store.

    A rule is already present when its source has a file with the same predicate.
    Id clashes with a different predicate get a numeric suffix. Returns the written
    files keyed by the rule's id in the run.
    """
    existing = load_rules(rules_dir)
    known = {(r["source"], _norm(r["violation_when"])) for r in existing}
    ids = {(r["source"], r["id"]) for r in existing}
    written: dict[str, Path] = {}
    for rule in rules:
        if rule["status"] != "survived" or (rule["source"], _norm(rule["violation_when"])) in known:
            continue
        data = to_yaml_rule(rule, run_id)
        base, n = data["id"], 2
        while (data["source"], data["id"]) in ids:
            data["id"], n = f"{base}-{n}", n + 1
        ids.add((data["source"], data["id"]))
        known.add((data["source"], _norm(data["violation_when"])))
        path = rule_path(rules_dir, data)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dump(data))
        written[rule["id"]] = path
    return written


def reviewer_name() -> str:
    try:
        name = subprocess.run(
            ["git", "config", "user.name"], capture_output=True, text=True, check=False
        ).stdout.strip()
    except OSError:
        name = ""
    return name or getpass.getuser()


def set_decision(path: Path, decision: str, reason: str | None, reviewer: str) -> dict:
    """Record a review decision in a rule file, keeping everything else as is."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {', '.join(DECISIONS)}")
    data = yaml.safe_load(path.read_text())
    data["review"] = {"decision": decision, "reason": reason, "reviewed_by": reviewer}
    data["provenance"]["approved_by"] = reviewer if decision == "approve" else None
    path.write_text(dump(data))
    return data


def review_history(rules_dir: Path, source: str, limit: int) -> dict:
    """Approved and rejected rules for a source, newest run first, for the prompt."""
    mine = [r for r in load_rules(rules_dir) if r.get("source") == source]
    mine.sort(key=lambda r: str((r.get("evidence") or {}).get("run_id", "")), reverse=True)

    def decided(decision: str) -> list[dict]:
        return [r for r in mine if (r.get("review") or {}).get("decision") == decision][:limit]

    return {
        "approved": [
            {"id": r["id"], "violation_when": r["violation_when"]} for r in decided("approve")
        ],
        "rejected": [
            {
                "id": r["id"],
                "violation_when": r["violation_when"],
                "reason": (r.get("review") or {}).get("reason"),
            }
            for r in decided("reject")
        ],
    }


def precision(rules_dir: Path) -> dict:
    """Reviewer precision: approved / (approved + rejected), overall and per source."""
    counts: dict[str, dict[str, int]] = {}
    for r in load_rules(rules_dir):
        decision = (r.get("review") or {}).get("decision", "pending")
        c = counts.setdefault(r["source"], {d: 0 for d in DECISIONS})
        c[decision] = c.get(decision, 0) + 1

    def ratio(c: dict) -> float | None:
        decided = c["approve"] + c["reject"]
        return c["approve"] / decided if decided else None

    total = {d: sum(c[d] for c in counts.values()) for d in DECISIONS}
    return {
        "overall": {**total, "precision": ratio(total)},
        "by_source": {s: {**c, "precision": ratio(c)} for s, c in counts.items()},
    }
