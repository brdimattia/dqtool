"""Command-line entry point: one command per mode."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

import typer

from dqtool.config import SourceConfig, load_config, load_dotenv
from dqtool.scan import ScanResult, run_scan

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.callback()
def main() -> None:
    """Data validation tool (placeholder name). See docs/SPEC.md."""


@app.command()
def scan(
    paths: Annotated[
        list[Path] | None,
        typer.Argument(help="Parquet or CSV files to scan instead of the configured sources."),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Config YAML (default: config/default.yaml)."),
    ] = None,
    source: Annotated[
        list[str] | None, typer.Option("--source", "-s", help="Scan only these configured sources.")
    ] = None,
    out: Annotated[Path, typer.Option(help="Directory for run outputs.")] = Path("runs"),
    manifest: Annotated[
        Path | None,
        typer.Option(help="Planted-error manifest; reports which plants the slices surface."),
    ] = None,
    propose: Annotated[
        bool | None,
        typer.Option(
            "--propose/--no-propose",
            help="Run the LLM Propose step (default: propose.enabled in config).",
        ),
    ] = None,
) -> None:
    """Pre-ingestion risk scan: profile, detect, and propose rules. Writes runs/<run_id>/."""
    load_dotenv()
    cfg = load_config(config)
    names = source
    if paths:
        cfg.sources = {p.stem: SourceConfig(path=p) for p in paths}
        names = None

    provider = None
    if cfg.propose.enabled if propose is None else propose:
        if cfg.propose.provider != "claude":
            raise typer.BadParameter(f"Unknown provider: {cfg.propose.provider}")
        if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
            from dqtool.propose.providers.claude import ClaudeProvider

            provider = ClaudeProvider()
        else:
            typer.echo("No ANTHROPIC_API_KEY in the environment or .env: skipping Propose.")

    result = run_scan(cfg, names, out, manifest, provider)

    for name, src in result.sources.items():
        own = [s for s in result.slices if s.source == name]
        typer.echo(
            f"\n{name}: {src.row_count:,} rows, {len(src.columns)} columns,"
            f" {len(own)} slices ({result.timings[name]}s)"
        )
        for s in sorted(own, key=lambda s: (s.detector, s.slice_id)):
            text = s.description if len(s.description) <= 80 else s.description[:77] + "..."
            typer.echo(f"  {s.detector:<18} {s.flag_count:>10,} {s.flag_rate:>8.3%}  {text}")

    if result.coverage is not None:
        typer.echo("\nPlanted errors surfaced:")
        for r in result.coverage:
            mark = "yes" if r["surfaced"] else ("no " if r["expected"] else "n/a")
            first = r["slices"][0] if r["slices"] else "-"
            typer.echo(f"  {mark}  {r['plant_id']:<30} {r['best_fraction']:>5.0%}  {first}")
        expected = [r for r in result.coverage if r["expected"]]
        typer.echo(
            f"  {sum(r['surfaced'] for r in expected)}/{len(expected)} expected plants surfaced"
            " (semantic plants are left to the LLM step)"
        )
    if result.proposals is not None:
        print_proposals(result)
    typer.echo(f"\nWrote {result.run_dir}")


def print_proposals(result: ScanResult) -> None:
    for p in result.proposals:
        typer.echo(f"\nProposals for {p['source']}:")
        if p["skipped_reason"]:
            typer.echo(f"  skipped ({p['skipped_reason']})")
            continue
        verdicts: dict[str, int] = {}
        for r in p["reviews"]:
            verdicts[str(r["verdict"])] = verdicts.get(str(r["verdict"]), 0) + 1
        typer.echo(
            f"  {p['slice_groups']} slice groups reviewed: "
            + ", ".join(f"{v} {k}" for k, v in sorted(verdicts.items()))
            + f"; {len(p['escalated'])} escalated; {len(p['new_issues'])} new issues"
        )
        for rule in p["rules"]:
            mark = "ok " if rule["safety"]["ok"] else "REJ"
            pred = rule["violation_when"]
            pred = pred if len(pred) <= 70 else pred[:67] + "..."
            typer.echo(f"  {mark} {rule['severity']:<5} {rule['id']:<36} {pred}")
            if not rule["safety"]["ok"]:
                typer.echo(f"        sql guard: {'; '.join(rule['safety']['errors'])}")
    cost = result.cost
    typer.echo(
        f"\nLLM cost: ${cost['spent_usd']:.4f} of ${cost['budget']['max_usd_per_scan']:.2f}"
        f" scan budget, {len(cost['calls'])} call(s)"
        + (f", {len(cost['skipped'])} skipped for budget" if cost["skipped"] else "")
    )


if __name__ == "__main__":
    app()
