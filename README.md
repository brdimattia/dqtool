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
uv run dqtool scan --pr                                    # ...and open a pull request to review
uv run dqtool pr runs/<run_id>                             # PR for an earlier run, no LLM
uv run dqtool review rules/<source>/<id>.yaml --approve    # or --reject --reason "..."
uv run dqtool review-stats                                 # reviewer precision
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The Propose step reads `ANTHROPIC_API_KEY` from the environment or from `.env`
(`ANTHROPIC_API_KEY=sk-ant-...`). Without a key, the scan skips Propose. Spend is
capped per scan and per month in `config/default.yaml`.

## Reviewing rules

Each scan PR adds one YAML file per proposed rule under `rules/<source>/` and an HTML
report under `reports/`. Set `review.decision` to `approve` or `reject` (with a reason) in
each file, or use `dqtool review`, then merge. Approved rules become the source's
baseline; rejected rules and their reasons go into the next scan's prompt.
