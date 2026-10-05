"""Injection canary: patterns for data values that read like instructions to an LLM.

A hit is reported as a finding in its own right (spec: Security, layer 6).
Patterns are RE2 syntax (DuckDB regexp_matches) and matched against lower(value).
"""

from __future__ import annotations

INSTRUCTION_PATTERNS = (
    r"ignore (all |any )?(the )?(previous|prior|above|earlier) (instructions|rules|prompts?)",
    r"disregard (all |any )?(the )?(previous|prior|above|earlier)",
    r"system (note|prompt|message|override)\s*:",
    r"(approve|accept) (every|all|each) ",
    r"do not (flag|report|mention)",
    r"you are (now )?(an?|the) [a-z]+ (assistant|model|ai)",
    r"</?(system|instructions?|prompt)>",
)

PATTERN = "|".join(f"(?:{p})" for p in INSTRUCTION_PATTERNS)


def sql_predicate(column_sql: str) -> str:
    """SQL condition true when the column's value looks like an instruction."""
    escaped = PATTERN.replace("'", "''")
    return f"regexp_matches(lower({column_sql}), '{escaped}')"
