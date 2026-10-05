# dqtool

Placeholder name. See [docs/SPEC.md](docs/SPEC.md) for the design and [docs/PLAN.md](docs/PLAN.md) for build status.

## Usage

```sh
uv sync
uv run python scripts/download_data.py            # 2025-01..03 yellow trips + zone lookup -> data/raw/
uv run python scripts/plant_errors.py --seed 42   # -> data/planted/ (+ manifest.json)
uv run dqtool scan --manifest data/planted/manifest.json   # profile, detect, propose, prove, score
uv run dqtool scan --no-propose                            # deterministic only, no LLM calls
uv run dqtool prove runs/<run_id> --manifest data/planted/manifest.json  # re-score, no LLM
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The Propose step reads `ANTHROPIC_API_KEY` from the environment or from `.env`
(`ANTHROPIC_API_KEY=sk-ant-...`). Without a key, the scan skips Propose. Spend is
capped per scan and per month in `config/default.yaml`.
