"""Polite HTTP: one request per interval, retries with backoff, JSON sanity check."""

from __future__ import annotations

import time
from typing import Any

import httpx

from inspraak.config import get_settings

RETRY_STATUS = {429, 500, 502, 503, 504}
BACKOFF_SECONDS = (2, 5, 15, 30)


class FetchError(RuntimeError):
    pass


class Fetcher:
    """Sequential client. Every request waits until `interval` has passed since the last."""

    def __init__(
        self,
        interval: float | None = None,
        client: httpx.Client | None = None,
        sleep=time.sleep,
        clock=time.monotonic,
    ) -> None:
        settings = get_settings()
        self.interval = settings.request_interval if interval is None else interval
        self.client = client or httpx.Client(
            headers={"User-Agent": settings.user_agent},
            timeout=httpx.Timeout(120.0, connect=30.0),
            follow_redirects=True,
        )
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None
        self.requests = 0

    def _wait(self) -> None:
        if self._last is not None:
            remaining = self.interval - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()

    def get(self, url: str, params: dict[str, Any] | None = None) -> httpx.Response:
        last_error = "no attempt"
        for attempt in range(len(BACKOFF_SECONDS) + 1):
            self._wait()
            self.requests += 1
            try:
                response = self.client.get(url, params=params)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    return response
                last_error = f"HTTP {response.status_code}"
                if response.status_code not in RETRY_STATUS:
                    break
            if attempt < len(BACKOFF_SECONDS):
                self._sleep(BACKOFF_SECONDS[attempt])
        raise FetchError(f"{url} failed: {last_error}")

    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        """GET and decode JSON. The HYS backend answers 200 with an HTML shell on a
        wrong path or an unsupported parameter, so the content type is checked."""
        response = self.get(url, params=params)
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            raise FetchError(f"{url} returned {content_type or 'no content type'}, not JSON")
        return response.json()

    def close(self) -> None:
        self.client.close()
