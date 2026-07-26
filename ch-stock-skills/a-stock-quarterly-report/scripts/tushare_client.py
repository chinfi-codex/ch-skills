#!/usr/bin/env python3
"""Tushare Pro client factory, per-endpoint rate limiter and retry proxy.

Token resolution order: TUSHARE_TOKEN env var, then cwd/.env.

Why a rate limiter here and not in the earnings-forecast skill: the financial
statement endpoints (income / balancesheet / cashflow / fina_indicator) have no
`_vip` bulk variant on a standard account and reject period-wide queries with
"必填参数, ts_code" — a full-market quarterly scan is therefore thousands of
per-code calls, and Tushare caps each endpoint at 200 calls/minute. Without a
limiter the fetch pool trips the cap in seconds and every worker burns its retry
budget on a throttle that a 0.3s wait would have avoided. Buckets are per
endpoint name because the caps are per endpoint, so income and fina_indicator
can saturate concurrently.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, Optional

try:
    import tushare as ts
except ImportError as exc:  # pragma: no cover - dependency guard
    raise RuntimeError("Missing dependency: install tushare before using this fetcher.") from exc


# Tushare's documented per-endpoint cap is 200/min; leave headroom for clock
# skew between our window and theirs.
DEFAULT_CALLS_PER_MIN = int(os.environ.get("TUSHARE_CALLS_PER_MIN", "180"))


def get_tushare_token() -> str:
    """Read TUSHARE_TOKEN from the environment or cwd/.env."""
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token

    env_path = os.path.join(os.getcwd(), ".env")
    if not os.path.exists(env_path):
        return ""

    with open(env_path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == "TUSHARE_TOKEN":
                return value.strip().strip('"').strip("'")
    return ""


def get_tushare_pro(token: Optional[str] = None):
    """Create a Tushare Pro client."""
    resolved_token = token or get_tushare_token()
    if not resolved_token:
        raise RuntimeError("Missing TUSHARE_TOKEN. Set it in the environment or cwd/.env.")
    return ts.pro_api(resolved_token)


class _SlidingWindowLimiter:
    """Sliding-window limiter: at most `limit` acquisitions per 60 seconds."""

    def __init__(self, limit: int):
        self._limit = max(1, limit)
        self._hits: Deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                while self._hits and now - self._hits[0] >= 60.0:
                    self._hits.popleft()
                if len(self._hits) < self._limit:
                    self._hits.append(now)
                    return
                wait = 60.0 - (now - self._hits[0]) + 0.01
            time.sleep(max(wait, 0.01))


class TushareProxy:
    """Retry + per-endpoint rate limiting around the Tushare Pro API."""

    def __init__(self, pro: Any, *, attempts: int = 4, backoff: float = 1.0,
                 calls_per_min: int = DEFAULT_CALLS_PER_MIN):
        self._pro = pro
        self._attempts = attempts
        self._backoff = backoff
        self._calls_per_min = calls_per_min
        self._cache: Dict[str, Callable] = {}
        self._limiters: Dict[str, _SlidingWindowLimiter] = {}
        self._lock = threading.Lock()  # __getattr__ is hit from fetch-worker threads

    @staticmethod
    def _is_rate_limit(exc: Exception) -> bool:
        msg = str(exc)
        return "频率超限" in msg or "too many requests" in msg.lower() or "rate limit" in msg.lower()

    @classmethod
    def _is_retriable(cls, exc: Exception) -> bool:
        if cls._is_rate_limit(exc):
            return True
        msg = str(exc).lower()
        # Auth / parameter / permission errors — never retry.
        if any(k in msg for k in ("invalid", "unauthorized", "token", "param", "argument", "permission", "权限")):
            return False
        # Transient network / server errors — retry.
        return any(k in msg for k in (
            "timeout", "timed out", "connection", "reset", "refused",
            "quota", "抱歉", "每分钟",
            "503", "502", "504", "temporary", "unavailable", "busy",
        ))

    def _limiter(self, name: str) -> _SlidingWindowLimiter:
        limiter = self._limiters.get(name)
        if limiter is None:
            with self._lock:
                limiter = self._limiters.setdefault(name, _SlidingWindowLimiter(self._calls_per_min))
        return limiter

    def __getattr__(self, name: str) -> Callable:
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        with self._lock:
            if name in self._cache:  # double-checked under lock
                return self._cache[name]
            original = getattr(self._pro, name)
            limiter = self._limiters.setdefault(name, _SlidingWindowLimiter(self._calls_per_min))

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                last_exc: Optional[Exception] = None
                for i in range(self._attempts):
                    limiter.acquire()
                    try:
                        return original(*args, **kwargs)
                    except Exception as exc:  # noqa: BLE001 - re-raised below
                        last_exc = exc
                        if not self._is_retriable(exc):
                            raise
                        if i < self._attempts - 1:
                            # A throttle means the server's window is already
                            # full; waiting out a whole window is cheaper than
                            # exponential guessing.
                            time.sleep(20.0 if self._is_rate_limit(exc) else self._backoff * (2 ** i))
                raise last_exc or RuntimeError(f"Tushare {name} failed after {self._attempts} attempts")

            self._cache[name] = wrapper
        return self._cache[name]
