"""Gemini through google-genai, JSON mode with the pydantic schema as response schema.

Checked on 3 October 2026 with one real call per model: `gemini-3.5-flash-lite` and
`gemini-2.5-flash-lite` both answer structured output on the free tier (13 prompt
tokens, 19 output tokens for the probe). Google no longer publishes the free-tier
request limits in the docs; the self-imposed budget lives in the token bucket.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from inspraak.llm.base import Provider, RawCompletion


class GeminiProvider(Provider):
    name = "gemini"

    def __init__(self, model: str, api_key: str, **kwargs) -> None:
        super().__init__(model, **kwargs)
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is empty; set it in .env or use LLM_PROVIDER=fake")
        from google import genai

        self._client = genai.Client(api_key=api_key)

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
        from google.genai import types

        config = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        response = self._client.models.generate_content(
            model=self.model, contents=prompt, config=config
        )
        usage = response.usage_metadata
        text = response.text or ""
        return RawCompletion(
            text=text,
            prompt_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(usage, "candidates_token_count", 0) or 0)
            + int(getattr(usage, "thoughts_token_count", 0) or 0),
        )

    @staticmethod
    def _code(exc: Exception) -> int | None:
        code = getattr(exc, "code", None)
        if isinstance(code, int):
            return code
        status = getattr(exc, "status_code", None)
        return status if isinstance(status, int) else None

    def _is_rate_limit(self, exc: Exception) -> bool:
        return self._code(exc) == 429

    def _is_daily_quota(self, exc: Exception) -> bool:
        if self._code(exc) != 429:
            return False
        text = str(exc).lower()
        return "per day" in text or "perday" in text or "daily" in text

    def _is_retryable(self, exc: Exception) -> bool:
        code = self._code(exc)
        return code is not None and code >= 500


def probe(provider: GeminiProvider) -> dict[str, Any]:
    """One tiny structured call, for `inspraak llm probe`."""

    class Probe(BaseModel):
        city: str
        country: str

    result = provider.complete_json(
        "Which city hosts the European Commission headquarters? Answer as JSON.",
        Probe,
        purpose="probe",
    )
    return {
        "model": provider.model,
        "answer": result.data.model_dump(),
        "prompt_tokens": result.prompt_tokens,
        "output_tokens": result.output_tokens,
        "duration_ms": result.duration_ms,
    }
