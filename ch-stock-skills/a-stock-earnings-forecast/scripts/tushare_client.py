#!/usr/bin/env python3
"""Tushare Pro client factory and retry proxy for the earnings-forecast skill.

Token resolution order: TUSHARE_TOKEN env var, then cwd/.env, then
~/.ch-skills/config.env. The proxy adds exponential-backoff retry for transient
network/rate-limit errors only; auth and parameter errors are never retried.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Dict, Optional

try:
    import tushare as ts
except ImportError as exc:  # pragma: no cover - dependency guard
    raise RuntimeError("Missing dependency: install tushare before using this fetcher.") from exc


def user_config_path() -> str:
    """Per-user config written by chstock-env-setup (same file db_core reads)."""
    return os.path.expanduser(
        os.environ.get("CH_SKILLS_CONFIG") or os.path.join("~", ".ch-skills", "config.env")
    )


def _read_env_key(path: str, wanted: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8") as env_file:
        for line in env_file:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == wanted:
                return value.strip().strip('"').strip("'")
    return ""


def get_tushare_token() -> str:
    """Read TUSHARE_TOKEN from the environment, cwd/.env, then ~/.ch-skills/config.env."""
    token = os.environ.get("TUSHARE_TOKEN", "").strip()
    if token:
        return token
    return (_read_env_key(os.path.join(os.getcwd(), ".env"), "TUSHARE_TOKEN")
            or _read_env_key(user_config_path(), "TUSHARE_TOKEN"))


def get_tushare_pro(token: Optional[str] = None):
    """Create a Tushare Pro client."""
    resolved_token = token or get_tushare_token()
    if not resolved_token:
        raise RuntimeError(
            "Missing TUSHARE_TOKEN. Set it in the environment, cwd/.env, "
            "or run chstock-env-setup to save it to ~/.ch-skills/config.env."
        )
    return ts.pro_api(resolved_token)


class TushareUnavailable:
    """Stand-in for the Pro client when no token is configured.

    Every endpoint raises, so each Tushare stage takes the same per-call
    failure path it already has for permission errors on low-point accounts:
    CNInfo facts still ship, Tushare-derived fields come back empty and noted.
    """

    def __init__(self, reason: str):
        self.reason = reason

    def __getattr__(self, name: str) -> Callable:
        def unavailable(*_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError(f"tushare_unavailable: {self.reason}")
        return unavailable


class TushareProxy:
    """Lightweight retry wrapper around Tushare Pro API."""

    def __init__(self, pro: Any, *, attempts: int = 3, backoff: float = 1.0):
        self._pro = pro
        self._attempts = attempts
        self._backoff = backoff
        self._cache: Dict[str, Callable] = {}
        self._lock = threading.Lock()  # __getattr__ is hit from fetch-worker threads

    @staticmethod
    def _is_retriable(exc: Exception) -> bool:
        msg = str(exc).lower()
        # Auth / parameter / permission errors — never retry.
        if any(k in msg for k in ("invalid", "unauthorized", "token", "param", "argument", "permission", "权限")):
            return False
        # Transient network / server errors — retry.
        return any(k in msg for k in (
            "timeout", "timed out", "connection", "reset", "refused",
            "too many requests", "rate limit", "quota", "抱歉", "每分钟",
            "503", "502", "504", "temporary", "unavailable", "busy",
        ))

    def __getattr__(self, name: str) -> Callable:
        cached = self._cache.get(name)
        if cached is not None:
            return cached
        with self._lock:
            if name in self._cache:  # double-checked under lock
                return self._cache[name]
            original = getattr(self._pro, name)

            def wrapper(*args: Any, **kwargs: Any) -> Any:
                last_exc: Optional[Exception] = None
                for i in range(self._attempts):
                    try:
                        return original(*args, **kwargs)
                    except Exception as exc:  # noqa: BLE001 - re-raised below
                        last_exc = exc
                        if not self._is_retriable(exc):
                            raise
                        if i < self._attempts - 1:
                            time.sleep(self._backoff * (2 ** i))
                raise last_exc or RuntimeError(f"Tushare {name} failed after {self._attempts} attempts")

            self._cache[name] = wrapper
        return self._cache[name]
