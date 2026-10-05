"""Deterministic detectors that turn a profile into suspicious slices. No LLM.

Each slice is a SQL predicate over the source view (derived columns included)
plus how many rows it flags and a deterministic sample of their row ids. Slices
are leads for the Propose step, not final rules: a slice may flag a lot of real
but legitimate data, and its flag_rate says so.
"""

from __future__ import annotations

import math
from statistics import median
from typing import Any

import duckdb
from pydantic import BaseModel

from dqtool.config import DetectConfig
from dqtool.profile.source import Column, Source, quote, sql_literal
from dqtool.safety import canary

MAD_SCALE = 0.6745  # MAD of a normal distribution, in standard deviations
UNUSUAL_CHARS = r"[^A-Za-z0-9 /&'.,()-]"
TEXT_MAX_SHARE = 0.05  # text checks only report patterns this rare


class Slice(BaseModel):
    slice_id: str
    source: str
    detector: str
    columns: list[str]
    description: str
    predicate: str
    flag_count: int
    flag_rate: float
    sample_row_ids: list[Any]
    stats: dict[str, Any] = {}


def _bound(value: float) -> float:
    """Round a threshold to 4 significant digits so predicates stay readable."""
    if value == 0 or not math.isfinite(value):
        return value
    return round(value, 3 - int(math.floor(math.log10(abs(value)))))


