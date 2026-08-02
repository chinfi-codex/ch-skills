#!/usr/bin/env python3
"""Unified HTTP fetch layer for ch-news-reporter: retry, classification, rate limiting.

Every collector in collect_news.py / jin10_mcp.py fetches through here so that
retry policy, failure classification and per-host pacing live in exactly one
place. Policy (mirrors shared/yahoo_http, minus Yahoo's dual-host failover):

- up to 4 attempts per request;
- exponential backoff (1s / 2s / 4s, capped) with jitter;
- 429 has its own budget of at most 2 retries and honours Retry-After first;
- other 4xx (400/401/403/404/...) are never retried — they will not succeed on
  a repeat and retrying only burns rate-limit budget;
- 5xx and 408 retry within the overall attempt budget;
- DNS/connect/timeout failures get an independent budget of 2 retries.

Failures are classified into categories (see FetchResult.error / FetchError.category):
timeout / rate_limited / forbidden / unauthorized / not_found / server_error /
unknown. Callers that tolerate per-item failure (e.g. per-repo GitHub metadata)
use fetch_safe(), which never raises.

The transport is injectable: every fetch function takes an `opener` callable
(and a `sleep` callable), so tests never touch the real network. TokenBucket
takes `clock`/`sleep` callables for the same reason.
"""

from __future__ import annotations

import json
import random
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

# Failure categories, also used as the value of FetchResult.error.
TIMEOUT = "timeout"
RATE_LIMITED = "rate_limited"
FORBIDDEN = "forbidden"
UNAUTHORIZED = "unauthorized"
NOT_FOUND = "not_found"
SERVER_ERROR = "server_error"
UNKNOWN = "unknown"

DEFAULT_MAX_ATTEMPTS = 4
RATE_LIMIT_MAX_RETRIES = 2
NETWORK_MAX_RETRIES = 2
BACKOFF_BASE = 1.0  # seconds; grows 1 / 2 / 4 with jitter on top
MAX_BACKOFF = 20.0
MAX_RETRY_AFTER = 60.0

# Statuses retried within the overall attempt budget. 429 is retryable too but
# has its own smaller budget and Retry-After handling; every other 4xx is final.
RETRYABLE_STATUS = frozenset({408, 429, 500, 502, 503, 504})

# Same UA the collectors already sent via get_session(); several endpoints
# (GitHub web, some RSS feeds) reject the urllib default.
DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146 Safari/537.36"
    )
}

__all__ = [
    "FetchError",
    "FetchResult",
    "RawResponse",
    "TokenBucket",
    "TransportError",
    "TransportTimeout",
    "configure_limiter",
    "fetch_json",
    "fetch_response",
    "fetch_safe",
    "fetch_text",
    "get_header",
    "get_limiter",
    "set_limiter",
]


class TransportError(Exception):
    """Opener-level DNS/connect failure (mapped to the ``unknown`` category)."""


class TransportTimeout(TransportError):
    """Opener-level timeout (mapped to the ``timeout`` category)."""


class FetchError(RuntimeError):
    """Raised by the fetch_* functions when every attempt failed."""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        url: str,
        status: Optional[int] = None,
        attempts: int = 1,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.url = url
        self.status = status
        self.attempts = attempts

    def __str__(self) -> str:
        return f"[{self.category}] {super().__str__()}"


@dataclass
class RawResponse:
    """What an opener returns: status code, response headers, decoded body."""

    status: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: str = ""


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: Optional[int] = None
    headers: Dict[str, str] = field(default_factory=dict)
    text: Optional[str] = None
    error: Optional[str] = None  # one of the category constants when ok=False
    error_message: Optional[str] = None
    attempts: int = 0

    def json(self) -> Any:
        return json.loads(self.text or "")


Opener = Callable[..., RawResponse]


