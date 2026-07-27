#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""One hardened GET for every Yahoo Finance call in this repo.

Yahoo fronts `query{1,2}.finance.yahoo.com` with an edge classifier that decides
per request whether the caller is allowed. When it says no it does not answer
with a clean auth error — it answers `403 Forbidden` or `429 Edge: Too Many
Requests`, which read as rate limits but are really the classifier's verdict on
the User-Agent. Retrying the same call unchanged is what fixes the transient
case; changing the UA to something "more realistic" is what breaks it.

DO NOT "upgrade" the User-Agent below. The counter-intuitive result, measured
against the live screener endpoint with interleaved A/B calls (4 rounds, fully
deterministic, not an ordering artifact):

    (no UA) / curl default                  -> 429
    python-requests/2.31.0                  -> 429
    Mozilla/5.0                             -> 200   <- what we send
    Mozilla/5.0 (Macintosh; Intel Mac ...)  -> 200
    Mozilla/5.0 (compatible)                -> 200
    Firefox/124 full UA                     -> 200
    Chrome/58 full UA (pre-client-hints)    -> 200
    Chrome/122 full UA                      -> 429   <- "realistic" fails
    Safari/605 full UA                      -> 429

The rule is not "look more like a browser", it is "do not claim to be a modern
browser you cannot back up". A UA claiming current Chrome/Safari is expected to
arrive with the `Sec-Fetch-*` headers and session cookies a real browser sends;
without them the claim is provably false and the edge drops it. Confirmed:
Chrome/122 + `Sec-Fetch-Site/Mode/Dest` goes back to 200, and adding a `sec-ch-ua`
that doesn't match drops it to 429 again. A generic `Mozilla/5.0` makes no such
claim, so there is nothing to contradict — it is the stable choice, and every
extra header (Accept, Accept-Language, Referer, Origin) is a no-op alongside it.

`v7/finance/quote`, `POST /v1/finance/screener` and `/v1/test/getcrumb` are crumb
gated and stay blocked whatever you send; nothing here recovers them. The
predefined `saved` screener and `v8/finance/chart` are the two doors still open,
and both go through `yahoo_get`.

Consumers (synced in as `scripts/_shared/yahoo_http` by the skill-sync bundle):
`chstock-usmarket-report` (screener + per-ticker chart) and `ch-news-reporter`
(macro position anchors). Import it by putting the *parent* directory on
`sys.path` — `scripts/_shared` in a synced copy, `<repo>/shared` in this repo —
and then `from yahoo_http import yahoo_get`.

This module is pure transport: retry policy, host failover, UA policy. It holds
no domain judgment — which symbols to fetch, what a refusal means for the
evidence, and how to report it stay with the calling skill.
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Dict, Optional

import requests

# Hosts serve identical payloads from separate edge pools, so a verdict on one is
# not a verdict on the other. Alternating across attempts is a real second chance.
YAHOO_HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")

# Deliberately minimal — see the module docstring before touching the UA.
REQUEST_HEADERS: Dict[str, str] = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/plain,*/*",
}

# The edge verdict codes worth retrying. 401 is included because the screener
# occasionally answers it in place of 403 when it wants a cookie handshake.
RETRY_STATUS = frozenset({401, 403, 408, 429, 500, 502, 503, 504})

DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_BASE = 1.5  # seconds; grows 1.5 / 3.0 / 6.0 with jitter on top
MAX_BACKOFF = 20.0

_local = threading.local()

__all__ = ["YahooBlocked", "yahoo_get", "YAHOO_HOSTS", "REQUEST_HEADERS"]


class YahooBlocked(RuntimeError):
    """Every attempt was refused by the edge — distinct from a real HTTP error."""


def _session() -> requests.Session:
    """One Session per thread: the universe scan fetches through a thread pool."""
    session = getattr(_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update(REQUEST_HEADERS)
        _local.session = session
    return session


def _sleep_for(attempt: int, response: Optional[requests.Response]) -> float:
    """Honour Retry-After when Yahoo sends one, else exponential backoff + jitter.

    The jitter matters more than usual here: the universe scan runs 8 workers, and
    without it a shared 429 would resynchronise them into one retry thundering herd.
    """
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), MAX_BACKOFF)
            except ValueError:
                pass
    return min(DEFAULT_BACKOFF_BASE * (2 ** attempt), MAX_BACKOFF) + random.uniform(0, 0.75)


def yahoo_get(
    path: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: int = 20,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> requests.Response:
    """GET a Yahoo Finance API path, retrying the edge verdict across both hosts.

    `path` is host-relative (e.g. `/v8/finance/chart/AAPL`) because the host is
    ours to choose per attempt. Returns the successful Response. Raises
    `YahooBlocked` when every attempt was refused — the caller can then record a
    blocked-by-edge reason instead of a bare "403 Client Error" string, which is
    what previously made this failure hard to read in the evidence JSON.
    """
    last_status: Optional[int] = None
    last_body = ""
    last_error: Optional[Exception] = None

    for attempt in range(max_attempts):
        host = YAHOO_HOSTS[attempt % len(YAHOO_HOSTS)]
        response = None
        try:
            response = _session().get(host + path, params=params, timeout=timeout)
        except requests.RequestException as exc:
            last_error = exc
        else:
            if response.status_code == 200:
                return response
            last_status = response.status_code
            last_body = response.text[:200].strip()
            # A non-retryable status is a real API error (e.g. 400 size is too
            # large, 404 unknown ticker) — surface it now rather than burning
            # three more attempts on a request that will never succeed.
            if response.status_code not in RETRY_STATUS:
                response.raise_for_status()

        if attempt < max_attempts - 1:
            time.sleep(_sleep_for(attempt, response))

    detail = f"HTTP {last_status}: {last_body}" if last_status else f"{type(last_error).__name__}: {last_error}"
    raise YahooBlocked(
        f"Yahoo 拒绝了 {path}（{max_attempts} 次尝试，query1/query2 均失败）——{detail}。"
        "通常是 edge 把请求判成机器人（UA/IP 维度），不是接口下线；稍后重试或换出口 IP。"
    )