class Detector:
    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        source: Source,
        profile: dict,
        cfg: DetectConfig,
    ):
        self.con = con
        self.source = source
        self.profile = profile
        self.cfg = cfg
        self.slices: list[Slice] = []

    def stats(self, col: Column) -> dict:
        return self.profile["columns"][col.name]

    def add(
        self,
        detector: str,
        key: str,
        columns: list[str],
        description: str,
        predicate: str,
        stats: dict | None = None,
    ) -> None:
        view = self.source.view
        count = self.con.execute(f"SELECT count(*) FROM {view} WHERE {predicate}").fetchone()[0]
        if count == 0:
            return
        samples: list = []
        if self.source.row_id:
            rid = quote(self.source.row_id)
            samples = [
                r[0]
                for r in self.con.execute(
                    f"SELECT {rid} FROM {view} WHERE {predicate}"
                    f" ORDER BY hash({rid}), {rid} LIMIT {self.cfg.sample_rows}"
                ).fetchall()
            ]
        self.slices.append(
            Slice(
                slice_id=f"{self.source.name}.{detector}.{key}",
                source=self.source.name,
                detector=detector,
                columns=columns,
                description=description,
                predicate=predicate,
                flag_count=count,
                flag_rate=count / self.source.row_count,
                sample_row_ids=samples,
                stats=stats or {},
            )
        )

    # --- single column ---------------------------------------------------

    def robust_outliers(self) -> None:
        k = self.cfg.mad_threshold
        for col in self.source.of_kind("numeric"):
            s = self.stats(col)
            if not s["mad"]:
                continue  # dominant_value covers columns with no spread
            c = quote(col.name)
            spread = s["mad"] / MAD_SCALE
            high = s["median"] + k * spread
            low = s["median"] - k * spread
            # Negatives are reported by negative_values, so they don't count against skew.
            mostly_positive = s["negative_share"] < 0.5
            p01 = max(s["p01"], 0) if mostly_positive else s["p01"]
            skewed = (
                s["log1p_mad"]
                and mostly_positive
                and s["p99"] - s["median"] > self.cfg.skew_ratio * (s["median"] - p01)
            )
            scale = "raw"
            if skewed:
                high = math.expm1(s["log1p_median"] + k * s["log1p_mad"] / MAD_SCALE)
                scale = "log1p"
            info = {"median": s["median"], "mad": s["mad"], "scale": scale, "threshold": k}
            if high < s["max"]:
                self.add(
                    "robust_outlier",
                    f"{col.name}.high",
                    [col.name],
                    f"{col.name} above {_bound(high)}, more than {k} robust deviations"
                    f" above the median ({s['median']}, {scale} scale).",
                    f"{c} > {_bound(high)}",
                    {**info, "bound": _bound(high)},
                )
            # Negatives in mostly non-negative columns are left to negative_values.
            if low > s["min"] and not (low <= 0 and mostly_positive):
                self.add(
                    "robust_outlier",
                    f"{col.name}.low",
                    [col.name],
                    f"{col.name} below {_bound(low)}, more than {k} robust deviations"
                    f" below the median ({s['median']}).",
                    f"{c} < {_bound(low)}",
                    {**info, "bound": _bound(low)},
                )

    def _measures(self) -> list[Column]:
        """Numeric-typed columns that are quantities, not configured codes or ids."""
        return [
            c
            for c in self.source.columns
            if c.is_numeric_type and c.kind != "id" and c.name not in self.source.config.categorical
        ]

    def negative_values(self) -> None:
        for col in self._measures():
            share = self.con.execute(
                f"SELECT avg(({quote(col.name)} < 0)::INT) FROM {self.source.view}"
            ).fetchone()[0]
            if share and share <= self.cfg.negative_max_share:
                self.add(
                    "negative_values",
                    col.name,
                    [col.name],
                    f"{col.name} is negative, which is rare in this column.",
                    f"{quote(col.name)} < 0",
                    {"negative_share": share},
                )

    def dominant_value(self) -> None:
        for col in self._measures():
            c = quote(col.name)
            mode = self.con.execute(f"SELECT mode({c}) FROM {self.source.view}").fetchone()[0]
            if mode is None:
                continue
            share = self.con.execute(
                f"SELECT count(*) FILTER ({c} = ?) / count({c}) FROM {self.source.view}", [mode]
            ).fetchone()[0]
            if self.cfg.dominant_value_share <= share < 1:
                self.add(
                    "dominant_value",
                    col.name,
                    [col.name],
                    f"{col.name} is {mode} in {share:.1%} of rows; other values are unusual.",
                    f"{c} <> {sql_literal(mode)}",
                    {"dominant_value": mode, "share": share},
                )

    def rare_values(self) -> None:
        for col in self.source.of_kind("categorical"):
            c = quote(col.name)
            non_null = self.stats(col)["non_null"]
            if not non_null:
                continue
            rare = self.con.execute(
                f"SELECT {c}, count(*) AS n FROM {self.source.view} WHERE {c} IS NOT NULL"
                f" GROUP BY 1 HAVING n < ? ORDER BY 1",
                [self.cfg.rare_value_share * non_null],
            ).fetchall()
            if not rare:
                continue
            values = ", ".join(sql_literal(v) for v, _ in rare)
            self.add(
                "rare_values",
                col.name,
                [col.name],
                f"{col.name} takes {len(rare)} value(s) that each appear in under"
                f" {self.cfg.rare_value_share:.2%} of rows.",
                f"{c} IN ({values})",
                {"values": [{"value": v, "count": n} for v, n in rare]},
            )

    def time_out_of_range(self) -> None:
        time_range = self.profile.get("time_range")
        if not time_range or not time_range["start"]:
            return
        t = quote(self.source.config.time_column)
        start, end = time_range["start"], time_range["end"]
        self.add(
            "time_out_of_range",
            self.source.config.time_column,
            [self.source.config.time_column],
            f"{self.source.config.time_column} outside the data's main range"
            f" [{start:%Y-%m-%d}, {end:%Y-%m-%d}).",
            f"{t} < TIMESTAMP '{start.isoformat()}' OR {t} >= TIMESTAMP '{end.isoformat()}'",
            {"start": start, "end": end},
        )

    # --- cross column ----------------------------------------------------

    def order_violations(self) -> None:
        temporal = [c.name for c in self.source.of_kind("temporal")]
        groups = [temporal, *self.source.config.comparable]
        pairs = {(a, b) for group in groups for i, a in enumerate(group) for b in group[i + 1 :]}
        for a, b in sorted(pairs):
            qa, qb = quote(a), quote(b)
            a_le_b, b_le_a = self.con.execute(
                f"SELECT avg(({qa} <= {qb})::INT), avg(({qb} <= {qa})::INT)"
                f" FROM {self.source.view} WHERE {qa} IS NOT NULL AND {qb} IS NOT NULL"
            ).fetchone()
            if a_le_b is None:
                continue
            for lo, hi, share, other in ((a, b, a_le_b, b_le_a), (b, a, b_le_a, a_le_b)):
                if self.cfg.order_min_share <= share < 1 and other < self.cfg.order_min_share:
                    self.add(
                        "order_violation",
                        f"{hi}_lt_{lo}",
                        [lo, hi],
                        f"{hi} is less than {lo}, though {lo} <= {hi} in {share:.2%} of rows.",
                        f"{quote(hi)} < {quote(lo)}",
                        {"expected": f"{lo} <= {hi}", "holds_share": share},
                    )

    # --- statistical -----------------------------------------------------

    def drift(self) -> None:
        by_period = self.profile.get("by_period") or {}
        if len(by_period) < 2:
            return
        t = quote(self.source.config.time_column)
        unit = self.source.config.period
        periods = sorted(by_period)

        def period_pred(p: str) -> str:
            return f"{t} >= TIMESTAMP '{p}' AND {t} < TIMESTAMP '{p}' + INTERVAL 1 {unit}"

        for col in self.source.of_kind("numeric"):
            series = {p: by_period[p]["median"][col.name] for p in periods}
            values = [v for v in series.values() if v is not None]
            if len(values) < 2:
                continue
            # Median over all periods: with few periods, a leave-one-out reference would
            # let one shifted period make the others look shifted too.
            ref = median(values)
            for p in periods:
                if series[p] is None or ref == 0:
                    continue
                change = (series[p] - ref) / abs(ref)
                if abs(change) > self.cfg.drift_relative_change:
                    self.add(
                        "drift_median",
                        f"{col.name}.{p[:10]}",
                        [col.name],
                        f"Median {col.name} in period {p[:10]} is {series[p]:.4g},"
                        f" {change:+.0%} vs the median across periods ({ref:.4g}).",
                        period_pred(p),
                        {
                            "period": p[:10],
                            "median": series[p],
                            "reference": ref,
                            "relative_change": change,
                            "medians_by_period": series,
                        },
                    )

        for col in self.source.columns:
            if col.kind == "id":
                continue
            series = {p: by_period[p]["null_rate"][col.name] for p in periods}
            ref = median(series.values())
            for p in periods:
                change = series[p] - ref
                if abs(change) > self.cfg.drift_null_rate_change:
                    self.add(
                        "drift_null_rate",
                        f"{col.name}.{p[:10]}",
                        [col.name],
                        f"Null rate of {col.name} in period {p[:10]} is {series[p]:.1%},"
                        f" vs a median of {ref:.1%} across periods.",
                        period_pred(p) + f" AND {quote(col.name)} IS NULL",
                        {
                            "period": p[:10],
                            "null_rate": series[p],
                            "reference": ref,
                            "null_rate_by_period": series,
                        },
                    )

    # --- text ------------------------------------------------------------

    def text_checks(self) -> None:
        for col in self.source.columns:
            if not col.is_text_type or col.kind == "id":
                continue
            c = quote(col.name)
            self.add(
                "instruction_text",
                col.name,
                [col.name],
                f"{col.name} contains text that reads like instructions to an AI system"
                " (possible prompt injection).",
                canary.sql_predicate(c),
            )
            unusual = f"regexp_matches({c}, {sql_literal(UNUSUAL_CHARS)})"
            share = self.con.execute(
                f"SELECT avg(({unusual})::INT) FROM {self.source.view}"
            ).fetchone()[0]
            if share and share <= TEXT_MAX_SHARE:
                self.add(
                    "unusual_characters",
                    col.name,
                    [col.name],
                    f"{col.name} contains characters rarely seen in this column.",
                    unusual,
                    {"share": share},
                )
            length = self.stats(col).get("length")
            if col.kind == "text" and length and length["mad"]:
                bound = _bound(
                    length["median"] + self.cfg.mad_threshold * length["mad"] / MAD_SCALE
                )
                if bound < length["max"]:
                    self.add(
                        "text_length",
                        col.name,
                        [col.name],
                        f"{col.name} is unusually long (over {bound} characters;"
                        f" median {length['median']}).",
                        f"length({c}) > {bound}",
                        {"bound": bound, "median_length": length["median"]},
                    )

    def run(self) -> list[Slice]:
        self.robust_outliers()
        self.negative_values()
        self.dominant_value()
        self.rare_values()
        self.time_out_of_range()
        self.order_violations()
        self.drift()
        self.text_checks()
        return self.slices


def detect(
    con: duckdb.DuckDBPyConnection, source: Source, profile: dict, cfg: DetectConfig
) -> list[Slice]:
    return Detector(con, source, profile, cfg).run()
