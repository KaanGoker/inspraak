"""Optional Anthropic provider (`uv sync --extra anthropic`, ANTHROPIC_API_KEY).
Structured output through a single forced tool whose input schema is the pydantic
schema. Not exercised in this build: no key is configured for the project."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from inspraak.llm.base import Provider, RawCompletion


class AnthropicProvider(Provider):
    name = "anthropic"

    def __init__(self, model: str, api_key: str, **kwargs) -> None:
        super().__init__(model, **kwargs)
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is empty")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("install the optional extra: uv sync --extra anthropic") from exc
        self._client = anthropic.Anthropic(api_key=api_key)

    def _generate(
        self,
        prompt: str,
        schema: type[BaseModel],
        *,
        system: str | None,
        temperature: float,
        max_output_tokens: int,
        context: dict[str, Any] | None,
    ) -> RawCompletion:
        import json

        tool = {
            "name": "emit",
            "description": "Return the answer in the required structure.",
            "input_schema": schema.model_json_schema(),
        }
        kwargs: dict[str, Any] = {}
        if system:
            kwargs["system"] = system
        response = self._client.messages.create(
            model=self.model,
            max_tokens=max_output_tokens,
            temperature=temperature,
            tools=[tool],
            tool_choice={"type": "tool", "name": "emit"},
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        payload = next((b.input for b in response.content if b.type == "tool_use"), {})
        return RawCompletion(
            text=json.dumps(payload),
            prompt_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

    def _is_rate_limit(self, exc: Exception) -> bool:
        return getattr(exc, "status_code", None) == 429

    def _is_retryable(self, exc: Exception) -> bool:
        code = getattr(exc, "status_code", None)
        return isinstance(code, int) and code >= 500
