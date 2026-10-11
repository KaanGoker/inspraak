"""Global rate limit for model calls: a token bucket per minute plus a daily counter,
shared by every process through Redis. An in-memory bucket with the same behaviour
serves tests and the case where Redis is down.

`acquire()` blocks until a token is free. When the daily counter is spent it raises
`QuotaExhausted`: the caller stops and reports instead of looping (SPEC, limits).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Protocol


class QuotaExhausted(RuntimeError):
    """The self-imposed daily budget is spent. Stop, do not retry."""


class TokenBucket(Protocol):
    def acquire(self, timeout: float | None = None) -> float: ...

    def used_today(self) -> int: ...


def _day_key() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _seconds_to_midnight_utc() -> int:
    now = datetime.now(UTC)
    return 86400 - (now.hour * 3600 + now.minute * 60 + now.second) + 5


class MemoryTokenBucket:
    """Same contract as the Redis one, one process only."""

    def __init__(
        self,
        rate_per_minute: int,
        daily_limit: int,
        *,
        capacity: int | None = None,
        clock=time.monotonic,
        sleep=time.sleep,
    ) -> None:
        self.rate = rate_per_minute / 60.0
        self.capacity = float(capacity or max(1, rate_per_minute))
        self.daily_limit = daily_limit
        self._tokens = self.capacity
        self._last = clock()
        self._clock = clock
        self._sleep = sleep
        self._day = _day_key()
        self._used = 0

    def _refill(self) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    def acquire(self, timeout: float | None = None) -> float:
        if _day_key() != self._day:
            self._day, self._used = _day_key(), 0
        if self._used >= self.daily_limit:
            raise QuotaExhausted(f"daily budget of {self.daily_limit} calls is spent")
        waited = 0.0
        while True:
            self._refill()
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                self._used += 1
                return waited
            wait = (1.0 - self._tokens) / self.rate
            if timeout is not None and waited + wait > timeout:
                raise TimeoutError("rate limit wait exceeded the timeout")
            self._sleep(wait)
            waited += wait

    def used_today(self) -> int:
        return self._used if _day_key() == self._day else 0


# Refill, then take one token or return the wait in milliseconds. Atomic in Redis.
_LUA = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])        -- tokens per second
local capacity = tonumber(ARGV[2])
local now = tonumber(ARGV[3])         -- seconds, float
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then tokens = capacity; ts = now end
tokens = math.min(capacity, tokens + (now - ts) * rate)
local wait_ms = 0
if tokens >= 1 then
  tokens = tokens - 1
else
  wait_ms = math.ceil((1 - tokens) / rate * 1000)
end
redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, 3600)
return wait_ms
"""


class RedisTokenBucket:
    def __init__(
        self,
        client,
        *,
        rate_per_minute: int,
        daily_limit: int,
        name: str = "inspraak:llm",
        capacity: int | None = None,
        sleep=time.sleep,
    ) -> None:
        self.client = client
        self.rate = rate_per_minute / 60.0
        self.capacity = capacity or max(1, rate_per_minute)
        self.daily_limit = daily_limit
        self.name = name
        self._sleep = sleep
        self._script = client.register_script(_LUA)

    def _daily_key(self) -> str:
        return f"{self.name}:day:{_day_key()}"

    def acquire(self, timeout: float | None = None) -> float:
        used = int(self.client.get(self._daily_key()) or 0)
        if used >= self.daily_limit:
            raise QuotaExhausted(f"daily budget of {self.daily_limit} calls is spent")
        waited = 0.0
        while True:
            wait_ms = int(
                self._script(
                    keys=[f"{self.name}:bucket"], args=[self.rate, self.capacity, time.time()]
                )
            )
            if wait_ms == 0:
                key = self._daily_key()
                count = self.client.incr(key)
                if count == 1:
                    self.client.expire(key, _seconds_to_midnight_utc())
                return waited
            wait = wait_ms / 1000.0
            if timeout is not None and waited + wait > timeout:
                raise TimeoutError("rate limit wait exceeded the timeout")
            self._sleep(wait)
            waited += wait

    def used_today(self) -> int:
        return int(self.client.get(self._daily_key()) or 0)


def make_bucket(redis_url: str, rate_per_minute: int, daily_limit: int) -> TokenBucket:
    """Redis when it answers, otherwise the in-process bucket (logged by the caller)."""
    try:
        import redis

        client = redis.Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=2)
        client.ping()
        return RedisTokenBucket(client, rate_per_minute=rate_per_minute, daily_limit=daily_limit)
    except Exception:
        return MemoryTokenBucket(rate_per_minute, daily_limit)
