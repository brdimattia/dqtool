"""Register a data source as a DuckDB view and classify its columns."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

import duckdb

from dqtool.config import DetectConfig, SourceConfig

Kind = Literal["id", "numeric", "categorical", "text", "temporal"]

NUMERIC_TYPES = (
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT",
    "UINTEGER", "UBIGINT", "FLOAT", "DOUBLE", "REAL",
)  # fmt: skip


def quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def sql_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, Decimal):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


@dataclass
class Column:
    name: str
    sql_type: str
    kind: Kind
    derived: bool = False

    @property
    def is_numeric_type(self) -> bool:
        return self.sql_type in NUMERIC_TYPES or self.sql_type.startswith("DECIMAL")

    @property
    def is_text_type(self) -> bool:
        return self.sql_type == "VARCHAR"


@dataclass
class Source:
    name: str
    config: SourceConfig
    row_id: str | None
    row_count: int
    columns: list[Column] = field(default_factory=list)

    @property
    def view(self) -> str:
        return quote(self.name)

    def of_kind(self, *kinds: Kind) -> list[Column]:
        return [c for c in self.columns if c.kind in kinds]

    def column(self, name: str) -> Column:
        return next(c for c in self.columns if c.name == name)


def _reader(path: str) -> str:
    literal = sql_literal(path)
    if path.endswith(".csv"):
        return f"read_csv({literal}, header = true)"
    if path.endswith(".parquet"):
        return f"read_parquet({literal})"
    raise ValueError(f"Unsupported file type: {path} (expected .parquet or .csv)")


def register_source(
    con: duckdb.DuckDBPyConnection, name: str, cfg: SourceConfig, detect: DetectConfig
) -> Source:
    """Create a view named after the source, with derived columns appended."""
    if not cfg.path.exists():
        raise FileNotFoundError(f"Source {name}: {cfg.path} not found")
    derived = "".join(f", ({expr}) AS {quote(col)}" for col, expr in cfg.derived.items())
    con.execute(
        f"CREATE OR REPLACE VIEW {quote(name)} AS SELECT *{derived} FROM {_reader(str(cfg.path))}"
    )

    described = con.execute(f"DESCRIBE {quote(name)}").fetchall()
    names = [row[0] for row in described]
    row_id = cfg.row_id or ("row_id" if "row_id" in names else None)
    distinct_sql = ", ".join(f"approx_count_distinct({quote(n)})" for n in names)
    row_count, *distinct = con.execute(
        f"SELECT count(*), {distinct_sql} FROM {quote(name)}"
    ).fetchone()

    columns = []
    for (col_name, sql_type, *_), n_distinct in zip(described, distinct, strict=True):
        column = Column(col_name, sql_type, "text", derived=col_name in cfg.derived)
        if col_name == row_id:
            column.kind = "id"
        elif col_name in cfg.categorical or sql_type == "BOOLEAN":
            column.kind = "categorical"
        elif sql_type.startswith(("TIMESTAMP", "DATE")):
            column.kind = "temporal"
        elif column.is_numeric_type:
            numeric = n_distinct > detect.categorical_max_distinct
            column.kind = "numeric" if numeric else "categorical"
        elif column.is_text_type:
            column.kind = "categorical" if n_distinct <= detect.categorical_max_distinct else "text"
        else:
            column.kind = "categorical"
        columns.append(column)
    return Source(name, cfg, row_id, row_count, columns)
