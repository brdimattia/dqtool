"""Render a source's profile, slices, and samples into a prompt.

All dataset content goes inside delimited blocks, JSON-encoded, with angle
brackets escaped so a value can never close a block, and truncated per value
(spec: Security, layer 1). Sensitive columns are masked before rendering.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

import duckdb

from dqtool.config import ProposeConfig
from dqtool.detect.detectors import Slice
from dqtool.logging import to_json
from dqtool.profile.source import Source, quote

PROMPT_VERSION = "v1"
MASK = "<masked>"


def system_prompt(version: str = PROMPT_VERSION) -> str:
    return resources.files("dqtool.propose.prompts").joinpath(f"system_{version}.md").read_text()


def _compact(value: Any) -> Any:
    """Floats to 6 significant digits; derived columns otherwise print 17."""
    if isinstance(value, float):
        return float(f"{value:.6g}")
    return value


def escape(value: Any, max_chars: int) -> str:
    """JSON-encode a value for a data block: truncated, no raw angle brackets."""
    if isinstance(value, str) and len(value) > max_chars:
        value = value[:max_chars] + "...[truncated]"
    text = json.dumps(_compact(value), default=to_json, ensure_ascii=False)
    return text.replace("<", "\\u003c").replace(">", "\\u003e")


def data_rows(rows: list[dict], max_chars: int) -> str:
    """A JSON header line of column names, then one JSON array of values per row."""
    if not rows:
        return ""
    names = list(rows[0])
    lines = ["[" + ", ".join(escape(n, max_chars) for n in names) + "]"]
    for row in rows:
        lines.append("[" + ", ".join(escape(row[n], max_chars) for n in names) + "]")
    return "\n".join(lines)


@dataclass
class SliceGroup:
    """Slices that flag the same rows, shown to the LLM once."""

    slice: Slice
    also: list[Slice] = field(default_factory=list)

    @property
    def slice_id(self) -> str:
        return self.slice.slice_id

    @property
    def columns(self) -> list[str]:
        cols = list(self.slice.columns)
        for s in self.also:
            cols += [c for c in s.columns if c not in cols]
        return cols


def group_slices(slices: list[Slice]) -> list[SliceGroup]:
    """Merge slices whose predicates select the same rows (same count and sample)."""
    groups: dict[tuple, SliceGroup] = {}
    for s in slices:
        key = (s.source, s.flag_count, tuple(s.sample_row_ids)) if s.sample_row_ids else s.slice_id
        if key in groups:
            groups[key].also.append(s)
        else:
            groups[key] = SliceGroup(s)
    return list(groups.values())


class SampleReader:
    """Fetches sample rows for a source: lookups joined in, sensitive columns masked."""

    def __init__(
        self,
        con: duckdb.DuckDBPyConnection,
        source: Source,
        sources: dict[str, Source],
        cfg: ProposeConfig,
    ):
        self.con = con
        self.source = source
        self.cfg = cfg
        self.sensitive = set(source.config.sensitive)
        selects = ["s.*"]
        joins = []
        for i, (col, lookup) in enumerate(source.config.lookups.items()):
            if lookup.source not in sources:
                continue
            alias = f"l{i}"
            joins.append(
                f"LEFT JOIN {sources[lookup.source].view} {alias}"
                f" ON s.{quote(col)} = {alias}.{quote(lookup.key)}"
            )
            for lc in lookup.columns:
                name = f"{col}_{lc}"
                selects.append(f"{alias}.{quote(lc)} AS {quote(name)}")
                if lc in sources[lookup.source].config.sensitive:
                    self.sensitive.add(name)
        self.base = f"SELECT {', '.join(selects)} FROM {source.view} s {' '.join(joins)}"

    def _fetch(self, sql: str, params: list | dict | None = None) -> list[dict]:
        cursor = self.con.execute(sql, params or [])
        names = [d[0] for d in cursor.description]
        return [
            {
                n: (MASK if n in self.sensitive and v is not None else v)
                for n, v in zip(names, row, strict=True)
            }
            for row in cursor.fetchall()
        ]

    def rows(self, row_ids: list) -> list[dict]:
        if not self.source.row_id or not row_ids:
            return []
        rid = quote(self.source.row_id)
        ids = row_ids[: self.cfg.sample_rows]
        return self._fetch(
            f"{self.base} WHERE s.{rid} IN (SELECT unnest($ids)) ORDER BY s.{rid}", {"ids": ids}
        )

    def full_table(self) -> list[dict]:
        order = f" ORDER BY s.{quote(self.source.row_id)}" if self.source.row_id else ""
        return self._fetch(self.base + order)


def _column_lines(source: Source, profile: dict) -> str:
    lines = []
    for col in source.columns:
        stats = profile["columns"][col.name]
        parts = [f"{col.name}: {col.sql_type}, {col.kind}"]
        if col.derived:
            parts.append(f"derived as {source.config.derived[col.name]}")
        if col.name in source.config.sensitive:
            parts.append("sensitive, masked")
        parts.append(f"null_rate={stats.get('null_rate', 0):.3f}")
        if col.kind == "numeric":
            parts.append(
                f"min={stats['min']} p01={stats['p01']} median={stats['median']}"
                f" p99={stats['p99']} max={stats['max']}"
            )
        elif "top_values" in stats and col.name not in source.config.sensitive:
            top = ", ".join(f"{v['value']} ({v['count']})" for v in stats["top_values"][:5])
            parts.append(f"distinct={stats['distinct']} top: {top}")
        elif col.kind == "temporal":
            parts.append(f"min={stats['min']} max={stats['max']}")
        lines.append("- " + "; ".join(str(p) for p in parts))
    return "\n".join(lines)


def render(
    source: Source,
    profile: dict,
    groups: list[SliceGroup],
    reader: SampleReader,
    cfg: ProposeConfig,
    first_pass: dict[str, dict] | None = None,
    candidate_issues: list[dict] | None = None,
) -> str:
    max_chars = cfg.max_value_chars
    out = [
        f"Source: {source.name} ({source.row_count:,} rows).",
        "Columns you may use in rules (with profile statistics):",
        "<profile>",
        _column_lines(source, profile),
    ]
    time_range = profile.get("time_range")
    if time_range and time_range.get("start"):
        out.append(
            f"Main time range of {profile['time_column']}: {time_range['start']} to"
            f" {time_range['end']}; rows per period: {json.dumps(time_range['periods'])}"
        )
    out.append("</profile>")

    if groups:
        out.append(f"\n{len(groups)} slice(s) to review:")
    for g in groups:
        s = g.slice
        out.append(f'\n<slice id="{s.slice_id}">')
        out.append(f"detector: {s.detector}")
        out.append(f"description: {escape(s.description, 1000)}")
        out.append(f"predicate: {s.predicate}")
        out.append(f"flagged rows: {s.flag_count:,} ({s.flag_rate:.4%} of the source)")
        if s.stats:
            out.append(f"statistics: {escape(s.stats, 4000)}")
        for other in g.also:
            out.append(
                f"same rows also flagged by: {other.detector}: {escape(other.description, 1000)}"
            )
        if first_pass and s.slice_id in first_pass:
            out.append(f"first-pass reviewer's note: {escape(first_pass[s.slice_id], 2000)}")
        rows = reader.rows(s.sample_row_ids)
        if rows:
            out.append(f"sample of flagged rows ({len(rows)}):")
            out.append("<data>")
            out.append(data_rows(rows, max_chars))
            out.append("</data>")
        out.append("</slice>")

    if source.row_count <= cfg.full_table_max_rows:
        out.append(f"\nThe full {source.name} table ({source.row_count} rows):")
        out.append("<table>")
        out.append(data_rows(reader.full_table(), max_chars))
        out.append("</table>")

    if candidate_issues:
        out.append(
            "\nA first-pass reviewer raised these possible issues with low confidence."
            " Confirm or drop each one, and report confirmed ones as new_issues:"
        )
        out.append("<data>")
        out.append(data_rows(candidate_issues, 2000))
        out.append("</data>")

    out.append(
        "\nReview every slice above and report any further issues as new_issues,"
        " following your instructions."
    )
    return "\n".join(out)
