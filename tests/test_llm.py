"""Provider interface, fake provider, token buckets, post-filter, retries."""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from inspraak.llm.base import InvalidOutput, LLMError, Provider, RawCompletion, filter_strings
from inspraak.llm.bucket import MemoryTokenBucket, QuotaExhausted, RedisTokenBucket
from inspraak.llm.fake import FakeProvider
from inspraak.llm.schemas import CriticReport, DraftResponse, FeedbackAnalysis, PublicationSummary


class Answer(BaseModel):
    text: str
    n: int = 0


def test_fake_provider_fills_any_schema_and_counts_calls():
    provider = FakeProvider()
    result = provider.complete_json("hello", Answer, purpose="unknown")
    assert isinstance(result.data, Answer)
    assert provider.calls == 1 and result.provider == "fake"
    for schema, purpose in (
        (PublicationSummary, "reader_summary"),
        (FeedbackAnalysis, "feedback_analysis"),
        (DraftResponse, "draft"),
        (CriticReport, "critic"),
    ):
        out = provider.complete_json("p", schema, purpose=purpose, context={"source_text": "a. b."})
        assert isinstance(out.data, schema)
    assert provider.calls == 5


def test_strings_are_post_filtered_for_long_dashes():
    em, en, minus = chr(0x2014), chr(0x2013), chr(0x2212)
    provider = FakeProvider(
        handlers={"x": lambda p, s, c: {"text": f"a {em} b {en} c {minus} d", "n": 1}}
    )
    result = provider.complete_json("p", Answer, purpose="x")
    assert result.data.text == "a - b - c - d"
    assert filter_strings({"k": [f"x{em}y", 3]}) == {"k": ["x-y", 3]}


def test_invalid_output_gets_one_corrective_retry_then_fails():
    attempts = []

    def flaky(prompt, schema, ctx):
        attempts.append(prompt)
        if len(attempts) == 1:
            return {"text": 123456, "n": "not a number"}  # n invalid
        return {"text": "fine", "n": 2}

    provider = FakeProvider(handlers={"x": flaky}, max_attempts=3)
    result = provider.complete_json("p", Answer, purpose="x")
    assert result.data.n == 2 and result.attempts == 2
    assert "not valid" in attempts[1]

    always_bad = FakeProvider(handlers={"x": lambda p, s, c: {"n": "bad"}}, max_attempts=2)
    with pytest.raises(InvalidOutput):
        always_bad.complete_json("p", Answer, purpose="x")
    assert always_bad.failed_calls == 1


class Boom(Provider):
    name = "boom"

    def __init__(self, errors, **kw):
        super().__init__("boom-1", **kw)
        self.errors = list(errors)

    def _generate(self, prompt, schema, **kw):
        if self.errors:
            raise self.errors.pop(0)
        return RawCompletion('{"text": "ok", "n": 1}', 10, 5)

    def _is_rate_limit(self, exc):
        return isinstance(exc, ConnectionError)

    def _is_daily_quota(self, exc):
        return "per day" in str(exc)


def test_rate_limit_backoff_then_success_and_daily_quota_stops():
    sleeps = []
    provider = Boom([ConnectionError("429 slow down")], sleep=sleeps.append, max_attempts=3)
    result = provider.complete_json("p", Answer, purpose="x")
    assert result.data.text == "ok" and result.attempts == 2 and sleeps == [5]
    assert result.prompt_tokens == 10 and provider.prompt_tokens == 10

    provider = Boom([ConnectionError("429 quota exceeded per day")], sleep=sleeps.append)
    with pytest.raises(QuotaExhausted):
        provider.complete_json("p", Answer, purpose="x")

    provider = Boom([ValueError("bad request")], sleep=sleeps.append)
    with pytest.raises(LLMError):
        provider.complete_json("p", Answer, purpose="x")


def test_memory_bucket_waits_and_enforces_daily_budget():
    clock = [0.0]
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        clock[0] += s

    bucket = MemoryTokenBucket(60, daily_limit=3, capacity=1, clock=lambda: clock[0], sleep=sleep)
    assert bucket.acquire() == 0.0
    waited = bucket.acquire()
    assert waited == pytest.approx(1.0) and sleeps == [pytest.approx(1.0)]
    bucket.acquire()
    assert bucket.used_today() == 3
    with pytest.raises(QuotaExhausted):
        bucket.acquire()


def test_provider_records_calls_and_stops_on_quota():
    records = []
    bucket = MemoryTokenBucket(6000, daily_limit=1)
    provider = FakeProvider(bucket=bucket, recorder=records.append)
    provider.complete_json("p", Answer, purpose="x")
    with pytest.raises(QuotaExhausted):
        provider.complete_json("p", Answer, purpose="x")
    assert [r.ok for r in records] == [True, False]
    assert records[0].purpose == "x" and records[1].error.startswith("daily budget")


def _redis_client():
    import redis

    from inspraak.config import get_settings

    client = redis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=1)
    try:
        client.ping()
    except Exception:
        pytest.skip("Redis not reachable; start it with `make up`")
    return client


def test_redis_bucket_is_shared_and_counts_the_day():
    client = _redis_client()
    name = "inspraak:test:bucket"
    for key in client.scan_iter(f"{name}*"):
        client.delete(key)
    sleeps = []
    a = RedisTokenBucket(
        client, rate_per_minute=600, daily_limit=5, name=name, capacity=2, sleep=sleeps.append
    )
    b = RedisTokenBucket(
        client, rate_per_minute=600, daily_limit=5, name=name, capacity=2, sleep=sleeps.append
    )
    a.acquire()
    b.acquire()
    assert a.used_today() == 2 == b.used_today()
    # the shared bucket is empty now: the third caller has to wait about 100 ms
    a.acquire()
    assert sleeps and 0 < sleeps[0] <= 0.2
    b.acquire()
    a.acquire()
    with pytest.raises(QuotaExhausted):
        b.acquire()
    for key in client.scan_iter(f"{name}*"):
        client.delete(key)
