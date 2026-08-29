#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""采集器公共件 —— HTTP 重试、配置读取、运行记录的形状。

一个采集器只做一件事：把某个源的原始响应变成标准化的行。它不算利差、
不判断质量、不决定要不要采信。所有派生量在 pricing/curve/ladder/attribution，
所有判断在模型。
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
import yaml

SKILL_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = SKILL_ROOT / "config"


def load_config(name: str) -> Dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8")) or {}


def fingerprint(payload: Any) -> str:
    """把口径关键参数哈希成指纹。指纹变了的两段序列不许直接相减。"""
    return hashlib.sha1(repr(payload).encode("utf-8")).hexdigest()[:12]


@dataclass
class CollectResult:
    source_id: str
    status: str = "ok"                      # ok | partial | failed
    instruments: List[Dict[str, Any]] = field(default_factory=list)
    observations: List[Dict[str, Any]] = field(default_factory=list)
    note: str = ""
    basket_fingerprint: str = ""

    def merge(self, other: "CollectResult") -> None:
        self.instruments.extend(other.instruments)
        self.observations.extend(other.observations)


def http_get(url: str, *, headers: Optional[Dict[str, str]] = None,
             timeout: int = 45, retries: int = 3, backoff: float = 2.0,
             binary: bool = False) -> Any:
    """带退避的 GET。失败抛异常由调用方决定降级——单源失败不阻断其它源。"""
    last: Optional[Exception] = None
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=headers or {}, timeout=timeout)
            resp.raise_for_status()
            return resp.content if binary else resp.text
        except Exception as exc:                    # noqa: BLE001
            last = exc
            if attempt < retries - 1:
                time.sleep(backoff * (2 ** attempt))
    raise RuntimeError(f"GET {url} 失败：{last}")


class RateLimiter:
    """SEC 要求 10 req/s 上限，我们按 8 走留余量。"""

    def __init__(self, per_second: float) -> None:
        self._min_gap = 1.0 / max(per_second, 0.1)
        self._last = 0.0

    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self._min_gap:
            time.sleep(self._min_gap - gap)
        self._last = time.monotonic()
