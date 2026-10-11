"""LLM providers: gemini, anthropic (optional), fake. `get_provider()` builds the one
named by LLM_PROVIDER with the shared Redis token bucket and the call log."""

from __future__ import annotations

from functools import lru_cache

from inspraak.config import get_settings
from inspraak.llm.base import (
    InvalidOutput,
    LLMError,
    LLMResult,
    Provider,
    RateLimited,
    db_recorder,
)
from inspraak.llm.bucket import MemoryTokenBucket, QuotaExhausted, make_bucket

__all__ = [
    "InvalidOutput",
    "LLMError",
    "LLMResult",
    "Provider",
    "QuotaExhausted",
    "RateLimited",
    "get_provider",
    "make_provider",
]


def make_provider(
    name: str | None = None, *, with_bucket: bool = True, with_recorder: bool = True
) -> Provider:
    settings = get_settings()
    name = name or settings.llm_provider
    recorder = db_recorder() if with_recorder else None
    if name == "fake":
        from inspraak.llm.fake import FakeProvider

        bucket = MemoryTokenBucket(6000, 10**9) if with_bucket else None
        return FakeProvider(
            bucket=bucket, recorder=recorder, max_attempts=settings.llm_max_attempts
        )
    bucket = (
        make_bucket(settings.redis_url, settings.llm_rpm, settings.llm_rpd) if with_bucket else None
    )
    if name == "gemini":
        from inspraak.llm.gemini import GeminiProvider

        return GeminiProvider(
            settings.gemini_model,
            settings.gemini_api_key,
            bucket=bucket,
            recorder=recorder,
            max_attempts=settings.llm_max_attempts,
        )
    if name == "anthropic":
        from inspraak.llm.anthropic_provider import AnthropicProvider

        return AnthropicProvider(
            settings.anthropic_model,
            settings.anthropic_api_key,
            bucket=bucket,
            recorder=recorder,
            max_attempts=settings.llm_max_attempts,
        )
    if name == "claude_cli":
        import os

        from inspraak.llm.claude_cli import ClaudeCliProvider

        # no shared bucket: the Gemini budget does not apply to the CLI
        return ClaudeCliProvider(
            os.environ.get("CLAUDE_CLI_MODEL", "haiku"),
            os.environ.get("CLAUDE_CLI_EFFORT", "low"),
            recorder=recorder,
            max_attempts=settings.llm_max_attempts,
        )
    if name == "agy":
        import os

        from inspraak.llm.agy import AgyProvider

        chain = os.environ.get("AGY_MODELS", "gemini-3.8-flash-low,claude-sonnet-4-6")
        return AgyProvider(
            [m.strip() for m in chain.split(",") if m.strip()],
            recorder=recorder,
            max_attempts=settings.llm_max_attempts,
        )
    raise ValueError(
        f"unknown LLM_PROVIDER {name!r}; use gemini, anthropic, claude_cli, agy or fake"
    )


@lru_cache(maxsize=1)
def get_provider() -> Provider:
    return make_provider()
