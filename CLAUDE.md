# dqtool

Data-quality tool (placeholder name): an LLM proposes data-quality rules, and they are proven deterministically against planted errors. Design: `docs/SPEC.md`. Build status and what's next: `docs/PLAN.md` (keep it updated as steps finish).

## Stack
Python 3.12 (exactly — `>=3.12,<3.13`), managed with `uv`. DuckDB, pydantic, sqlglot, typer, Anthropic SDK.

## Layout
- `src/dqtool/` — `cli.py` (typer entry point `dqtool`), `scan.py`, and one package per pipeline stage: `profile/`, `detect/`, `propose/`, `prove/`, `report/`, plus `safety/`, `config.py`
- `config/default.yaml` — thresholds, sources, LLM spend caps (validated by pydantic)
- `rules/<source>/<id>.yaml` — reviewed rules · `runs/<run_id>/` — scan outputs · `data/` — raw + planted data (not in git)
- `scripts/` — `download_data.py`, `plant_errors.py` · `tests/`

## Commands (verified 2026-10-18)
```sh
uv sync                                             # set up .venv
uv run pytest                                       # 96 tests, ~70 s
uv run ruff check . && uv run ruff format --check . # lint + format (line length 100)
uv run python scripts/download_data.py              # NYC yellow-taxi 2025-01..03 → data/raw/
uv run python scripts/plant_errors.py --seed 42     # → data/planted/ + manifest.json
uv run dqtool scan --no-propose                     # deterministic only, no LLM calls
uv run dqtool scan --manifest data/planted/manifest.json   # full scan (uses the API)
```

## Notes
- The Propose step calls Claude using `ANTHROPIC_API_KEY` from `.env` (gitignored — never commit it). Without a key, Propose is skipped. Spend is capped per scan and per month in `config/default.yaml`; prefer `--no-propose` when testing.
- Same seed must give the same planted output — keep `plant_errors.py` deterministic.
- Work happens on step branches (currently `phase0-step5`).
