"""Propose step: LLM triage of slices, escalation of hard ones, and rule drafting.

Per source: one call to the triage model over every slice, then at most one call
to the mid-tier model for slices triage marked ambiguous or low-confidence, or
that span several columns. Every call goes through the budget ledger and the
result cache, and every proposed rule goes through the SQL guard.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
from pydantic import ValidationError

from dqtool.config import Config, Price, TierConfig
from dqtool.detect.detectors import Slice
from dqtool.logging import RunLog, to_json
from dqtool.profile.source import Source
from dqtool.propose.prompt import (
    PROMPT_VERSION,
    SampleReader,
    SliceGroup,
    group_slices,
    render,
    system_prompt,
)
from dqtool.propose.providers.base import LLMRequest, LLMResponse, Provider
from dqtool.propose.schema import ReviewOutput, SliceReview
from dqtool.safety.sql_guard import check_predicate


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@dataclass
class Ledger:
    """Tracks spend against the per-scan and monthly caps, and records every call."""

    config: Config
    month_spent: float = 0.0
    spent: float = 0.0
    calls: list[dict] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def price(self, model: str) -> Price:
        pricing = self.config.pricing
        if model in pricing:
            return pricing[model]
        # Responses can name a dated snapshot (claude-haiku-4-5-20251001): match by prefix.
        prefixes = [k for k in pricing if model.startswith(k + "-")]
        if prefixes:
            return pricing[max(prefixes, key=len)]
        # Unknown model (for example a refusal fallback): charge at the priciest known rate.
        return max(pricing.values(), key=lambda p: p.output)

    def cost(self, response: LLMResponse) -> float:
        p = self.price(response.model)
        return (
            response.input_tokens * p.input
            + response.cache_write_tokens * p.input * 1.25
            + response.cache_read_tokens * p.cache_read
            + response.output_tokens * p.output
        ) / 1e6

    def estimate(self, model: str, input_tokens: int, max_tokens: int) -> float:
        p = self.price(model)
        return (input_tokens * p.input + max_tokens * p.output) / 1e6

    def allows(self, estimate: float) -> str | None:
        """None if the call fits the budget, else the reason it does not."""
        cap = self.config.budget
        if self.spent + estimate > cap.max_usd_per_scan:
            return f"scan budget ${cap.max_usd_per_scan:.2f} (spent ${self.spent:.4f})"
        if self.month_spent + self.spent + estimate > cap.max_usd_per_month:
            return f"monthly budget ${cap.max_usd_per_month:.2f}"
        return None


class ResultCache:
    def __init__(self, directory: Path | None):
        self.directory = directory
        if directory:
            directory.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> str | None:
        if not self.directory:
            return None
        path = self.directory / f"{key}.json"
        return path.read_text() if path.exists() else None

    def put(self, key: str, text: str) -> None:
        if self.directory:
            (self.directory / f"{key}.json").write_text(text)


@dataclass
class Caller:
    provider: Provider
    ledger: Ledger
    cache: ResultCache
    log: RunLog

    def request_key(self, req: LLMRequest) -> str:
        schema = json.dumps(req.schema.model_json_schema(), sort_keys=True)
        parts = [
            self.provider.name,
            req.model,
            str(req.max_tokens),
            str(req.effort),
            str(req.fallbacks),
            PROMPT_VERSION,
            req.system,
            req.prompt,
            schema,
        ]
        return sha256("\x1f".join(parts))

    def call(self, req: LLMRequest, source: str, tier: str) -> ReviewOutput | None:
        key = self.request_key(req)
        base = {
            "source": source,
            "tier": tier,
            "provider": self.provider.name,
            "model": req.model,
            "prompt_version": PROMPT_VERSION,
            "request_key": key,
        }
        cached = self.cache.get(key)
        if cached is not None:
            self.log.event("llm_call", **base, cache_hit=True, cost_usd=0.0)
            self.ledger.calls.append({**base, "cache_hit": True, "cost_usd": 0.0})
            return ReviewOutput.model_validate_json(cached)

        for attempt in (1, 2):
            try:
                input_tokens = self.provider.count_tokens(req)
            except Exception as e:  # count is only for the budget check
                input_tokens = len(req.system + req.prompt) // 3
                self.log.event("llm_count_failed", **base, error=str(e))
            estimate = self.ledger.estimate(req.model, input_tokens, req.max_tokens)
            blocked = self.ledger.allows(estimate)
            if blocked:
                self.ledger.skipped.append({**base, "reason": blocked, "estimate_usd": estimate})
                self.log.event("budget_skip", **base, reason=blocked, estimate_usd=estimate)
                return None

            started = time.perf_counter()
            try:
                response = self.provider.complete(req)
            except Exception as e:
                self.log.event(
                    "llm_error", **base, attempt=attempt, error=f"{type(e).__name__}: {e}"
                )
                self.ledger.calls.append(
                    {**base, "attempt": attempt, "error": str(e), "cost_usd": 0.0}
                )
                return None
            latency = round(time.perf_counter() - started, 2)
            cost = self.ledger.cost(response)
            self.ledger.spent += cost
            record = {
                **base,
                "attempt": attempt,
                "cache_hit": False,
                "served_by": response.model,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "cache_read_tokens": response.cache_read_tokens,
                "cache_write_tokens": response.cache_write_tokens,
                "cost_usd": round(cost, 6),
                "latency_s": latency,
                "stop_reason": response.stop_reason,
                "request_id": response.request_id,
            }
            self.ledger.calls.append(record)
            self.log.event(
                "llm_call",
                **record,
                system_hash=sha256(req.system),
                prompt_hash=sha256(req.prompt),
                prompt=req.prompt,
                response=response.text,
            )

            if response.stop_reason == "refusal":
                self.log.event("llm_refusal", **base)
                return None
            try:
                parsed = req.schema.model_validate_json(response.text)
            except ValidationError as e:
                # Discarded, never repaired by guesswork; one retry.
                self.log.event(
                    "schema_failure",
                    **base,
                    attempt=attempt,
                    stop_reason=response.stop_reason,
                    errors=str(e)[:2000],
                )
                continue
            self.cache.put(key, response.text)
            return parsed
        return None


def _rule_id(raw: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-") or "rule"
    rule_id, n = base, 2
    while rule_id in taken:
        rule_id, n = f"{base}-{n}", n + 1
    taken.add(rule_id)
    return rule_id


def _needs_escalation(review: SliceReview | None, group: SliceGroup) -> bool:
    if review is None:
        return True
    # Multi-column means the slice's own condition spans columns, not slices merged by grouping.
    multi_column = len(group.slice.columns) > 1
    return review.verdict == "ambiguous" or review.confidence == "low" or multi_column


def propose_source(
    con: duckdb.DuckDBPyConnection,
    source: Source,
    profile: dict,
    slices: list[Slice],
    sources: dict[str, Source],
    config: Config,
    caller: Caller,
) -> dict:
    cfg = config.propose
    groups = group_slices([s for s in slices if s.source == source.name])[: cfg.max_slices]
    small_table = source.row_count <= cfg.full_table_max_rows
    result: dict = {
        "source": source.name,
        "slice_groups": len(groups),
        "reviews": [],
        "new_issues": [],
        "rules": [],
        "escalated": [],
        "skipped_reason": None,
    }
    if not groups and not small_table:
        result["skipped_reason"] = "quiet: no slices"
        caller.log.event("propose_skipped", source=source.name, reason="quiet")
        return result

    reader = SampleReader(con, source, sources, cfg)
    system = system_prompt()

    def request(tier: TierConfig, prompt: str) -> LLMRequest:
        return LLMRequest(
            system=system,
            prompt=prompt,
            schema=ReviewOutput,
            model=tier.model,
            max_tokens=tier.max_tokens,
            effort=tier.effort,
            fallbacks=tier.fallbacks,
        )

    triage = caller.call(
        request(cfg.triage, render(source, profile, groups, reader, cfg)), source.name, "triage"
    )
    by_id = {g.slice_id: g for g in groups}
    reviews: dict[str, tuple[SliceReview, str]] = {}
    issues: list[tuple] = []
    if triage:
        for r in triage.reviews:
            if r.slice_id in by_id:
                reviews[r.slice_id] = (r, cfg.triage.model)
        issues = [(i, cfg.triage.model) for i in triage.new_issues]

    hard = [g for g in groups if _needs_escalation(reviews.get(g.slice_id, (None,))[0], g)]
    unsure = [i for i, _ in issues if i.confidence == "low"]
    if cfg.escalate is not None and triage is not None and (hard or unsure):
        notes = {
            g.slice_id: reviews[g.slice_id][0].model_dump() for g in hard if g.slice_id in reviews
        }
        prompt = render(
            source,
            profile,
            hard,
            reader,
            cfg,
            first_pass=notes,
            candidate_issues=[i.model_dump() for i in unsure],
        )
        second = caller.call(request(cfg.escalate, prompt), source.name, "escalate")
        result["escalated"] = [g.slice_id for g in hard]
        if second:
            hard_ids = {g.slice_id for g in hard}
            for r in second.reviews:
                if r.slice_id in hard_ids:
                    reviews[r.slice_id] = (r, cfg.escalate.model)
            issues = [(i, m) for i, m in issues if i.confidence != "low"]
            issues += [(i, cfg.escalate.model) for i in second.new_issues]

    known_columns = {c.name for c in source.columns}
    taken: set[str] = set()

    def add_rule(rule, model, slice_ids, samples, confidence, reasoning) -> None:
        guard = check_predicate(rule.violation_when, known_columns)
        record = {
            "id": _rule_id(rule.rule_id, taken),
            "source": source.name,
            "description": rule.description,
            "violation_when": rule.violation_when,
            "severity": rule.severity,
            "max_violation_rate": config.prove.max_violation_rate,
            "status": "proposed" if guard.ok else "rejected_by_sql_guard",
            "confidence": confidence,
            "reasoning": reasoning,
            "evidence": {
                "slice_ids": slice_ids,
                "backtest_violation_rate": None,
                "sample_row_ids": samples,
            },
            "safety": {"ok": guard.ok, "errors": guard.errors, "columns": guard.columns},
            "provenance": {
                "proposed_by": "llm",
                "model": model,
                "prompt_version": PROMPT_VERSION,
                "approved_by": None,
            },
        }
        result["rules"].append(record)
        if not guard.ok:
            caller.log.event(
                "sql_guard_rejection",
                source=source.name,
                rule_id=record["id"],
                predicate=rule.violation_when,
                errors=guard.errors,
            )

    for g in groups:
        if g.slice_id not in reviews:
            result["reviews"].append(
                {"slice_id": g.slice_id, "verdict": None, "reason": "no review returned"}
            )
            continue
        review, model = reviews[g.slice_id]
        result["reviews"].append(
            {**review.model_dump(), "model": model, "grouped_with": [s.slice_id for s in g.also]}
        )
        if review.verdict == "error" and review.rule:
            add_rule(
                review.rule,
                model,
                [g.slice_id, *[s.slice_id for s in g.also]],
                g.slice.sample_row_ids,
                review.confidence,
                review.reasoning,
            )
    for issue, model in issues:
        result["new_issues"].append({**issue.model_dump(), "model": model})
        add_rule(issue.rule, model, [], [], issue.confidence, issue.reasoning)
    return result


def month_spend(out_dir: Path, month: str) -> float:
    """Spend recorded by earlier runs this month (run ids start with YYYYMM)."""
    total = 0.0
    for path in out_dir.glob(f"{month}*/cost.json"):
        try:
            total += json.loads(path.read_text()).get("spent_usd", 0.0)
        except (OSError, json.JSONDecodeError):
            continue
    return total


def cost_summary(ledger: Ledger) -> dict:
    return json.loads(
        json.dumps(
            {
                "spent_usd": round(ledger.spent, 6),
                "budget": ledger.config.budget.model_dump(),
                "month_spent_before_run_usd": round(ledger.month_spent, 6),
                "calls": ledger.calls,
                "skipped": ledger.skipped,
            },
            default=to_json,
        )
    )
