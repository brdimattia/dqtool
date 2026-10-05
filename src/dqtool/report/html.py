"""Static HTML report: one file, three layers (summary, findings, rules) plus an appendix.

Built from the same findings as the rule files, so stakeholders, analysts, and
engineers each stop at the depth they need (spec: Users and workflow). No
scripts and no external assets, so it opens anywhere, offline.
"""

from __future__ import annotations

import html
import json
from datetime import UTC, datetime
from typing import Any

from dqtool.config import Config
from dqtool.profile.source import quote
from dqtool.prove.backtest import sandbox

MASK = "<masked>"

# How the overall risk level is rated, shown in the report so it can be checked.
RISK_RULE = (
    "High: instruction-like text was found, or a surviving error-severity rule flags at least"
    " 0.1% of a source's rows or a whole period. Medium: any other surviving error-severity"
    " rule. Low: only warnings, or nothing."
)


def e(value: Any) -> str:
    if isinstance(value, float):
        value = f"{value:.6g}"
    return html.escape("" if value is None else str(value))


def priority(rule: dict) -> tuple:
    """Order for the summary: injection text, whole-period shifts, errors, then size."""
    ev = rule["evidence"]
    injection = any(".instruction_text." in s for s in ev.get("slice_ids", [])) or (
        "regexp_matches" in rule["violation_when"] and "instruction" in rule["id"]
    )
    return (
        not injection,
        ev.get("kind") != "period",
        rule["severity"] != "error",
        -(ev.get("backtest_violation_count") or 0),
    )


def pct(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}%}"


def risk_level(rules: list[dict], slices: list[dict]) -> str:
    survived = [r for r in rules if r["status"] == "survived"]
    canary = any(s["detector"] == "instruction_text" for s in slices)
    errors = [r for r in survived if r["severity"] == "error"]
    big = [
        r
        for r in errors
        if (r["evidence"].get("backtest_violation_rate") or 0) >= 0.001
        or r["evidence"].get("kind") == "period"
    ]
    if canary or big:
        return "High"
    return "Medium" if errors else "Low"


def _sample_rows(config: Config, rules: list[dict]) -> dict[str, tuple[list[str], list[tuple]]]:
    """Example rows per surviving rule: its own columns plus the row id, sensitive masked."""
    n = config.review.report_sample_rows
    survived = [r for r in rules if r["status"] == "survived"]
    if not n or not survived:
        return {}
    names = sorted({r["source"] for r in survived})
    con, sources = sandbox(config, names)
    out = {}
    try:
        for r in survived:
            source = sources[r["source"]]
            ids = r["evidence"].get("sample_row_ids", [])[:n]
            if not ids or not source.row_id:
                continue
            cols = [source.row_id] + [
                c for c in r.get("safety", {}).get("columns", []) if c != source.row_id
            ]
            if source.config.time_column and source.config.time_column not in cols:
                cols.insert(1, source.config.time_column)
            known = {c.name.lower(): c.name for c in source.columns}
            cols = [known[c.lower()] for c in cols if c.lower() in known]
            sensitive = set(source.config.sensitive)
            rows = con.execute(
                f"SELECT {', '.join(quote(c) for c in cols)} FROM {source.view}"
                f" WHERE {quote(source.row_id)} IN (SELECT unnest($ids))"
                f" ORDER BY {quote(source.row_id)}",
                {"ids": ids},
            ).fetchall()
            rows = [
                tuple(
                    MASK if c in sensitive and v is not None else v
                    for c, v in zip(cols, row, strict=True)
                )
                for row in rows
            ]
            out[r["id"]] = (cols, rows)
    finally:
        con.close()
    return out


