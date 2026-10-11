"""Provider interface: one method, `complete_json`, that returns a validated pydantic
object. The base class owns what every provider shares: the token bucket, retries with
backoff, JSON validation with one corrective retry, the dash post-filter on every string
the model produced, and the call log (`llm_calls`).

Providers implement `_generate(prompt, schema, system, temperature, max_output_tokens)`
and return the raw JSON text plus token counts. `context` is ignored by real providers;
the fake one uses it to produce answers that fit the source text.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from inspraak.llm.bucket import QuotaExhausted, TokenBucket
from inspraak.textutil import plain_dashes

T = TypeVar("T", bound=BaseModel)

BACKOFF_SECONDS = (5, 15, 30, 60)


class LLMError(RuntimeError):
    pass


class RateLimited(LLMError):
    """The provider answered 429. Retried with backoff; raised when attempts run out."""


class InvalidOutput(LLMError):
    """The model's JSON did not validate against the schema, twice."""


@dataclass
class RawCompletion:
    text: str
    prompt_tokens: int = 0
    output_tokens: int = 0


@dataclass
class LLMResult[T: BaseModel]:
    data: T
    provider: str
    model: str
    purpose: str
    prompt_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    attempts: int = 1


@dataclass
class CallRecord:
    provider: str
    model: str
    purpose: str
    ok: bool
    cache_key: str | None = None
    run_id: str | None = None
    prompt_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    attempts: int = 1
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


Recorder = Callable[[CallRecord], None]


def filter_strings(value: Any) -> Any:
    """Apply `plain_dashes` to every string inside a parsed JSON value."""
    if isinstance(value, str):
        return plain_dashes(value)
    if isinstance(value, list):
        return [filter_strings(v) for v in value]
    if isinstance(value, dict):
        return {k: filter_strings(v) for k, v in value.items()}
    return value


class Provider:
    name = "base"

    def __init__(
        self,
        model: str,
        *,
        bucket: TokenBucket | None = None,
        recorder: Recorder | None = None,
        max_attempts: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model = model
        self.bucket = bucket
        self.recorder = recorder
        self.max_attempts = max_attempts
        self._sleep = sleep
        self.calls = 0
        self.failed_calls = 0
        self.prompt_tokens = 0
        self.output_tokens = 0
        self.run_id: str | None = None  # set by the graph so the call log carries it

    # ----------------------------------------------------------------- to implement
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
        raise NotImplementedError

    def _is_rate_limit(self, exc: Exception) -> bool:
        return False

    def _is_daily_quota(self, exc: Exception) -> bool:
        return False

    def _is_retryable(self, exc: Exception) -> bool:
        return False

    # ----------------------------------------------------------------- public
    def complete_json(
        self,
        prompt: str,
        schema: type[T],
        *,
        purpose: str,
        system: str | None = None,
        temperature: float = 0.0,
        max_output_tokens: int = 4096,
        context: dict[str, Any] | None = None,
        cache_key: str | None = None,
    ) -> LLMResult[T]:
        started = time.monotonic()
        attempts = 0
        prompt_tokens = output_tokens = 0
        corrective: str | None = None
        last_error: Exception | None = None
        while attempts < self.max_attempts:
            attempts += 1
            if self.bucket is not None:
                try:
                    # the logged duration is model time: the pacing wait is not counted
                    started += self.bucket.acquire() or 0.0
                except QuotaExhausted as exc:
                    self._record(purpose, False, cache_key, started, attempts, 0, 0, str(exc))
                    raise
            full_prompt = prompt if corrective is None else f"{prompt}\n\n{corrective}"
            try:
                raw = self._generate(
                    full_prompt,
                    schema,
                    system=system,
                    temperature=temperature,
                    max_output_tokens=max_output_tokens,
                    context=context,
                )
            except QuotaExhausted:
                raise
            except Exception as exc:  # provider errors
                last_error = exc
                if self._is_daily_quota(exc):
                    self._record(purpose, False, cache_key, started, attempts, 0, 0, str(exc))
                    raise QuotaExhausted(
                        f"provider reports the daily quota is spent: {exc}"
                    ) from exc
                if (
                    self._is_rate_limit(exc) or self._is_retryable(exc)
                ) and attempts < self.max_attempts:
                    self._sleep(BACKOFF_SECONDS[min(attempts - 1, len(BACKOFF_SECONDS) - 1)])
                    continue
                self._record(purpose, False, cache_key, started, attempts, 0, 0, repr(exc))
                self.failed_calls += 1
                if self._is_rate_limit(exc):
                    raise RateLimited(str(exc)) from exc
                raise LLMError(f"{type(exc).__name__}: {exc}") from exc
            prompt_tokens += raw.prompt_tokens
            output_tokens += raw.output_tokens
            try:
                parsed = schema.model_validate(filter_strings(_loads(raw.text)))
            except (ValidationError, ValueError) as exc:
                last_error = exc
                corrective = (
                    "Your previous answer was not valid for the required JSON schema: "
                    f"{str(exc)[:500]}. Answer again with valid JSON only."
                )
                if attempts < self.max_attempts:
                    continue
                self._record(
                    purpose,
                    False,
                    cache_key,
                    started,
                    attempts,
                    prompt_tokens,
                    output_tokens,
                    str(exc),
                )
                self.failed_calls += 1
                raise InvalidOutput(str(exc)) from exc
            duration_ms = int((time.monotonic() - started) * 1000)
            self.calls += 1
            self.prompt_tokens += prompt_tokens
            self.output_tokens += output_tokens
            self._record(
                purpose, True, cache_key, started, attempts, prompt_tokens, output_tokens, None
            )
            return LLMResult(
                data=parsed,
                provider=self.name,
                model=self.model,
                purpose=purpose,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                duration_ms=duration_ms,
                attempts=attempts,
            )
        self.failed_calls += 1
        raise LLMError(f"gave up after {attempts} attempts: {last_error}")

    def _record(
        self,
        purpose: str,
        ok: bool,
        cache_key: str | None,
        started: float,
        attempts: int,
        prompt_tokens: int,
        output_tokens: int,
        error: str | None,
    ) -> None:
        if self.recorder is None:
            return
        record = CallRecord(
            provider=self.name,
            model=self.model,
            purpose=purpose,
            ok=ok,
            cache_key=cache_key,
            run_id=self.run_id,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
            attempts=attempts,
            error=error[:500] if error else None,
        )
        try:
            self.recorder(record)
        except Exception:
            pass  # the log never breaks a run


def _loads(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def db_recorder(database_url: str | None = None) -> Recorder:
    """Writes every call to `llm_calls` on its own short connection, so the log does not
    depend on the caller's transaction."""
    import psycopg

    from inspraak.config import get_settings

    url = database_url or get_settings().database_url

    def _write(record: CallRecord) -> None:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO llm_calls (provider, model, purpose, cache_key, run_id, "
                "prompt_tokens, "
                "output_tokens, duration_ms, attempts, ok, error) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    record.provider,
                    record.model,
                    record.purpose,
                    record.cache_key,
                    record.run_id,
                    record.prompt_tokens,
                    record.output_tokens,
                    record.duration_ms,
                    record.attempts,
                    record.ok,
                    record.error,
                ),
            )

    return _write
