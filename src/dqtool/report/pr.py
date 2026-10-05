"""Open a pull request per scan: rule files plus the HTML report (spec: Review surface).

Works in a temporary git worktree on a new branch from the remote's base branch,
so the user's working tree and current branch are never touched.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from dqtool.config import Config
from dqtool.report.html import risk_level
from dqtool.report.rules_yaml import write_rules


class PRError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise PRError(f"{' '.join(args[:3])} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def repo_root(cwd: Path = Path(".")) -> Path:
    return Path(_run(["git", "rev-parse", "--show-toplevel"], cwd))


def base_branch(config: Config, root: Path) -> str:
    if config.review.base_branch:
        return config.review.base_branch
    try:
        ref = _run(["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], root)
        return ref.removeprefix("origin/")
    except PRError:
        return _run(
            ["gh", "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
            root,
        )


def pr_body(
    run_id: str,
    rules: list[dict],
    files: dict[str, Path],
    slices: list[dict],
    results: dict | None,
    cost: dict | None,
    report_rel: str,
) -> str:
    survived = [r for r in rules if r["id"] in files]
    errors = sum(r["severity"] == "error" for r in survived)
    lines = [
        f"## Data risk scan `{run_id}`",
        "",
        f"Overall risk: **{risk_level(rules, slices)}**. {len(survived)} rule(s) for review"
        f" ({errors} errors, {len(survived) - errors} warnings). Full report with evidence:"
        f" `{report_rel}` (download and open in a browser).",
        "",
        "| Rule | Severity | Rows flagged | Share | violation_when |",
        "|---|---|---|---|---|",
    ]
    for r in survived:
        ev = r["evidence"]
        pred = r["violation_when"].replace("|", "\\|")
        pred = pred if len(pred) <= 90 else pred[:87] + "..."
        rate = ev.get("backtest_violation_rate") or 0
        lines.append(
            f"| `{files[r['id']].name}` | {r['severity']}"
            f" | {ev.get('backtest_violation_count', 0):,} | {rate:.3%} | `{pred}` |"
        )
    lines += [
        "",
        "### How to review",
        "",
        "For each rule file, set `review.decision` to `approve` or `reject` and give a reason,"
        " or run:",
        "",
        "```sh",
        "uv run dqtool review rules/<source>/<id>.yaml --approve",
        'uv run dqtool review rules/<source>/<id>.yaml --reject --reason "..."',
        "```",
        "",
        "Edit `violation_when` to adjust a rule. When you merge, approved rules become the"
        " source's baseline, and rejected rules with their reasons go into the next scan's"
        " prompt so it learns what this source considers normal.",
    ]
    footer = []
    if results:
        m = results["metrics"]
        footer.append(
            f"Planted-error recall: {m['planted_recall_by_type']:.0%} by type,"
            f" {m['planted_recall_by_plant']:.0%} by plant."
        )
    if cost:
        footer.append(f"LLM cost: ${cost['spent_usd']:.4f} ({len(cost.get('calls', []))} calls).")
    if footer:
        lines += ["", " ".join(footer)]
    lines += ["", "_Opened by dqtool._"]
    return "\n".join(lines)


@dataclass
class PRResult:
    url: str | None
    branch: str
    files: dict[str, Path]


def open_pr(
    config: Config,
    run_id: str,
    rules: list[dict],
    slices: list[dict],
    report_html_for: Callable[[dict[str, str]], str],
    results: dict | None,
    cost: dict | None,
    cwd: Path = Path("."),
) -> PRResult:
    """Write new rule files and the report on a branch, push it, and open a pull request.

    report_html_for(files) builds the report once the rule file paths are known.
    """
    root = repo_root(cwd)
    base = base_branch(config, root)
    branch = f"{config.review.branch_prefix}{run_id}"
    _run(["git", "fetch", "origin", base], root)
    worktree = Path(tempfile.mkdtemp(prefix="dqtool-pr-"))
    shutil.rmtree(worktree)
    _run(["git", "worktree", "add", "-b", branch, str(worktree), f"origin/{base}"], root)
    pushed = False
    try:
        written = write_rules(worktree / config.review.rules_dir, rules, run_id)
        if not written:
            return PRResult(None, branch, {})
        files = {rid: path.relative_to(worktree) for rid, path in written.items()}
        report_rel = config.review.reports_dir / f"{run_id}.html"
        (worktree / report_rel).parent.mkdir(parents=True, exist_ok=True)
        (worktree / report_rel).write_text(report_html_for({k: str(v) for k, v in files.items()}))

        _run(["git", "add", str(config.review.rules_dir), str(report_rel)], worktree)
        _run(
            ["git", "commit", "-m", f"dqtool scan {run_id}: {len(files)} rule(s) for review"],
            worktree,
        )
        _run(["git", "push", "-u", "origin", branch], worktree)
        pushed = True
        body = pr_body(run_id, rules, files, slices, results, cost, str(report_rel))
        body_file = worktree.parent / f"{worktree.name}-body.md"
        body_file.write_text(body)
        try:
            url = _run(
                [
                    "gh",
                    "pr",
                    "create",
                    "--base",
                    base,
                    "--head",
                    branch,
                    "--title",
                    f"Data risk scan {run_id}: {len(files)} rule(s) to review",
                    "--body-file",
                    str(body_file),
                ],
                worktree,
            )
        finally:
            body_file.unlink(missing_ok=True)
        return PRResult(url.splitlines()[-1] if url else None, branch, files)
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=root,
            capture_output=True,
            check=False,
        )
        if not pushed:
            subprocess.run(
                ["git", "branch", "-D", branch], cwd=root, capture_output=True, check=False
            )
