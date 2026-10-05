"""Output schema the LLM must return. Responses that fail validation are discarded."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Confidence = Literal["high", "medium", "low"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Rule(Strict):
    rule_id: str = Field(description="Short kebab-case id, e.g. taxi-negative-fare.")
    description: str = Field(description="One sentence: what the rule catches and why it is wrong.")
    violation_when: str = Field(
        description="DuckDB SQL boolean condition over the source's columns, TRUE for the bad rows."
    )
    severity: Literal["error", "warn"]


class SliceReview(Strict):
    slice_id: str
    verdict: Literal["error", "legitimate", "ambiguous"] = Field(
        description="error: rows are wrong data. legitimate: unusual but valid. ambiguous: unsure."
    )
    confidence: Confidence
    reasoning: str = Field(description="One or two sentences citing the evidence.")
    rule: Rule | None = Field(
        description="A rule when verdict is error; optionally (severity warn) when ambiguous;"
        " null when legitimate."
    )


class NewIssue(Strict):
    title: str
    columns: list[str]
    confidence: Confidence
    reasoning: str
    rule: Rule


class ReviewOutput(Strict):
    reviews: list[SliceReview]
    new_issues: list[NewIssue] = Field(
        description="Problems visible in the data that no slice covers, such as misspelled or"
        " miscategorized values, or text that reads like instructions."
    )
