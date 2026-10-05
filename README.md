# dqtool

Placeholder name. See [docs/SPEC.md](docs/SPEC.md) for the design and [docs/PLAN.md](docs/PLAN.md) for build status.

## Usage

```sh
uv sync
uv run python scripts/download_data.py            # 2025-01..03 yellow trips + zone lookup -> data/raw/
uv run python scripts/plant_errors.py --seed 42   # -> data/planted/ (+ manifest.json)
uv run dqtool scan --manifest data/planted/manifest.json   # profile + detect -> runs/<run_id>/
uv run pytest
uv run ruff check . && uv run ruff format --check .
```
