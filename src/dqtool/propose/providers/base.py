"""Provider interface: prompt in, validated JSON out. One thin seam for every LLM call."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class LLMRequest:
    system: str
    prompt: str
    schema: type[BaseModel]
    model: str
    max_tokens: int
    effort: str | None = None
    fallbacks: str | None = None


@dataclass
class LLMResponse:
    text: str
    model: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    request_id: str | None = None


class Provider(Protocol):
    name: str

    def complete(self, request: LLMRequest) -> LLMResponse:
        """Run one request. Raise on transport or API errors."""
        ...

    def count_tokens(self, request: LLMRequest) -> int:
        """Input tokens the request would use."""
        ...