def get_header(headers: Mapping[str, str], name: str) -> Optional[str]:
    """Case-insensitive header lookup (urllib preserves the server's casing)."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _classify_status(status: int) -> str:
    if status == 401:
        return UNAUTHORIZED
    if status == 403:
        return FORBIDDEN
    if status == 404:
        return NOT_FOUND
    if status == 429:
        return RATE_LIMITED
    if status >= 500:
        return SERVER_ERROR
    if status == 408:
        return TIMEOUT
    return UNKNOWN


def _urllib_opener(
    url: str,
    *,
    method: str,
    headers: Mapping[str, str],
    body: Optional[bytes],
    timeout: float,
) -> RawResponse:
    """Default transport: stdlib urllib, no third-party dependency."""
    request = urllib.request.Request(
        url, data=body, headers=dict(headers), method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
            return RawResponse(
                status=response.status,
                headers=dict(response.headers.items()),
                body=text,
            )
    except urllib.error.HTTPError as exc:  # non-2xx still carries a response
        text = exc.read().decode("utf-8", "replace") if exc.fp is not None else ""
        return RawResponse(
            status=exc.code,
            headers=dict(exc.headers.items()) if exc.headers else {},
            body=text,
        )
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)) or "timed out" in str(
            reason
        ).lower():
            raise TransportTimeout(str(reason) or "timed out") from exc
        raise TransportError(str(reason)) from exc
    except (socket.timeout, TimeoutError) as exc:
        raise TransportTimeout(str(exc) or "timed out") from exc
    except OSError as exc:
        raise TransportError(str(exc)) from exc


def _backoff(attempt: int) -> float:
    delay = min(BACKOFF_BASE * (2 ** (attempt - 1)), MAX_BACKOFF)
    # jitter: concurrent workers that share a 429/5xx must not resynchronise
    return delay + random.uniform(0, 0.75)


def _retry_after_seconds(headers: Mapping[str, str]) -> Optional[float]:
    value = get_header(headers, "Retry-After")
    if not value:
        return None
    try:
        return max(0.0, min(float(value), MAX_RETRY_AFTER))
    except ValueError:
        return None  # HTTP-date form: fall back to exponential backoff


def _fetch(
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 20.0,
    family: Optional[str] = None,
    method: Optional[str] = None,
    body: Optional[bytes] = None,
    opener: Optional[Opener] = None,
    sleep: Optional[Callable[[float], None]] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> FetchResult:
    opener = opener or _urllib_opener
    sleep = time.sleep if sleep is None else sleep
    limiter = get_limiter(family) if family else None
    method = method or ("POST" if body is not None else "GET")
    merged_headers = {**DEFAULT_HEADERS, **dict(headers or {})}

    rate_limit_retries = 0
    network_retries = 0
    result = FetchResult(url=url, ok=False)

    for attempt in range(1, max_attempts + 1):
        if limiter is not None:
            limiter.acquire()
        raw: Optional[RawResponse] = None
        category: str
        message: str
        try:
            raw = opener(
                url, method=method, headers=merged_headers, body=body, timeout=timeout
            )
        except TransportTimeout as exc:
            category, message = TIMEOUT, f"request timed out after {timeout}s: {exc}"
        except TransportError as exc:
            category, message = UNKNOWN, f"connection failed: {exc}"
        except Exception as exc:  # opener bug or malformed URL — do not retry
            result.error, result.error_message, result.attempts = (
                UNKNOWN,
                f"{type(exc).__name__}: {exc}",
                attempt,
            )
            return result
        else:
            result.status, result.headers, result.text = (
                raw.status,
                raw.headers,
                raw.body,
            )
            if 200 <= raw.status < 300:
                result.ok, result.attempts = True, attempt
                return result
            category = _classify_status(raw.status)
            message = f"HTTP {raw.status}: {(raw.body or '')[:200].strip()}"
        result.error, result.error_message, result.attempts = (
            category,
            message,
            attempt,
        )

        if attempt >= max_attempts:
            break
        if category == RATE_LIMITED:
            rate_limit_retries += 1
            if rate_limit_retries > RATE_LIMIT_MAX_RETRIES:
                break
            retry_after = _retry_after_seconds(raw.headers if raw else {})
            delay = retry_after if retry_after is not None else _backoff(attempt)
        elif category in (TIMEOUT, UNKNOWN) and raw is None:
            network_retries += 1
            if network_retries > NETWORK_MAX_RETRIES:
                break
            delay = _backoff(attempt)
        elif raw is not None and raw.status in RETRYABLE_STATUS:
            delay = _backoff(attempt)
        else:
            break  # other 4xx: retrying cannot succeed
        sleep(delay)

    return result


def fetch_response(
    url: str,
    *,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 20.0,
    family: Optional[str] = None,
    method: Optional[str] = None,
    body: Optional[bytes] = None,
    opener: Optional[Opener] = None,
    sleep: Optional[Callable[[float], None]] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> FetchResult:
    """Fetch and return the full FetchResult; raise FetchError on failure."""
    result = _fetch(
        url,
        headers=headers,
        timeout=timeout,
        family=family,
        method=method,
        body=body,
        opener=opener,
        sleep=sleep,
        max_attempts=max_attempts,
    )
    if not result.ok:
        raise FetchError(
            result.error or UNKNOWN,
            result.error_message or "fetch failed",
            url=url,
            status=result.status,
            attempts=result.attempts,
        )
    return result


def fetch_text(url: str, **kwargs: Any) -> str:
    """Fetch and return the decoded body; raise FetchError on failure."""
    return fetch_response(url, **kwargs).text or ""


def fetch_json(url: str, **kwargs: Any) -> Any:
    """Fetch and parse a JSON body; raise FetchError on transport failure."""
    return json.loads(fetch_text(url, **kwargs))


def fetch_safe(url: str, **kwargs: Any) -> FetchResult:
    """Never-raise variant: failure comes back as FetchResult(ok=False, error=...)."""
    try:
        return _fetch(url, **kwargs)
    except Exception as exc:  # belt and braces: _fetch already never raises
        return FetchResult(
            url=url,
            ok=False,
            error=UNKNOWN,
            error_message=f"{type(exc).__name__}: {exc}",
        )


class TokenBucket:
    """Thread-safe token bucket. `clock`/`sleep` are injectable for tests."""

    def __init__(
        self,
        rate_per_sec: float,
        burst: Optional[float] = None,
        *,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], None]] = None,
    ) -> None:
        if rate_per_sec <= 0:
            raise ValueError("rate_per_sec must be positive")
        self.rate = float(rate_per_sec)
        self.capacity = float(burst) if burst is not None else max(1.0, self.rate)
        if self.capacity <= 0:
            raise ValueError("burst must be positive")
        self._tokens = self.capacity
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._updated = self._clock()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        while True:
            with self._lock:
                now = self._clock()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self.rate
            self._sleep(wait)


_limiters: Dict[str, TokenBucket] = {}
_limiters_lock = threading.Lock()

# Family defaults for families nobody configured explicitly. Collectors tune
# their own hot families via configure_limiter() before starting workers.
DEFAULT_FAMILY_RATES: Dict[str, Tuple[float, Optional[float]]] = {}


def get_limiter(
    family: str,
    rate_per_sec: float = 1.0,
    burst: Optional[float] = None,
) -> TokenBucket:
    """Return the shared limiter for a host family, creating it on first use."""
    with _limiters_lock:
        bucket = _limiters.get(family)
        if bucket is None:
            rate_per_sec, burst = DEFAULT_FAMILY_RATES.get(
                family, (rate_per_sec, burst)
            )
            bucket = TokenBucket(rate_per_sec, burst)
            _limiters[family] = bucket
        return bucket


def configure_limiter(
    family: str, rate_per_sec: float, burst: Optional[float] = None
) -> TokenBucket:
    """Create or replace the shared limiter for a family (call before workers start)."""
    with _limiters_lock:
        bucket = TokenBucket(rate_per_sec, burst)
        _limiters[family] = bucket
        return bucket


def set_limiter(family: str, bucket: TokenBucket) -> None:
    """Install a pre-built bucket (test hook for injecting fake clocks)."""
    with _limiters_lock:
        _limiters[family] = bucket
