#!/usr/bin/env python3
"""ch-ai-eng-tracker 共享机制层：时间窗、HTTP、配置、JSON 输出、multi_search 导入。

只放确定性机制，不做任何领域判断——取什么、怎么筛、报告怎么写都由模型决定。
各 fetch 脚本 import 本模块；同步后 scripts/ 目录整体搬走，相对导入不受影响。
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SHANGHAI = ZoneInfo("Asia/Shanghai")
SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


class ConfigError(RuntimeError):
    """config/sources.yaml 缺失或不可解析。"""


class HttpError(RuntimeError):
    def __init__(self, status: int | str, message: str, url: str = "") -> None:
        super().__init__(f"HTTP {status}: {message} ({url})")
        self.status = status
        self.url = url


# ---------------------------------------------------------------- 时间窗

def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)


def iso_shanghai(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=SHANGHAI)
    return dt.astimezone(SHANGHAI).isoformat(timespec="seconds")


def window_start(days: int) -> datetime:
    return now_shanghai() - timedelta(days=days)


def window_start_utc_iso(days: int) -> str:
    """GitHub API `since=` 用的 UTC ISO8601。"""
    return window_start(days).astimezone(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- HTTP

def make_session(referer: str | None = None):
    import requests  # 延迟导入，缺依赖时给结构化错误

    session = requests.Session()
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8"}
    if referer:
        headers["Referer"] = referer
    session.headers.update(headers)
    return session


def http_get_text(session, url: str, *, timeout: int = 20, headers: dict | None = None) -> str:
    resp = session.get(url, timeout=timeout, headers=headers)
    if resp.status_code != 200:
        raise HttpError(resp.status_code, resp.reason, url)
    if not resp.encoding:
        resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def http_get_json(session, url: str, *, timeout: int = 20, headers: dict | None = None):
    resp = session.get(url, timeout=timeout, headers=headers)
    if resp.status_code != 200:
        raise HttpError(resp.status_code, resp.reason, url)
    return resp.json()


class RateLimiter:
    """同一 host 家族的最小调用间隔，防 403/429。"""

    def __init__(self, min_interval: float) -> None:
        self._min = min_interval
        self._last = 0.0

    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self._min:
            time.sleep(self._min - gap)
        self._last = time.monotonic()


# ---------------------------------------------------------------- 配置

def load_config() -> dict:
    path = SKILL_ROOT / "config" / "sources.yaml"
    if not path.exists():
        raise ConfigError(f"config not found: {path}")
    import yaml

    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001
        raise ConfigError(f"config parse failed: {exc}") from exc


# ---------------------------------------------------------------- 输出

def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def emit_error(code: str, detail, exit_code: int = 1) -> None:
    """整跑失败：结构化错误 + 退出码。部分失败不要走这里，放 degraded 数组。"""
    emit({"error": {"code": code, "detail": detail}})
    raise SystemExit(exit_code)


def write_or_emit(args, payload: dict) -> None:
    """--output 写文件（stdout 只报摘要）；否则完整 JSON 到 stdout。"""
    out = getattr(args, "output", None)
    if not out:
        emit(payload)
        return
    path = Path(out)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    items = payload.get("items")
    if items is None:
        items = sum(len(v) for v in payload.values() if isinstance(v, list))
    emit({"written": str(path), "items": items})


def add_common_args(parser, default_days: int = 7, default_limit: int | None = None) -> None:
    parser.add_argument("--days", type=int, default=default_days, help="时间窗（滚动天数，默认 7）")
    if default_limit is not None:
        parser.add_argument("--limit", type=int, default=default_limit, help="返回条数上限")
    parser.add_argument("--timeout", type=int, default=20, help="单请求超时秒数")
    parser.add_argument("--output", default=None, help="JSON 写入该文件（否则打到 stdout）")


# ---------------------------------------------------------------- 文本

def strip_html(text: str, cap: int | None = None) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        text = TAG_RE.sub(" ", text or "")
    else:
        text = BeautifulSoup(text or "", "html.parser").get_text(" ", strip=True)
    text = WS_RE.sub(" ", text).strip()
    return text[:cap] if cap else text


def keyword_match(text: str, keywords: list[str]) -> list[str]:
    """返回命中的关键词。≤3 字符的按整词匹配（避免 ai 误中 mail）。"""
    low = (text or "").lower()
    hit: list[str] = []
    for kw in keywords:
        kw = str(kw).lower()
        if not kw:
            continue
        if len(kw) <= 3:
            if re.search(rf"\b{re.escape(kw)}\b", low):
                hit.append(kw)
        elif kw in low:
            hit.append(kw)
    return hit


# ---------------------------------------------------------------- multi_search 导入

def resolve_web_search():
    """返回 multi_search 模块；同步包与 dev 仓库两种布局都兼容，找不到返回 None。

    同步后：scripts/_shared/web_search/；开发态：<repo>/shared/web_search/。
    """
    candidates = [SCRIPT_DIR / "_shared" / "web_search", SKILL_ROOT.parent / "shared" / "web_search"]
    for cand in candidates:
        if (cand / "multi_search.py").exists():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            import multi_search

            return multi_search
    return None


def search_via_multi_search(
    query: str,
    *,
    engines: list[str],
    max_results: int = 8,
    days: int | None = None,
    include_domains: list[str] | None = None,
):
    """调用共享 multi_search()，返回其 payload dict（含 error/skipped 语义）。"""
    mod = resolve_web_search()
    if mod is None:
        return {"error": {"code": "shared_module_missing", "detail": "multi_search.py 未找到（检查 _shared/web_search 同步）"}}
    return mod.multi_search(
        query,
        engines=engines,
        mode="failover",
        max_results=max_results,
        days=days,
        include_domains=include_domains or [],
    )