def _table(headers: list[str], rows: list[list[str]], cls: str = "") -> str:
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return (
        f'<div class="scroll"><table class="{cls}"><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _missing(profiles: dict[str, dict]) -> list[str]:
    """What is absent: mostly-empty columns and thin periods (spec: Missing information)."""
    notes = []
    for name, prof in profiles.items():
        for col, stats in prof["columns"].items():
            rate = stats.get("null_rate") or 0
            if rate >= 0.05:
                notes.append(f"<code>{e(name)}.{e(col)}</code> is empty in {pct(rate, 1)} of rows")
        tr = prof.get("time_range") or {}
        core = set(tr.get("core_periods") or [])
        thin = {p: n for p, n in (tr.get("periods") or {}).items() if p not in core}
        if thin:
            notes.append(
                f"<code>{e(name)}</code> has {sum(thin.values()):,} rows in {len(thin)} period(s)"
                " outside its main time range"
            )
    return notes


CSS = """
:root { --bg:#fbfaf8; --fg:#1d1d1f; --muted:#6b6b70; --line:#e4e2dd; --card:#ffffff;
  --code:#f3f1ec; --high:#b42318; --med:#b54708; --low:#067647; --accent:#3e4c9a; }
@media (prefers-color-scheme: dark) { :root { --bg:#161618; --fg:#ececee; --muted:#9b9ba1;
  --line:#2c2c30; --card:#1e1e21; --code:#26262a; --high:#f97066; --med:#fdb022;
  --low:#47cd89; --accent:#a4b0f5; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.55 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 1040px; margin: 0 auto; padding: 32px 20px 64px; }
h1 { font-size: 26px; margin: 0 0 4px; } h2 { font-size: 20px; margin: 40px 0 12px; }
h3 { font-size: 16px; margin: 0 0 6px; }
.meta, .muted { color: var(--muted); font-size: 13px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 18px 20px; margin: 12px 0; }
.risk { display:inline-block; font-weight:700; padding:2px 10px; border-radius:999px;
  border:2px solid currentColor; }
.risk.High { color: var(--high); } .risk.Medium { color: var(--med); }
.risk.Low { color: var(--low); }
.sev { font-size:12px; font-weight:600; text-transform:uppercase; letter-spacing:.04em; }
.sev.error { color: var(--high); } .sev.warn { color: var(--med); }
code, pre { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px;
  background: var(--code); border-radius: 4px; }
code { padding: 1px 4px; }
pre { padding: 10px 12px; white-space: pre-wrap; word-break: break-word; margin: 8px 0; }
.scroll { overflow-x: auto; margin: 8px 0; }
table { border-collapse: collapse; width: 100%; font-size: 13px; }
th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line);
  vertical-align: top; }
th { color: var(--muted); font-weight: 600; }
.stats { display:flex; flex-wrap:wrap; gap: 8px 28px; margin: 8px 0; }
.stats div b { display:block; font-size: 20px; }
details > summary { cursor: pointer; font-weight: 600; margin: 8px 0; }
ul { padding-left: 20px; } li { margin: 4px 0; }
"""


def build_report(
    config: Config,
    run_id: str,
    profiles: dict[str, dict],
    slices: list[dict],
    proposals: list[dict],
    rules: list[dict],
    results: dict | None,
    cost: dict | None,
    rule_files: dict[str, str] | None = None,
) -> str:
    rule_files = rule_files or {}
    survived = [r for r in rules if r["status"] == "survived"]
    survived.sort(key=priority)
    level = risk_level(rules, slices)
    samples = _sample_rows(config, rules)
    rows_by_source = {n: p["row_count"] for n, p in profiles.items()}
    out = [
        "<!doctype html><html lang='en'><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>Data risk report {e(run_id)}</title><style>{CSS}</style><main>"
    ]

    sources = ", ".join(f"{e(n)} ({c:,} rows)" for n, c in rows_by_source.items())
    out.append(
        f"<h1>Pre-ingestion risk report</h1><div class='meta'>Run <code>{e(run_id)}</code>"
        f" · {e(sources)} · generated {datetime.now(UTC):%Y-%m-%d %H:%M} UTC</div>"
    )

    # Layer 1: summary for stakeholders.
    errors = sum(r["severity"] == "error" for r in survived)
    out.append("<h2>Summary</h2><div class='card'>")
    out.append(f"<p>Overall risk: <span class='risk {level}'>{level}</span></p>")
    out.append(
        f"<p>The scan proposes <b>{len(survived)}</b> rules for review ({errors} errors,"
        f" {len(survived) - errors} warnings). Top issues:</p><ul>"
    )
    for r in survived[:5]:
        ev = r["evidence"]
        n = ev.get("backtest_violation_count") or 0
        scope = f"{n:,} row{'s' if n != 1 else ''}"
        if ev.get("kind") == "period":
            scope = f"period {e(ev['period'])}, {scope}"
        out.append(
            f"<li>{e(r['description'])} <span class='muted'>({scope},"
            f" {pct(ev.get('backtest_violation_rate'))})</span></li>"
        )
    out.append(f"</ul><p class='muted'>How risk is rated: {e(RISK_RULE)}</p></div>")

    missing = _missing(profiles)
    if missing:
        out.append(
            "<h3>What is missing</h3><ul>" + "".join(f"<li>{m}</li>" for m in missing) + "</ul>"
        )

    # Layer 2: findings for analysts.
    out.append(
        "<h2>Findings</h2><p class='muted'>Each finding with its evidence: how many rows,"
        " why it was flagged, and example rows.</p>"
    )
    for r in survived:
        ev = r["evidence"]
        out.append(f"<div class='card' id='{e(r['id'])}'>")
        out.append(
            f"<span class='sev {e(r['severity'])}'>{e(r['severity'])}</span>"
            f"<h3>{e(r['description'])}</h3>"
        )
        out.append(
            f"<div class='stats'><div><b>{ev.get('backtest_violation_count', 0):,}</b>rows</div>"
            f"<div><b>{pct(ev.get('backtest_violation_rate'))}</b>of {e(r['source'])}</div>"
            f"<div><b>{e(r.get('confidence') or '-')}</b>confidence</div></div>"
        )
        out.append(f"<p>{e(r.get('reasoning'))}</p>")
        if ev.get("kind") == "period":
            periods = ev.get("violations_by_period") or {}
            out.append("<p class='muted'>Rows flagged per period:</p>")
            out.append(_table(["Period", "Rows"], [[e(p), f"{n:,}"] for p, n in periods.items()]))
        if r["id"] in samples:
            cols, rows = samples[r["id"]]
            out.append(f"<p class='muted'>Example rows ({len(rows)}):</p>")
            out.append(_table([e(c) for c in cols], [[e(v) for v in row] for row in rows]))
        out.append(
            f"<p class='muted'>Rule: <a href='#rule-{e(r['id'])}'>{e(r['id'])}</a></p></div>"
        )

    # Layer 3: rules for engineers.
    out.append(
        "<h2>Rules</h2><p class='muted'>Approve, edit, or reject each rule file in the pull"
        " request. A rule flags the rows where <code>violation_when</code> is true.</p>"
    )
    for r in survived:
        ev, prov = r["evidence"], r["provenance"]
        out.append(f"<div class='card' id='rule-{e(r['id'])}'>")
        out.append(
            f"<h3><code>{e(r['id'])}</code> <span class='sev {e(r['severity'])}'>"
            f"{e(r['severity'])}</span></h3>"
        )
        out.append(f"<pre>{e(r['violation_when'])}</pre>")
        facts = [
            ["Source", e(r["source"])],
            [
                "Backtest",
                f"{ev.get('backtest_violation_count', 0):,} rows,"
                f" {pct(ev.get('backtest_violation_rate'))}"
                f" (noise limit {pct(r['max_violation_rate'], 1)})",
            ],
            [
                "Kind",
                e(ev.get("kind"))
                + (
                    f" ({e(ev['period'])}, exempt from the noise limit)"
                    if ev.get("kind") == "period"
                    else ""
                ),
            ],
            ["Proposed by", f"{e(prov.get('model'))}, prompt {e(prov.get('prompt_version'))}"],
            [
                "From slices",
                ", ".join(f"<code>{e(s)}</code>" for s in ev.get("slice_ids", [])) or "new issue",
            ],
        ]
        if r["id"] in rule_files:
            facts.append(["File", f"<code>{e(rule_files[r['id']])}</code>"])
        out.append(_table(["", ""], facts, "facts"))
        out.append("</div>")

    # Appendix: everything else, so nothing is a black box.
    out.append("<h2>Appendix</h2>")
    dropped = [r for r in rules if r["status"] != "survived"]
    out.append(f"<details><summary>Rules dropped automatically ({len(dropped)})</summary>")
    if dropped:
        out.append(
            _table(
                ["Rule", "Status", "Detail", "Predicate"],
                [
                    [
                        f"<code>{e(r['id'])}</code>",
                        e(r["status"]),
                        e(
                            r["evidence"].get("error")
                            or "; ".join(r.get("safety", {}).get("errors", []))
                            or pct(r["evidence"].get("backtest_violation_rate"))
                        ),
                        f"<code>{e(r['violation_when'])}</code>",
                    ]
                    for r in dropped
                ],
            )
        )
    out.append("</details>")

    legit = [
        (p["source"], rv)
        for p in proposals
        for rv in p["reviews"]
        if rv.get("verdict") == "legitimate"
    ]
    out.append(f"<details><summary>Slices judged legitimate ({len(legit)})</summary>")
    out.append(
        _table(
            ["Slice", "Confidence", "Reasoning"],
            [
                [
                    f"<code>{e(rv['slice_id'])}</code>",
                    e(rv.get("confidence")),
                    e(rv.get("reasoning")),
                ]
                for _, rv in legit
            ],
        )
    )
    out.append("</details>")

    out.append(f"<details><summary>All detector slices ({len(slices)})</summary>")
    out.append(
        _table(
            ["Slice", "Rows", "Share", "Description"],
            [
                [
                    f"<code>{e(s['slice_id'])}</code>",
                    f"{s['flag_count']:,}",
                    pct(s["flag_rate"]),
                    e(s["description"]),
                ]
                for s in slices
            ],
        )
    )
    out.append("</details>")

    if cost:
        calls = cost.get("calls", [])
        out.append(
            f"<details><summary>LLM cost: ${cost['spent_usd']:.4f} of"
            f" ${cost['budget']['max_usd_per_scan']:.2f} budget, {len(calls)} call(s)</summary>"
        )
        out.append(
            _table(
                ["Source", "Tier", "Model", "Input tokens", "Output tokens", "Cost", "Cached"],
                [
                    [
                        e(c.get("source")),
                        e(c.get("tier")),
                        e(c.get("served_by") or c.get("model")),
                        f"{c.get('input_tokens') or 0:,}",
                        f"{c.get('output_tokens') or 0:,}",
                        f"${c.get('cost_usd', 0):.4f}",
                        "yes" if c.get("cache_hit") else "no",
                    ]
                    for c in calls
                ],
            )
        )
        if cost.get("skipped"):
            out.append(f"<p>Skipped for budget: {e(json.dumps(cost['skipped']))}</p>")
        out.append("</details>")

    if results:
        m = results["metrics"]
        out.append(
            "<details><summary>Evaluation against planted errors: recall by type"
            f" {pct(m['planted_recall_by_type'], 0)}, by plant"
            f" {pct(m['planted_recall_by_plant'], 0)}</summary>"
        )
        out.append(
            _table(
                ["Plant", "Type", "Caught", "Recall", "Rules"],
                [
                    [
                        e(p["plant_id"]),
                        e(p["type"]),
                        "yes" if p["caught"] else "no",
                        pct(p["recall"], 0),
                        e(", ".join(p["rules"])),
                    ]
                    for p in results["plants"]
                ],
            )
        )
        out.append("</details>")

    out.append("</main></html>")
    return "\n".join(out)
