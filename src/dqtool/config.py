"""Configuration: thresholds and source definitions.

Defaults here must match config/default.yaml (a test checks this), so the tool
runs with no config file at all and the YAML documents every tunable value.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DetectConfig(Strict):
    # Modified z-score cutoff: 0.6745 * |x - median| / MAD (Iglewicz and Hoaglin).
    mad_threshold: float = 3.5
    # Right-skewed columns get their upper bound on a log1p scale. A column is
    # right-skewed when (p99 - p50) > skew_ratio * (p50 - p01).
    skew_ratio: float = 3.0
    # Numeric columns with at most this many distinct values are treated as categorical.
    categorical_max_distinct: int = 50
    # A categorical value this rare (share of non-null rows) is suspicious.
    rare_value_share: float = 0.0001
    # When one value covers at least this share of a numeric column, other values are flagged.
    dominant_value_share: float = 0.95
    # Negative values are flagged when they are at most this share of a column.
    negative_max_share: float = 0.10
    # Column order a <= b is treated as expected when it holds for this share of rows.
    order_min_share: float = 0.99
    # Periods with less than this share of rows are outside the data's main time range.
    min_period_share: float = 0.01
    # A period's median differing from the other periods' by this relative amount is drift.
    drift_relative_change: float = 0.25
    # A period's null rate differing from the other periods' by this absolute amount is drift.
    drift_null_rate_change: float = 0.05
    # Evidence row_ids kept per slice.
    sample_rows: int = 20


class ProveConfig(Strict):
    # Rules flagging more than this share of rows are dropped as noisy (used in step 4).
    max_violation_rate: float = 0.01


class TierConfig(Strict):
    model: str
    max_tokens: int = 16000
    # Thinking depth on models that support it (low, medium, high, xhigh, max).
    effort: str | None = None
    # Server-side refusal fallback ("default" lets the API pick by refusal category).
    fallbacks: str | None = None


class ProposeConfig(Strict):
    enabled: bool = True
    provider: str = "claude"
    # Smallest current model does first-pass triage.
    triage: TierConfig = Field(
        default_factory=lambda: TierConfig(model="claude-haiku-4-5", max_tokens=16000)
    )
    # Mid-tier model reviews only slices triage marks ambiguous or low-confidence, or that
    # span several columns.
    escalate: TierConfig = Field(
        default_factory=lambda: TierConfig(
            model="claude-sonnet-5-5", max_tokens=32000, effort="medium", fallbacks="default"
        )
    )
    # Sample rows shown per slice (spec default: 20 per issue).
    sample_rows: int = 20
    # Each value in a prompt is truncated to this many characters.
    max_value_chars: int = 200
    # Tables this small (such as lookups) are shown in full, so the LLM can judge their text.
    full_table_max_rows: int = 300
    # At most this many slices per source go into one prompt.
    max_slices: int = 60
    # Cache LLM results by a hash of the request; unchanged data costs nothing to rescan.
    cache: bool = True


class BudgetConfig(Strict):
    max_usd_per_scan: float = 1.0
    max_usd_per_month: float = 20.0


class Price(Strict):
    # USD per million tokens.
    input: float
    output: float
    cache_read: float


def _default_pricing() -> dict[str, Price]:
    return {
        "claude-haiku-4-5": Price(input=1.0, output=5.0, cache_read=0.1),
        "claude-sonnet-5-5": Price(input=2.0, output=10.0, cache_read=0.2),
        "claude-opus-5-5": Price(input=4.0, output=20.0, cache_read=0.2),
    }


class LookupConfig(Strict):
    # Another configured source to join into sample rows, by key.
    source: str
    key: str
    columns: list[str]


class SourceConfig(Strict):
    path: Path
    row_id: str | None = None
    time_column: str | None = None
    period: str = "month"
    # Columns that hold codes or ids rather than quantities.
    categorical: list[str] = Field(default_factory=list)
    # Groups of columns in the same unit, checked for a usual ordering (a <= b).
    comparable: list[list[str]] = Field(default_factory=list)
    # Derived columns: name -> SQL expression over the source's columns.
    derived: dict[str, str] = Field(default_factory=dict)
    # Columns masked before any sample leaves the machine.
    sensitive: list[str] = Field(default_factory=list)
    # Lookups joined into sample rows (column -> lookup), e.g. zone names for location ids.
    lookups: dict[str, LookupConfig] = Field(default_factory=dict)


class Config(Strict):
    detect: DetectConfig = Field(default_factory=DetectConfig)
    prove: ProveConfig = Field(default_factory=ProveConfig)
    propose: ProposeConfig = Field(default_factory=ProposeConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    pricing: dict[str, Price] = Field(default_factory=_default_pricing)
    sources: dict[str, SourceConfig] = Field(default_factory=dict)


DEFAULT_CONFIG = Path("config/default.yaml")


def load_dotenv(path: Path = Path(".env")) -> None:
    """Set KEY=VALUE lines from .env into the environment, without overriding existing vars."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load_config(path: Path | None = None) -> Config:
    """Load a YAML config. With no path, use config/default.yaml if present, else defaults."""
    if path is None:
        if not DEFAULT_CONFIG.exists():
            return Config()
        path = DEFAULT_CONFIG
    data = yaml.safe_load(path.read_text()) or {}
    return Config.model_validate(data)
