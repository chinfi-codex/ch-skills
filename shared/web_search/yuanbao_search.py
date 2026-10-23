#!/usr/bin/env python3
"""元宝搜索（Tencent WSA）helper —— 原子联网能力（脚本只取数，不下结论）。

从环境变量或就近的 .env 读腾讯云密钥（TENCENTCLOUD_SECRET_ID /
TENCENTCLOUD_SECRET_KEY，兼容 TENCENT_SECRET_ID / TENCENT_SECRET_KEY），
按 Tencent Cloud API 3.0 规范做 TC3-HMAC-SHA256 签名后调用元宝搜索
SearchPro 接口（https://wsa.tencentcloudapi.com），把结构化结果以 JSON
打到 stdout。脚本不调 LLM、不做相关性判断——查什么、怎么读结果由模型决定。

与 tavily_search.py / deepseek_search.py 同一契约：成功 {"query",
"results"}，失败 {"query", "error": {code, detail}}。元宝搜索带腾讯内容
生态（腾讯新闻、搜狗百科、企鹅号等），A股公告/官方信源核查时优先。

依赖：需要已开通元宝搜索（控制台 https://console.cloud.tencent.com/wsapi/index），
未开通时 API 回 ResourceNotFound，脚本原样透传。

用法：
    python yuanbao_search.py "寒武纪 业绩预告"
    python yuanbao_search.py "光模块 订单" --freshness d7 --cnt 20
    python yuanbao_search.py "腾讯财报" --site exclude:qq.com|sohu.com
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

ENDPOINT = "https://wsa.tencentcloudapi.com"
ENDPOINT_HOST = "wsa.tencentcloudapi.com"
API_SERVICE = "wsa"
API_ACTION = "SearchPro"
API_VERSION = "2025-05-08"
SIGNED_HEADERS = "content-type;host;x-tc-action"
CONTENT_TYPE = "application/json; charset=utf-8"

# Freshness 取值见 API 文档：相对窗口 d/m/y，或绝对日期 / 日期区间
_FRESHNESS_PATTERNS = (
    re.compile(r"^d(?:[1-9]|[12]\d|30)$"),
    re.compile(r"^m(?:[1-9]|1[0-2])$"),
    re.compile(r"^y[1-5]$"),
    re.compile(r"^\d{4}-\d{2}-\d{2}$"),
    re.compile(r"^\d{4}-\d{2}-\d{2},\d{4}-\d{2}-\d{2}$"),
)


def _read_keys_from_env_file(path: Path, names: set[str]) -> dict[str, str]:
    """从单个 .env 文件里读指定 key（容错，读不到的名字不出现）。"""
    found: dict[str, str] = {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                key = key.strip()
                if key in names and key not in found:
                    found[key] = value.strip().strip('"').strip("'")
    except OSError:
        return found
    return found


def _keys_from_env_files(names: set[str]) -> dict[str, str]:
    """就近找 .env：先 cwd，再脚本上溯最多 6 层；先找到的值优先。"""
    candidates = [Path.cwd() / ".env"]
    here = Path(__file__).resolve()
    candidates += [parent / ".env" for parent in list(here.parents)[:6]]

    merged: dict[str, str] = {}
    seen: set[Path] = set()
    for cand in candidates:
        if cand in seen or not cand.exists():
            continue
        seen.add(cand)
        for name, value in _read_keys_from_env_file(cand, names).items():
            merged.setdefault(name, value)
    return merged


def get_tencent_credentials() -> tuple[str, str]:
    """腾讯云 SecretId/SecretKey：优先环境变量，否则就近的 .env。"""
    id_names = {"TENCENTCLOUD_SECRET_ID", "TENCENT_SECRET_ID"}
    key_names = {"TENCENTCLOUD_SECRET_KEY", "TENCENT_SECRET_KEY"}

    secret_id = next(
        (v for v in (os.environ.get(n, "").strip() for n in sorted(id_names)) if v), ""
    )
    secret_key = next(
        (v for v in (os.environ.get(n, "").strip() for n in sorted(key_names)) if v), ""
    )
    from_files = _keys_from_env_files(id_names | key_names)
    secret_id = secret_id or next(
        (from_files[n] for n in sorted(id_names) if n in from_files), ""
    )
    secret_key = secret_key or next(
        (from_files[n] for n in sorted(key_names) if n in from_files), ""
    )
    return secret_id, secret_key


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def build_tc3_headers(
    payload: str,
    secret_id: str,
    secret_key: str,
    timestamp: int,
) -> dict[str, str]:
    """按 Tencent Cloud API 3.0 规范构造带 TC3-HMAC-SHA256 签名的请求头。"""
    date = datetime.fromtimestamp(timestamp, tz=timezone.utc).strftime("%Y-%m-%d")
    canonical_request = (
        "POST\n"
        "/\n"
        "\n"
        f"content-type:{CONTENT_TYPE}\n"
        f"host:{ENDPOINT_HOST}\n"
        f"x-tc-action:{API_ACTION.lower()}\n"
        "\n"
        f"{SIGNED_HEADERS}\n"
        f"{hashlib.sha256(payload.encode('utf-8')).hexdigest()}"
    )
    credential_scope = f"{date}/{API_SERVICE}/tc3_request"
    string_to_sign = (
        "TC3-HMAC-SHA256\n"
        f"{timestamp}\n"
        f"{credential_scope}\n"
        f"{hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()}"
    )

    k_date = _hmac_sha256(("TC3" + secret_key).encode("utf-8"), date)
    k_service = _hmac_sha256(k_date, API_SERVICE)
    k_signing = _hmac_sha256(k_service, "tc3_request")
    signature = hmac.new(
        k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    return {
        "Content-Type": CONTENT_TYPE,
        "Host": ENDPOINT_HOST,
        "X-TC-Action": API_ACTION,
        "X-TC-Version": API_VERSION,
        "X-TC-Timestamp": str(timestamp),
        "Authorization": (
            f"TC3-HMAC-SHA256 Credential={secret_id}/{credential_scope}, "
            f"SignedHeaders={SIGNED_HEADERS}, Signature={signature}"
        ),
    }


def _validate_freshness(freshness: str) -> str | None:
    if not any(pattern.fullmatch(freshness) for pattern in _FRESHNESS_PATTERNS):
        return (
            "freshness must be d[1-30] / m[1-12] / y[1-5] / YYYY-MM-DD "
            "or 'YYYY-MM-DD,YYYY-MM-DD'"
        )
    today = date.today()
    for part in freshness.split(","):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", part):
            continue
        try:
            parsed = datetime.strptime(part, "%Y-%m-%d").date()
        except ValueError:
            return f"freshness contains an invalid date: {part}"
        if parsed < date(1970, 1, 1):
            return f"freshness date cannot be before 1970-01-01: {part}"
        if parsed > today:
            return f"freshness date cannot be later than today ({today.isoformat()}): {part}"
    if "," in freshness:
        start, end = freshness.split(",")
        # 文档正文写反了约束（示例为 A<B），按示例执行：起点不得晚于终点
        if start > end:
            return f"freshness range start must be on or before end: {freshness}"
    return None


def _validate_site(site: str) -> str | None:
    parts = [p for p in site.split("|") if p.strip()]
    if not parts:
        return "site cannot be blank"
    if parts[0].startswith("exclude:"):
        parts = [parts[0][len("exclude:") :], *parts[1:]]
        if len(parts) > 5:
            return "site excludes at most 5 domains"
    elif len(parts) > 1:
        # 文档约束：指定网址搜索每次仅支持一个域名，多个域名只支持排除形态
        return "site 站内搜索仅支持一个域名；排除多个域名用 'exclude:a.com|b.com'"
    return None


def _normalize_page(raw: object) -> dict | None:
    """Pages 数组里每项是 JSON 字符串：公共字段归一，其余（含旗舰版独有的
    authority_level / deeplinks、通用 pics / favicon）原样保留，不丢字段。"""
    if isinstance(raw, dict):
        item = raw
    elif isinstance(raw, str):
        try:
            item = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(item, dict):
            return None
    else:
        return None
    consumed = ("title", "date", "content", "passage")
    normalized = {
        "title": item.get("title"),
        "url": item.get("url"),
        "published_date": item.get("date"),
        "content": item.get("content") or item.get("passage"),
    }
    for key, value in item.items():
        if key not in consumed:
            normalized[key] = value
    return normalized


def yuanbao_search(
    query: str,
    *,
    mode: int | None = None,
    site: str | None = None,
    cnt: int | None = None,
    industry: str | None = None,
    freshness: str | None = None,
    deeplinks: bool | None = None,
    secret_id: str | None = None,
    secret_key: str | None = None,
) -> dict:
    """调用元宝搜索 SearchPro，返回精简后的结构化 dict；任何失败都回结构化 error。"""
    if not query.strip():
        return {
            "query": query,
            "error": {"code": "invalid_parameters", "detail": "query cannot be blank"},
        }
    param_error = None
    if site:
        param_error = _validate_site(site)
    if param_error is None and freshness:
        param_error = _validate_freshness(freshness)
    if param_error:
        return {
            "query": query,
            "error": {"code": "invalid_parameters", "detail": param_error},
        }

    try:
        import requests  # 本地导入，缺依赖时给清晰报错
    except ImportError:
        return {
            "query": query,
            "error": {"code": "missing_dependency", "detail": "pip install requests"},
        }

    api_id, api_key = (secret_id, secret_key) if secret_id and secret_key else get_tencent_credentials()
    if not api_id or not api_key:
        return {
            "query": query,
            "error": {
                "code": "missing_api_key",
                "detail": (
                    "Set TENCENTCLOUD_SECRET_ID and TENCENTCLOUD_SECRET_KEY "
                    "in the environment or repo-root .env."
                ),
            },
        }

    payload: dict = {"Query": query}
    if mode is not None:
        payload["Mode"] = mode
    if site:
        payload["Site"] = site
    if cnt is not None:
        payload["Cnt"] = cnt
    if industry:
        payload["Industry"] = industry
    if freshness:
        payload["Freshness"] = freshness
    if deeplinks is not None:
        payload["Deeplinks"] = deeplinks
    body = json.dumps(payload, ensure_ascii=False)

    headers = build_tc3_headers(body, api_id, api_key, int(time.time()))
    try:
        resp = requests.post(ENDPOINT, data=body.encode("utf-8"), headers=headers, timeout=30)
    except requests.RequestException as exc:
        return {
            "query": query,
            "error": {"code": "request_failed", "detail": str(exc)},
        }

    if resp.status_code != 200:
        return {
            "query": query,
            "error": {
                "code": f"http_{resp.status_code}",
                "detail": resp.text[:500],
            },
        }

    try:
        data = resp.json()
    except ValueError:
        return {
            "query": query,
            "error": {"code": "invalid_response", "detail": resp.text[:500]},
        }

    body_data = data.get("Response", data)
    if isinstance(body_data, dict) and body_data.get("Error"):
        err = body_data["Error"]
        return {
            "query": query,
            "error": {
                "code": err.get("Code") or "api_error",
                "detail": err.get("Message") or json.dumps(err, ensure_ascii=False)[:500],
            },
        }

    pages = [
        normalized
        for raw in body_data.get("Pages", [])
        if (normalized := _normalize_page(raw)) is not None
    ]
    payload: dict = {"query": query, "results": pages}
    # Version 告知本次由哪一档服务应答（lite/standard/flagship），Msg 承载
    # "hit black query" 之类的引擎提示；缺省时不出现在返回里。
    version = body_data.get("Version")
    msg = body_data.get("Msg")
    if version or msg:
        payload["meta"] = {"version": version, "msg": msg}
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="元宝搜索（Tencent WSA SearchPro）-> JSON（脚本只取数，模型判断）。"
    )
    parser.add_argument("query", help="检索词")
    parser.add_argument(
        "--mode",
        type=int,
        choices=[0, 1, 2],
        default=None,
        help="0=公开网页信源，1=优质权威垂直信源(VR)，2=混合（极速版不支持）",
    )
    parser.add_argument(
        "--site",
        default=None,
        help="站内搜索仅一个域名如 'qq.com'；排除用 'exclude:a.com|b.com'（最多 5 个）",
    )
    parser.add_argument(
        "--cnt",
        type=int,
        choices=[10, 20, 30, 40, 50],
        default=None,
        help="返回条数，只能取 10/20/30/40/50（极速版不支持）",
    )
    parser.add_argument(
        "--industry",
        choices=["gov", "news", "acad", "finance"],
        default=None,
        help="信源行业约束（极速版不支持）",
    )
    parser.add_argument(
        "--freshness",
        default=None,
        help="时效窗口：d7 / m3 / y1 / 2026-01-01 / '2026-01-01,2026-06-30'",
    )
    parser.add_argument(
        "--deeplinks",
        action="store_true",
        help="请求结果站内链接（仅旗舰版支持）",
    )
    args = parser.parse_args(argv)

    result = yuanbao_search(
        args.query,
        mode=args.mode,
        site=args.site,
        cnt=args.cnt,
        industry=args.industry,
        freshness=args.freshness,
        deeplinks=True if args.deeplinks else None,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if "error" not in result else 1


if __name__ == "__main__":
    sys.exit(main())
