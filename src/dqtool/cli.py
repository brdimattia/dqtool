"""Command-line entry point: one command per mode."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from dqtool.config import SourceConfig, load_config
from dqtool.scan import run_scan

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
) -> None:
    """Pre-ingestion risk scan: profile and detect (no LLM yet). Writes runs/<run_id>/."""
    cfg = load_config(config)
    names = source
    if paths:
        cfg.sources = {p.stem: SourceConfig(path=p) for p in paths}
        names = None

    result = run_scan(cfg, names, out, manifest)

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
    typer.echo(f"\nWrote {result.run_dir}")


if __name__ == "__main__":
    app()
