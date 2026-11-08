"""Shared HTTP core: per-account rate limit, retries on 429/5xx and network errors.

The core never puts a URL or an exception text into its errors: a Bitrix24 webhook carries its
secret in the path, and httpx exception texts may quote the URL.
"""

import asyncio
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from alnuzum_crm.errors import CrmUnavailable

USER_AGENT = "alnuzum-crm/0.1 (+https://github.com/alnuzum/solutions)"


class RateLimiter:
    """Sliding window: at most ``rate`` calls of ``acquire()`` per ``period`` seconds."""

    def __init__(self, rate: float, period: float = 1.0) -> None:
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate
        self.period = period
        self._times: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._times and now - self._times[0] >= self.period:
                    self._times.popleft()
                if len(self._times) < self.rate:
                    self._times.append(now)
                    return
                await asyncio.sleep(self.period - (now - self._times[0]))


_limiters: dict[tuple[str, float], RateLimiter] = {}


def shared_limiter(key: str, rate: float, period: float = 1.0) -> RateLimiter:
    """One limiter per CRM account in the process: every client of the account shares its limit."""
    lim = _limiters.get((key, period))
    if lim is None or lim.rate != rate:
        lim = _limiters[(key, period)] = RateLimiter(rate, period)
    return lim


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    backoff: float = 0.5
    max_delay: float = 30.0
    statuses: frozenset[int] = field(default_factory=lambda: frozenset({429, 500, 502, 503, 504}))

    def delay(self, attempt: int, retry_after: str | None) -> float:
        if retry_after and retry_after.strip().isdigit():
            return min(float(retry_after), self.max_delay)
        return float(min(self.backoff * 2 ** (attempt - 1), self.max_delay))


class HttpCore:
    def __init__(
        self,
        crm: str,
        *,
        limiter: RateLimiter,
        retry: RetryPolicy | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
        base_url: str = "",
    ) -> None:
        self.crm = crm
        self.limiter = limiter
        self.retry = retry or RetryPolicy()
        self.http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout,
            transport=transport,
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Any = None,
        json: Any = None,
        content: bytes | None = None,
        files: Any = None,
        retry_if: Callable[[httpx.Response], bool] | None = None,
    ) -> httpx.Response:
        """One request with pacing and retries; the last answer is returned whatever its status."""
        attempt = 0
        while True:
            await self.limiter.acquire()
            try:
                resp = await self.http.request(
                    method, url, headers=headers, params=params, json=json, content=content, files=files
                )
            except httpx.TransportError as exc:
                if attempt < self.retry.max_retries:
                    attempt += 1
                    await asyncio.sleep(self.retry.delay(attempt, None))
                    continue
                raise CrmUnavailable(f"unreachable ({type(exc).__name__})", crm=self.crm) from None
            retryable = resp.status_code in self.retry.statuses or (retry_if is not None and retry_if(resp))
            if retryable and attempt < self.retry.max_retries:
                attempt += 1
                await asyncio.sleep(self.retry.delay(attempt, resp.headers.get("Retry-After")))
                continue
            return resp
