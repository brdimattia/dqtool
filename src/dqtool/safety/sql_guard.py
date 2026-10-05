"""SQL guard: accept only a read-only boolean predicate over known columns.

Every LLM-proposed rule passes through here before it is run anywhere (spec:
Security, layer 4). The predicate is parsed into a syntax tree with sqlglot and
every node is checked against an allow-list, so anything not listed (subqueries,
table references, DDL/DML, file or network functions, parameters) is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

MAX_PREDICATE_CHARS = 2000
MAX_NODES = 400


def _classes(*names: str) -> tuple[type, ...]:
    """Expression classes by name, skipping any this sqlglot version lacks."""
    return tuple(getattr(exp, n) for n in names if hasattr(exp, n))


PREDICATES = _classes(
    "EQ", "NEQ", "GT", "GTE", "LT", "LTE", "NullSafeEQ", "NullSafeNEQ",
    "And", "Or", "Xor", "Not", "In", "Between", "Is", "Like", "ILike",
    "RegexpLike", "RegexpILike", "Boolean",
)  # fmt: skip

VALUES = _classes(
    "Column", "Identifier", "Literal", "Null", "Paren", "Neg", "Tuple", "Array",
    "Add", "Sub", "Mul", "Div", "IntDiv", "Mod", "Pow", "DPipe",
    "Cast", "TryCast", "DataType", "DataTypeParam", "Interval", "Var",
    "Case", "If", "Coalesce", "Nullif", "Greatest", "Least",
    "Abs", "Round", "Floor", "Ceil", "Ln", "Log", "Exp", "Sqrt", "Sign",
    "Length", "Lower", "Upper", "Trim", "Substring", "Concat", "StrPosition",
    "DateDiff", "DateTrunc", "TimestampTrunc", "TimestampDiff", "Extract",
    "Year", "Month", "Day", "DayOfWeek", "DayOfMonth", "Quarter", "Week", "Hour",
    "TimeToStr", "StrToTime", "TsOrDsToDate", "Date", "CurrentDate",
    "TimeToUnix", "UnixToTime", "IsNan", "IsInf",
)  # fmt: skip

ALLOWED = PREDICATES + VALUES

# Functions sqlglot does not model get parsed as Anonymous; allow only these.
ALLOWED_ANONYMOUS = {
    "regexp_matches", "date_diff", "datediff", "date_part", "epoch", "isnan",
    "isfinite", "isinf", "strftime", "length", "strlen", "to_years", "to_months",
    "to_days", "to_hours", "to_minutes", "to_seconds",
}  # fmt: skip


@dataclass
class GuardResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    normalized: str | None = None


def _is_predicate(node: exp.Expression) -> bool:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, PREDICATES):
        return True
    return isinstance(node, exp.Anonymous) and node.name.lower() == "regexp_matches"


def check_predicate(predicate: str, columns: set[str] | list[str]) -> GuardResult:
    """Validate predicate against an allow-list and the source's known columns."""
    known = {c.lower() for c in columns}
    if not predicate or not predicate.strip():
        return GuardResult(False, ["empty predicate"])
    if len(predicate) > MAX_PREDICATE_CHARS:
        return GuardResult(False, [f"predicate longer than {MAX_PREDICATE_CHARS} characters"])
    if "--" in predicate or "/*" in predicate:
        return GuardResult(False, ["comments are not allowed"])

    try:
        statements = sqlglot.parse(f"SELECT 1 WHERE {predicate}", read="duckdb")
    except sqlglot.errors.ParseError as e:
        return GuardResult(False, [f"does not parse: {str(e).splitlines()[0]}"])
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        return GuardResult(False, ["must be a single predicate, not multiple statements"])
    select = statements[0]
    extra = {k for k, v in select.args.items() if v and k not in ("expressions", "where")}
    if extra or len(select.expressions) != 1:
        return GuardResult(False, [f"unexpected SQL clauses: {', '.join(sorted(extra))}"])
    where = select.args.get("where")
    if where is None:
        return GuardResult(False, ["no predicate found"])
    root = where.this

    errors: list[str] = []
    used: set[str] = set()
    nodes = list(root.walk())
    if len(nodes) > MAX_NODES:
        errors.append(f"predicate too complex ({len(nodes)} nodes)")
    for node in nodes:
        if isinstance(node, exp.Column):
            if node.table:
                errors.append(f"column {node.sql()} references another table")
            name = node.name.lower()
            if name not in known:
                errors.append(f"unknown column: {node.name}")
            used.add(node.name)
        elif isinstance(node, exp.Anonymous):
            if node.name.lower() not in ALLOWED_ANONYMOUS:
                errors.append(f"function not allowed: {node.name}")
        elif not isinstance(node, ALLOWED):
            errors.append(f"not allowed: {type(node).__name__} ({node.sql(dialect='duckdb')[:60]})")
    if not _is_predicate(root):
        errors.append("must be a boolean condition (comparison, AND/OR, IN, IS, LIKE, ...)")

    # Report each distinct problem once, in order.
    errors = list(dict.fromkeys(errors))
    return GuardResult(
        ok=not errors,
        errors=errors,
        columns=sorted(used),
        normalized=root.sql(dialect="duckdb") if not errors else None,
    )
