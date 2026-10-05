"""Claude implementation of the provider interface (Anthropic Python SDK)."""

from __future__ import annotations

from typing import Any

import anthropic

from dqtool.propose.providers.base import LLMRequest, LLMResponse

# Refusal fallback, "default" form: the API picks the substitute model by refusal category.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Models that reject the effort parameter.
NO_EFFORT = ("claude-haiku-4-5",)


class ClaudeProvider:
    name = "claude"

    def __init__(self, client: anthropic.Anthropic | None = None):
        self.client = client or anthropic.Anthropic()

    def _params(self, request: LLMRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens,
            # Stable system prompt first so repeated scans can reuse the cached prefix.
            "system": [
                {"type": "text", "text": request.system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": request.prompt}],
        }
        output_config: dict[str, Any] = {
            "format": {
                "type": "json_schema",
                "schema": anthropic.transform_schema(request.schema),
            }
        }
        if request.effort and request.model not in NO_EFFORT:
            output_config["effort"] = request.effort
        params["output_config"] = output_config
        return params

    def complete(self, request: LLMRequest) -> LLMResponse:
        params = self._params(request)
        if request.fallbacks:
            stream = self.client.beta.messages.stream(
                **params, betas=[FALLBACK_BETA], fallbacks=request.fallbacks
            )
        else:
            stream = self.client.messages.stream(**params)
        # Streaming avoids HTTP timeouts on large max_tokens.
        with stream as s:
            message = s.get_final_message()
        text = "".join(b.text for b in message.content if b.type == "text")
        usage = message.usage
        return LLMResponse(
            text=text,
            model=message.model,
            stop_reason=message.stop_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            cache_write_tokens=usage.cache_creation_input_tokens or 0,
            request_id=getattr(message, "_request_id", None),
        )

    def count_tokens(self, request: LLMRequest) -> int:
        params = self._params(request)
        params.pop("max_tokens")
        return self.client.messages.count_tokens(**params).input_tokens
