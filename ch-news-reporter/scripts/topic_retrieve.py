#!/usr/bin/env python3
"""自定义关注主题的多通道检索编排层(topic retrieve)。

按 config/custom_topics.yaml 的主题配置,编排 scripts/retrievers/ 下的原子
通道(news_db / pg_data / web_search / vault_fs),合并去重后输出主题证据包
(evidence packet,JSON/markdown,风格对齐 prepare_report_data.py)。

脚本只做取数 / 去重 / 落库 / 打包:
- web_search 结果落库 items(source_type='web_search',带 query/retrieved_at),
  并自动做 retention_days 清理(也可 --groom 单独触发);
- coverage 段如实记录每通道取到多少条、哪些通道降级 / 跳过 / 失败;
- 哪些证据重要、今天有没有边际变化,全归模型判断。

用法:
    python scripts/topic_retrieve.py --topic nvidia_rubin --date today --format json
    python scripts/topic_retrieve.py --topic all --date today --format markdown
    python scripts/topic_retrieve.py --groom --date today
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from db_adapter import (  # noqa: E402
    ensure_connectable as db_ensure_connectable,
    get_connection,
    get_latest_report_state as db_get_latest_report_state,
    init_news_schema,
)
from profile_config import (  # noqa: E402
    CUSTOM_TOPICS_CONFIG,
    custom_profile_name,
    load_custom_profile,
    load_custom_topics,
)
from retrievers import news_db, pg_data, vault_fs, web_search  # noqa: E402
from retrievers.common import compact_text, dedupe_pair, json_loads  # noqa: E402

SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_DB = Path("data/news_research.sqlite")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="自定义关注主题的多通道检索编排:检索 → 去重/落库 → 主题证据包。"
    )
    parser.add_argument(
        "--topic",
        default=None,
        help="主题 slug,或 all(遍历 active 且 frequency=daily 的主题);--groom 时不需要。",
    )
    parser.add_argument("--date", default="today", help="today 或 YYYY-MM-DD。")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 数据库路径(仅 sqlite fallback)。")
    parser.add_argument(
        "--custom-config",
        default=str(CUSTOM_TOPICS_CONFIG),
        help="自定义主题配置 YAML 路径。",
    )
    parser.add_argument(
        "--format",
        choices=["markdown", "json"],
        default="json",
        help="证据包输出格式。",
    )
    parser.add_argument(
        "--groom",
        action="store_true",
        help="只做 web_search 落库行的 retention 清理,不做检索。",
    )
    return parser.parse_args()


def now_iso() -> str:
    return datetime.now(SHANGHAI).isoformat(timespec="seconds")


def resolve_date_key(value: str) -> str:
    if value == "today":
        return datetime.now(SHANGHAI).strftime("%Y-%m-%d")
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError as exc:
        raise SystemExit("--date must be 'today' or YYYY-MM-DD") from exc


# ---------------------------------------------------------------------------
# 证据合并与 coverage
# ---------------------------------------------------------------------------
def merge_evidence(channels: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """合并各通道证据为扁平 items 列表,并做跨通道去重。

    去重规则与 web_search 落库一致:规范化 URL 与规范化标题都相同才算重复;
    news_db(库内已采集新闻)优先保留,web_search 与之重复的丢弃并计数。
    vault 笔记与 pg 数据是异构证据,不参与 URL/标题判重,原样追加。
    """
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    dropped = 0
    for name in ("news_db", "web_search"):
        payload = channels.get(name) or {}
        for item in payload.get("items") or []:
            pair = dedupe_pair(item.get("url"), item.get("title"))
            if pair in seen:
                dropped += 1
                continue
            seen.add(pair)
            merged.append({**item, "channel": name})
    for note in (channels.get("alpha_vault") or {}).get("notes") or []:
        merged.append({**note, "channel": "alpha_vault"})
    hooks = (channels.get("pg_data") or {}).get("hooks") or {}
    for hook_name, payload in hooks.items():
        for key, rows in (payload.get("keys") or {}).items():
            for row in rows:
                merged.append({"channel": "pg_data", "hook": hook_name, "key": key, "row": row})
    return merged, dropped


def build_coverage(
    channels: dict[str, Any], groomed: int, merged_dropped: int
) -> dict[str, Any]:
    """coverage 段:每通道状态/条数/告警 + 整体降级标记(模型据此给判断降级)。"""
    summary: dict[str, Any] = {}
    degraded = False
    for name, payload in channels.items():
        if payload is None:
            continue
        status = str(payload.get("status") or "ok")
        if status in {"degraded", "error"}:
            degraded = True
        summary[name] = {
            "status": status,
            "count": int(payload.get("count") or 0),
            "warnings": payload.get("warnings") or [],
        }
    enabled = [name for name, payload in summary.items() if payload["status"] != "skipped"]
    complete = bool(enabled) and not degraded and all(
        summary[name]["status"] == "ok" for name in enabled
    )
    coverage: dict[str, Any] = {
        "channels": summary,
        "degraded": degraded,
        "complete": complete,
        "groomed_rows": groomed,
        "merged_duplicates_dropped": merged_dropped,
    }
    budget = (channels.get("web_search") or {}).get("budget")
    if budget:
        coverage["web_budget"] = budget
    return coverage


# ---------------------------------------------------------------------------
# 核心:单主题检索
# ---------------------------------------------------------------------------
def retrieve_topic(
    slug: str,
    date_key: str,
    db_path: str,
    config_path: Path | str = CUSTOM_TOPICS_CONFIG,
) -> dict[str, Any]:
    """单主题多通道检索 → 去重/落库 → 主题证据包(dict)。

    供 CLI 与 prepare_report_data 的 custom 路径共用。archived 主题直接报错
    退出(归档不再检索);paused 主题允许显式检索,但在证据包与 coverage 里
    标注状态,提醒模型这是手动补跑。
    """
    profile = load_custom_profile(slug, config_path)
    topic = profile["topic_config"]
    settings = profile["custom_settings"]
    status = str(topic.get("status") or "active")
    if status == "archived":
        raise SystemExit(
            f"主题 {slug!r} 已归档(archived),不再检索;历史 watchboard 保留可回看。"
        )

    db_ensure_connectable(db_path)
    channels: dict[str, Any] = {}
    groomed = 0
    prior_state = None
    with get_connection(db_path) as con:
        init_news_schema(con)
        channels["news_db"] = news_db.retrieve(con, topic, date_key)
        channels["pg_data"] = pg_data.retrieve(con, topic, date_key)
        channels["web_search"] = web_search.retrieve(con, slug, topic, date_key, settings)
        channels["alpha_vault"] = vault_fs.retrieve(topic, settings)
        retention = int(
            ((settings.get("web_search") or {}).get("retention_days")) or 0
        )
        if retention > 0:
            groomed = web_search.groom(con, retention, date_key)
        if profile.get("state_enabled"):
            row = db_get_latest_report_state(
                con, custom_profile_name(slug), before_date_key=date_key
            )
            if row:
                prior_state = {
                    "state_date_key": str(row.get("date_key")),
                    "updated_at": str(row.get("updated_at")) if row.get("updated_at") else None,
                    "watchboard": json_loads(str(row.get("payload")), {}),
                }

    merged, dropped = merge_evidence(channels)
    packet: dict[str, Any] = {
        "custom_topic": True,
        "profile": custom_profile_name(slug),
        "profile_title": profile.get("title") or slug,
        "reference_dir": profile.get("reference_dir") or "custom_topic",
        "date_key": date_key,
        "generated_at": now_iso(),
        "topic": {
            "slug": slug,
            "title": profile.get("title") or slug,
            "status": status,
            "frequency": str(topic.get("frequency") or "daily"),
            "focus": str(topic.get("focus") or ""),
            "keywords": topic.get("keywords") or [],
            "exclude_keywords": topic.get("exclude_keywords") or [],
            "time_window_days": int(topic.get("time_window_days") or 3),
        },
        "state_enabled": bool(profile.get("state_enabled")),
        "prior_state": prior_state,
        "channels": channels,
        "coverage": build_coverage(channels, groomed, dropped),
        "items": merged,
        "items_count": len(merged),
    }
    if status == "paused":
        packet["coverage"]["channels"].setdefault("_topic", {})["warnings"] = [
            f"主题当前为 paused,本次为手动补跑;--topic all 不会带上它"
        ]
        packet["coverage"]["topic_status"] = "paused"
    return packet


# ---------------------------------------------------------------------------
# markdown 输出(风格对齐 prepare_report_data.emit_markdown)
# ---------------------------------------------------------------------------
def _emit_prior_watchboard(packet: dict[str, Any]) -> None:
    print("## Prior Watchboard (carry-forward)\n")
    if not packet.get("state_enabled"):
        print("- 本主题已关闭跨天状态(state_enabled false 或 open_budget 0),无 carry-forward。\n")
        return
    prior = packet.get("prior_state")
    if not prior:
        print(
            "- None found — 冷启动。按主题 focus 构造第一份 watchboard,"
            "写报告后用 save_report_state.py 回写。\n"
        )
        return
    watchboard = prior.get("watchboard") or {}
    open_items = [
        item
        for item in (watchboard.get("tracking_items") or [])
        if isinstance(item, dict) and item.get("status") == "open"
    ]
    print(f"- State date: {prior.get('state_date_key')}")
    try:
        gap = (
            datetime.strptime(str(packet.get("date_key")), "%Y-%m-%d")
            - datetime.strptime(str(prior.get("state_date_key")), "%Y-%m-%d")
        ).days
    except (ValueError, TypeError):
        gap = None
    if gap and gap > 1:
        print(
            f"  ⚠️ 距上一期 watchboard {gap} 天,中间 {gap - 1} 天无状态 —— "
            "勿把数据空洞误读为没有进展"
        )
    print(f"- Regime: {watchboard.get('regime')}")
    print(f"- Open tracking items to reconcile today: {len(open_items)}")
    for item in open_items:
        print(
            f"  - {item.get('id')} (opened {item.get('opened')}): "
            f"{item.get('statement')}"
        )
    print(
        "- Full prior watchboard JSON:\n"
        + json.dumps(watchboard, ensure_ascii=False, indent=2, default=str)
        + "\n"
    )


def _emit_item(prefix: str, item: dict[str, Any]) -> None:
    time_label = item.get("published_at") or item.get("fetched_at") or "unknown"
    url = item.get("url") or ""
    link = f" ([link]({url}))" if url else ""
    print(f"### {prefix}{item.get('title') or '(untitled)'}{link}")
    print(f"- Time: {time_label}")
    if item.get("id"):
        print(f"- Item ID: {item.get('id')}")
    if item.get("matched_keywords"):
        print(f"- Matched: {', '.join(str(k) for k in item['matched_keywords'])}")
    metadata = item.get("metadata") or {}
    if metadata.get("query"):
        print(f"- Query: {metadata['query']} (retrieved_at: {metadata.get('retrieved_at')})")
    if item.get("content"):
        print(f"- Summary: {compact_text(item.get('content'), 520)}")
    print()


def emit_topic_markdown(packet: dict[str, Any]) -> None:
    topic = packet["topic"]
    print(f"# Topic Evidence Packet: {packet['profile']} / {packet['date_key']}\n")
    print(f"- Generated: {packet['generated_at']}")
    print(f"- Title: {topic['title']} (status={topic['status']}, frequency={topic['frequency']})")
    print(f"- Focus: {topic['focus'] or '(未填写)'}")
    print(
        f"- Keywords: {', '.join(topic['keywords']) or '(none)'}; "
        f"window={topic['time_window_days']}d"
    )
    print(f"- Merged items: {packet['items_count']}\n")

    coverage = packet.get("coverage") or {}
    print("## Coverage (multi-channel)\n")
    for name, summary in (coverage.get("channels") or {}).items():
        if name.startswith("_"):
            continue
        line = f"- {name}: {summary['status']}, {summary['count']} 条"
        print(line)
        for warning in summary.get("warnings") or []:
            print(f"  ⚠️ {warning}")
    budget = coverage.get("web_budget") or {}
    if budget:
        print(
            f"- web 预算: 主题 {budget.get('topic_used')}/{budget.get('topic_max')}, "
            f"全局 {budget.get('global_used')}/{budget.get('global_max')}"
            f"(本次执行 {budget.get('ran_now')} 条 query)"
        )
    if coverage.get("groomed_rows"):
        print(f"- retention 清理: 删除过期 web_search 行 {coverage['groomed_rows']} 条")
    if coverage.get("merged_duplicates_dropped"):
        print(f"- 跨通道去重: 丢弃 {coverage['merged_duplicates_dropped']} 条重复")
    print()

    _emit_prior_watchboard(packet)

    channels = packet.get("channels") or {}

    news_items = (channels.get("news_db") or {}).get("items") or []
    if news_items:
        print(f"## news_db ({len(news_items)})\n")
        for item in news_items:
            _emit_item("", item)

    web_channel = channels.get("web_search") or {}
    if web_channel.get("queries"):
        print("## web_search queries\n")
        for report in web_channel["queries"]:
            if report.get("status") == "error":
                print(f"- ✗ {report['query']} → error: {report.get('error')}")
            else:
                print(
                    f"- {report['query']} → {report.get('results')} 条结果,"
                    f"新入库 {report.get('kept')},重复丢弃 {report.get('duplicated')}"
                )
        print()
    web_items = web_channel.get("items") or []
    if web_items:
        print(f"## web_search ({len(web_items)})\n")
        for item in web_items:
            _emit_item("", item)

    notes = (channels.get("alpha_vault") or {}).get("notes") or []
    if notes:
        print(f"## alpha_vault ({len(notes)} 篇命中笔记)\n")
        for note in notes:
            print(f"### {note.get('title') or note.get('path')}")
            print(f"- Path: {note.get('path')} (mtime: {note.get('mtime')})")
            print(f"- Matched: {', '.join(note.get('matched_keywords') or [])}")
            for snippet in note.get("snippets") or []:
                print(f"- Snippet: {snippet}")
            print()

    hooks = (channels.get("pg_data") or {}).get("hooks") or {}
    if hooks:
        print("## pg_data\n")
        for hook_name, payload in hooks.items():
            print(f"### {hook_name} → {payload.get('table')}\n")
            for key, rows in (payload.get("keys") or {}).items():
                print(f"- {key}: {len(rows)} 行")
                if rows:
                    print(
                        "```json\n"
                        + json.dumps(rows, ensure_ascii=False, indent=2, default=str)
                        + "\n```"
                    )
        print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def run_groom(date_key: str, db_path: str, config_path: str) -> int:
    """独立 retention 清理入口:删除过期 web_search 行。"""
    payload = load_custom_topics(config_path)
    retention = int(
        ((payload["settings"].get("web_search") or {}).get("retention_days")) or 0
    )
    if retention <= 0:
        print(json.dumps({"groomed_rows": 0, "reason": "retention_days 未配置"}, ensure_ascii=False))
        return 0
    db_ensure_connectable(db_path)
    with get_connection(db_path) as con:
        init_news_schema(con)
        deleted = web_search.groom(con, retention, date_key)
    print(
        json.dumps(
            {"groomed_rows": deleted, "retention_days": retention, "as_of": date_key},
            ensure_ascii=False,
        )
    )
    return 0


def main() -> int:
    args = parse_args()
    date_key = resolve_date_key(args.date)
    db_path = str(Path(args.db))

    if args.groom:
        try:
            return run_groom(date_key, db_path, args.custom_config)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    if not args.topic:
        raise SystemExit("--topic 为必填(或用 --groom 只做 retention 清理)")

    if args.topic == "all":
        payload = load_custom_topics(args.custom_config)
        slugs = [
            slug
            for slug, topic in payload["topics"].items()
            if str(topic.get("status") or "active") == "active"
            and str(topic.get("frequency") or "daily") == "daily"
        ]
        if not slugs:
            raise SystemExit("没有 active 且 frequency=daily 的自定义主题。")
    else:
        slugs = [args.topic]

    packets: dict[str, Any] = {}
    failures: dict[str, str] = {}
    try:
        for slug in slugs:
            try:
                packets[slug] = retrieve_topic(
                    slug, date_key, db_path, args.custom_config
                )
            except SystemExit:
                raise
            except Exception as exc:  # 单主题失败不拖垮批量,记入输出
                failures[slug] = str(exc)
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.format == "json":
        output: Any = packets[slugs[0]] if args.topic != "all" else {
            "date_key": date_key,
            "generated_at": now_iso(),
            "topics": packets,
            "failures": failures,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, default=str))
    else:
        for slug in slugs:
            if slug in packets:
                emit_topic_markdown(packets[slug])
                print("\n---\n")
            elif slug in failures:
                print(f"# {slug}: 检索失败\n\n- {failures[slug]}\n\n---\n")
    return 1 if failures else 0


if __name__ == "__main__":
    if sys.platform == "win32":
        sys.stdout = io.TextIOWrapper(
            sys.stdout.buffer, encoding="utf-8", errors="replace"
        )
    raise SystemExit(main())
