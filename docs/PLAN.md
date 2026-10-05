# Build Plan

The working checklist for building what [SPEC.md](SPEC.md) describes. Update it in the same
commit as the work it describes: tick items when done, and add items when scope changes or
something gets deferred.

Status: `[x]` done · `[ ]` planned · `[~]` in progress · *deferred* items say where they moved.

## Phase 0: Prototype

Goal: a full pre-ingestion scan on three months of NYC yellow taxi data, scored against
planted errors. Done when the [Evaluation](SPEC.md#evaluation) targets are met.

### Step 1: Data and plants

- [x] Repo setup: Python 3.12, uv, ruff, pytest, spec folder layout
- [x] `scripts/download_data.py`: 2025-01 to 2025-03 yellow trips plus zone lookup into
      `data/raw/`
- [x] `scripts/plant_errors.py --seed 42`: stable `row_id`, plants for every catalog type,
      `data/planted/` and `manifest.json`
- [x] Tests: same seed gives same output, every plant is present, predicates flag their plants

### Step 2: Profile and detect (no LLM)

- [ ] Config: thresholds in `config/default.yaml` (3.5 MAD cutoff, 1% noise threshold)
- [ ] Profiler: per-column stats (nulls, min, max, quantiles, median, MAD, distinct counts,
      top values), all split by month
- [ ] Derived fields: `duration_hours`, `implied_mph`, `fare_per_mile`, `tip_ratio`,
      `total_amount` consistency
- [ ] Zone lookup profiled as its own source: text length, character classes, borough counts
- [ ] Robust-stat detector: values beyond 3.5 MADs on raw and derived columns
- [ ] Hard checks: negative amounts, timestamps outside the data's range, dropoff before pickup
- [ ] Drift detector: month-over-month shifts in median and spread
- [ ] Text checks on zones: unusual names and instruction-like text (canary scan)
- [ ] Output: `runs/<run_id>/slices.json`, each slice with predicate, flag rate, sample
      row_ids, and the field that triggered it
- [ ] CLI: `dqtool scan` (typer)
- [ ] Coverage check: detector surfaces every planted type except semantic

### Step 2b: Isolation forest (deferred from step 2)

- [ ] Isolation forest on a sample, catching unusual combinations across columns
- [ ] Each flag reports which fields drove it, so it can become a deterministic rule

### Step 3: Propose

- [ ] Provider interface: prompt in, validated JSON out
- [ ] Claude implementation, model tiers from config
- [ ] Versioned prompt templates; samples in delimited, escaped blocks
- [ ] Output schema (pydantic), at most one retry
- [ ] SQL safety parser (sqlglot): read-only boolean predicate, known columns, allow-listed
      functions
- [ ] Cost controls: skip when quiet, fingerprint cache, budget cap, cost log

### Step 4: Prove and score

- [ ] Backtester: run each rule on the full data in a read-only, resource-limited connection
- [ ] Drop rules that fail to run or flag more than the noise threshold
- [ ] Scoring against the manifest: planted recall, cost per scan
- [ ] Results file per run, comparable across runs

### Step 5: Review

- [ ] Rule YAML writer (spec rule format)
- [ ] Static HTML report with summary, findings, and rules layers
- [ ] Pull request opened per scan

### Open questions from step 1

- [ ] The 1% noise threshold would drop correct rules on common real errors (for example
      `fare_amount < 0` matches 4.8% of rows). Decide before step 4.
- [ ] How step 4 decides drift "was caught" (the plant is a whole month, not a set of rows).
- [ ] Whether semantic and canary plants are scored on the zone table or the joined trips.
- [ ] Recall by type (spec: 5 types) or by plant (10 plants).

## Phase 1: Understand

- [ ] Data explainer
- [ ] Missing-information detection
- [ ] PII detection
- [ ] Data classification
- [ ] Deterministic-only mode

## Phase 2: Operate

- [ ] Ongoing pipeline validation
- [ ] Dataset health scoring
- [ ] Alerts and metrics
- [ ] Primary key recommendation
- [ ] Rule compaction
- [ ] dbt and DQX compile targets

## Phase 3: Connect

- [ ] Codebase review
- [ ] Lineage and impact
- [ ] Data product trust review
- [ ] Cross-source reconciliation
- [ ] Contract generation

## Phase 4: Extend

- [ ] Skill integration
- [ ] Embeddings
- [ ] More LLM providers
- [ ] Great Expectations target
- [ ] Interactive review app
- [ ] Text normalization
