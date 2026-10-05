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

- [x] Config: thresholds and sources in `config/default.yaml`, validated by pydantic
- [x] Profiler: per-column stats (nulls, min, max, quantiles, median, MAD, distinct counts,
      top values), plus per-month row counts, null rates, medians, and MADs
- [x] Derived fields (defined in config): `duration_hours`, `implied_mph`, `fare_per_mile`,
      `tip_ratio`
- [x] Zone lookup profiled as its own source: text length, character classes, categories
- [x] Robust-stat detector: modified z-score above 3.5, on a log scale for right-skewed columns
- [x] Hard checks: negative values, rare categorical values, a dominant value with exceptions,
      timestamps outside the main time range, column order violations
- [x] Drift detector: month-over-month shifts in median and null rate
- [x] Text checks: instruction-like text (canary scan), unusual characters, unusual length
- [x] Output: `runs/<run_id>/` with `profile.json`, `slices.json`, `log.jsonl`, and
      `coverage.json` when given a manifest
- [x] CLI: `dqtool scan` (configured sources, or ad-hoc files)
- [x] Coverage check: 8/8 expected plants surfaced on real data with default config

### Step 2b: Isolation forest (deferred from step 2)

- [ ] Isolation forest on a sample, catching unusual combinations across columns
- [ ] Each flag reports which fields drove it, so it can become a deterministic rule

### Detector backlog (found during step 2)

- [ ] `total_amount` consistency check. The plain sum of components mismatches on 34% of
      real rows, so the real formula needs research first.
- [ ] Referential check: trip location ids that are missing from the zone lookup
- [ ] Drift on categorical value shares, not just medians and null rates
- [ ] Split large slices by cross-column signature (for example negative fare with a
      positive total vs everything negated), so small anomalous subsets show up in samples

### Step 3: Propose

- [x] Provider interface: prompt in, validated JSON out (`propose/providers/base.py`)
- [x] Claude implementation: structured outputs, streaming, refusal fallback (`"default"`)
      on the mid tier, model tiers and prices from config
- [x] Versioned prompt templates (`system_v1.md`); samples in delimited, escaped,
      truncated blocks; sensitive columns masked
- [x] Output schema (pydantic); invalid responses discarded with at most one retry
- [x] SQL guard (sqlglot): read-only boolean predicate, known columns, allow-listed
      functions and nodes
- [x] Optional two-tier review (triage plus escalation for ambiguous, low-confidence, or
      multi-column slices); default is single pass on Sonnet 5.5 (see measurements below)
- [x] Prompt v2: test a "legitimate" explanation against the flagged rows, write rules for
      unexplained period shifts, and offer `warn` rules for ambiguous slices
- [x] Cost controls: skip when quiet, result cache keyed by request hash, per-scan and
      monthly budget caps, cost log in `cost.json` and `log.jsonl`
- [x] Slices that flag the same rows grouped before prompting
- [x] Zone names joined into trip samples; small tables (the zone lookup) shown in full
- [x] Live runs on the real data (results below)
- [ ] Batch API mode for non-urgent scans (deferred)
- [ ] Per-source budget overrides (deferred)

#### Step 3 measurements (one run each, 2026-10-05)

Rules are counted as catching a plant when together they flag at least 90% of its rows or
zones. One run per configuration, so the numbers are directional.

| Plant | Haiku triage, v1 | Sonnet single pass, v1 | Sonnet single pass, v2 |
|---|---|---|---|
| Negative fares | caught | legitimate (refunds) | legitimate (refunds) |
| 2031 pickups | caught | caught | caught |
| Passenger count 87 | missed | caught | caught |
| Dropoff before pickup | caught | caught | caught |
| 0-mile trips, large fares | caught | 10% | 86% |
| Tip larger than fare | 1% | 8% | 18% |
| March distance doubled | "seasonal" | ambiguous, no rule | error; rule flags 38% of March |
| Misspelled zones | 3 of 5 | 5 of 5 | 5 of 5 |
| Wrong boroughs | 0 of 3 | 2 of 3 | 2 of 3 |
| Injection canary | caught | caught | caught |
| Rules (flagging >1%) | 27 (6) | 13 (2) | 20 (2) |
| Cost per scan | $0.16 | $0.33 | $0.34 |

Findings:

- Haiku's confidence did not track its accuracy, so escalation never triggered.
- The remaining misses are mostly structural. A slice's 20 sample rows rarely include
  planted rows when they are a tiny share of the slice (107 planted negative fares among
  535k real ones), so the LLM judges the slice by its real majority. Splitting large slices
  by cross-column signature (detector backlog) should address this better than prompting.

### Step 4: Prove and score

- [x] Backtester sandbox: fresh DuckDB connection that can read only the source files,
      with external access and extension loading off, settings locked, a memory limit,
      and a per-rule timeout
- [x] Drop rules that fail to run, or flag more than 1% of rows and more than 10 rows (the
      row floor keeps precise rules on small tables); SQL guard re-checked before running
- [x] Period rules (95% of flags in one period) are exempt from the noise threshold
- [x] Scoring against the manifest: recall by type (spec metric) and by plant, mean plant
      recall, rules hitting plants vs other findings, cost against budget
- [x] `results.json` per run, plus `runs/results.jsonl` history for comparing runs
- [x] `dqtool prove RUN_DIR`: re-run backtests and scoring with no LLM calls
- [x] Current result (Sonnet single pass, prompt v2): recall by type 100% (target 90%),
      by plant 60%, 20 of 20 rules surviving, $0.34 per scan

### Step 5: Review

- [ ] Rule YAML writer (spec rule format)
- [ ] Feedback memory: rejected rules and reasons fed into the next prompt
- [ ] Static HTML report with summary, findings, and rules layers
- [ ] Pull request opened per scan

### Open questions

- [x] How drift is scored: caught when one surviving rule puts at least 90% of its flags in
      the planted month and flags at least 10% of that month's rows.
- [x] Recall by type or by plant: both are reported; by type is the headline (spec).
- [x] Period rules vs the 1% noise threshold: period rules are exempt.
- [x] Noise threshold on small tables: rules flagging 10 rows or fewer are never noisy.
- [ ] Common real errors vs the noise threshold: a correct rule like `fare_amount < 0`
      (4.8% of rows) would be dropped. Decide whether such rules are dropped, kept as
      `warn`, or split (detector backlog).
- [ ] Planted negative fares are invisible to sample-based review (107 among 535k real
      refunds). Either split slices by signature or make the plant distinguishable.

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
