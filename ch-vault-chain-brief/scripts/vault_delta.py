#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
金库增量提取器（AlphaVault 侧的"手"）。

只做确定性提取与计数：读来源登记表、扫 wiki 结论块、读催化事件与地缘台账，
按时间窗过滤后原样吐 JSON。**不做任何聚类、命名、归因或结论撰写**——
产业链归属、五维填充、事实/推断标注、假设变化推导全部由模型完成。

数据源（相对 vault 根）：
  system/frameworks/来源登记表.json     摄取登记（含权重/动作/更新页面）
  wiki/**/*.md                          词条页的 `## 可产出结论` 区块
  ledgers/catalyst-events/events.jsonl  催化事件台账
  ledgers/geopolitics/status.json       地缘框架状态机

用法：
  python scripts/vault_delta.py --since 2026-08-03
  python scripts/vault_delta.py --since 2026-08-03 --until 2026-08-08 --part registry
  python scripts/vault_delta.py --week            # 本周一至今
  python scripts/vault_delta.py --days 1          # 日报窗口

vault 根目录取值优先级：--vault 参数 > 环境变量 ALPHAVAULT_ROOT。
两者都缺时返回 {"error": "MISSING_CONFIG"} 并以退出码 2 结束。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

REGISTRY_REL = "system/frameworks/来源登记表.json"
WIKI_REL = "wiki"
CATALYST_REL = "ledgers/catalyst-events/events.jsonl"
GEO_REL = "ledgers/geopolitics/status.json"

# `- **C-存储芯片-06 ｜ 标题**（🟢 高置信｜已上修｜2026-08-07，来源）：正文...`
CONCLUSION_SPLIT = re.compile(r"\n(?=- \*\*C-)")
CONCLUSION_HEAD = re.compile(r"- \*\*(C-[^*]+?)\*\*[（(]([^）)]*)[）)]")
CONCLUSION_SECTION = re.compile(r"\n#+ *可产出结论\s*\n(.*?)(?=\n#+ |\Z)", re.S)
DATE_IN_META = re.compile(r"(20\d{2}-\d{2}-\d{2})")
WEIGHT_CHARS = ("🟢", "🟡", "🔴")


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------
def read_text(path: Path) -> str:
    """金库内各状态文件 BOM 情况不一致，统一用 utf-8-sig 读。"""
    return path.read_text(encoding="utf-8-sig")


def iso_date(value: str) -> str:
    """Argparse type: accept real ISO calendar dates only."""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {value!r}; expected YYYY-MM-DD") from exc


def positive_int(value: str) -> int:
    """Argparse type for inclusive window sizes and text limits."""
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid integer {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def load_json(path: Path) -> Any:
    return json.loads(read_text(path))


def first_weight(value: Optional[str]) -> str:
    """来源权重形如 '🟢 高（Goldman Sachs）'，只取权重符号。"""
    if not value:
        return "?"
    head = value.strip()[:1]
    return head if head in WEIGHT_CHARS else "?"


def in_window(value: Optional[str], since: str, until: str) -> bool:
    """value 是 ISO 时间串或日期串，按前 10 位比较。"""
    if not value:
        return False
    day = str(value)[:10]
    return since <= day <= until


def resolve_vault(arg: Optional[str]) -> Optional[Path]:
    raw = arg or os.environ.get("ALPHAVAULT_ROOT")
    if not raw:
        return None
    return Path(os.path.expanduser(raw)).resolve()


# --------------------------------------------------------------------------
# part: registry —— 来源登记表增量
# --------------------------------------------------------------------------
def extract_registry(vault: Path, since: str, until: str) -> Dict[str, Any]:
    path = vault / REGISTRY_REL
    if not path.exists():
        return {"available": False, "reason": f"not found: {REGISTRY_REL}"}

    raw = load_json(path)
    sources = raw.get("sources", raw) if isinstance(raw, dict) else raw
    if not isinstance(sources, list):
        return {"available": False, "reason": "unexpected registry shape"}

    rows: List[Dict[str, Any]] = []
    for item in sources:
        if not isinstance(item, dict):
            continue
        stamp = item.get("最后摄取时间") or item.get("首次摄取时间")
        if not in_window(stamp, since, until):
            continue
        rows.append(
            {
                "date": str(stamp)[:10],
                "source_type": item.get("来源类型"),
                "action": item.get("摄取动作"),
                "status": item.get("登记状态"),
                "weight": first_weight(item.get("来源权重")),
                "weight_raw": item.get("来源权重"),
                "org": item.get("来源机构"),
                "file": item.get("文件路径"),
                "pages": item.get("更新页面") or [],
                "blocked_reason": item.get("失败原因"),
            }
        )

    rows.sort(key=lambda r: (r["date"], r.get("file") or ""))

    page_hits: Counter = Counter()
    for row in rows:
        for page in row["pages"]:
            page_hits[page] += 1

    return {
        "available": True,
        "total_in_registry": len(sources),
        "count": len(rows),
        "by_action": dict(Counter(r["action"] for r in rows)),
        "by_status": dict(Counter(r["status"] for r in rows)),
        "by_weight": dict(Counter(r["weight"] for r in rows)),
        "by_source_type": dict(Counter(r["source_type"] for r in rows)),
        "by_org": Counter(r["org"] for r in rows if r["org"]).most_common(15),
        "page_update_counts": page_hits.most_common(),
        "blocked": [r for r in rows if r["status"] not in ("ingested", None)],
        "rows": rows,
    }


# --------------------------------------------------------------------------
# part: conclusions —— wiki 结论块增量
# --------------------------------------------------------------------------
def _chain_of(rel: str) -> Optional[str]:
    """wiki/03-产业链图谱/AI基建-存储.md -> AI基建-存储；非产业链页返回 None。"""
    parts = Path(rel).parts
    if len(parts) >= 3 and parts[1].endswith("产业链图谱"):
        return Path(parts[-1]).stem
    return None


def extract_conclusions(vault: Path, since: str, until: str) -> Dict[str, Any]:
    root = vault / WIKI_REL
    if not root.is_dir():
        return {"available": False, "reason": f"not found: {WIKI_REL}"}

    rows: List[Dict[str, Any]] = []
    for md in sorted(root.rglob("*.md")):
        try:
            text = read_text(md)
        except (OSError, UnicodeDecodeError):
            continue
        section = CONCLUSION_SECTION.search(text)
        if not section:
            continue
        rel = md.relative_to(vault).as_posix()
        for block in CONCLUSION_SPLIT.split(section.group(1)):
            conclusion_text = block.strip()
            head = CONCLUSION_HEAD.match(conclusion_text)
            if not head:
                continue
            meta = head.group(2)
            stamps = DATE_IN_META.findall(meta)
            stamp = stamps[-1] if stamps else None
            if not stamp or not (since <= stamp <= until):
                continue
            fields = [f.strip() for f in re.split(r"[｜|]", meta) if f.strip()]
            rows.append(
                {
                    "date": stamp,
                    "page": rel,
                    "page_name": md.stem,
                    "chain": _chain_of(rel),
                    "cid": head.group(1).split("｜")[0].split("|")[0].strip(),
                    "title": head.group(1).strip(),
                    "weight": first_weight(meta),
                    "confidence": fields[0] if fields else None,
                    "status": fields[1] if len(fields) > 1 else None,
                    # Keep the original conclusion block. Metadata alone cannot
                    # support the report's fact/inference and source-weight work.
                    "text": conclusion_text,
                    "chars": len(conclusion_text),
                }
            )

    rows.sort(key=lambda r: (r["date"], r["page_name"]))
    return {
        "available": True,
        "count": len(rows),
        "by_weight": dict(Counter(r["weight"] for r in rows)),
        "by_page_dir": dict(Counter(Path(r["page"]).parts[1] for r in rows)),
        "by_page": Counter(r["page_name"] for r in rows).most_common(),
        "chains_touched": sorted({r["chain"] for r in rows if r["chain"]}),
        "rows": rows,
    }


# --------------------------------------------------------------------------
# part: catalysts —— 催化事件台账增量
# --------------------------------------------------------------------------
def extract_catalysts(vault: Path, since: str, until: str, desc_chars: int) -> Dict[str, Any]:
    path = vault / CATALYST_REL
    if not path.exists():
        return {"available": False, "reason": f"not found: {CATALYST_REL}"}

    rows: List[Dict[str, Any]] = []
    total = 0
    with path.open(encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            total += 1
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not in_window(item.get("created_at"), since, until):
                continue
            rows.append(
                {
                    "created_at": str(item.get("created_at"))[:10],
                    "event_date": item.get("event_date"),
                    "direction": item.get("direction"),
                    "catalyst_type": item.get("catalyst_type"),
                    "catalyst_family": item.get("catalyst_family"),
                    "description": (item.get("event_description") or "")[:desc_chars],
                    "benefit_chain": item.get("benefit_chain") or [],
                }
            )

    rows.sort(key=lambda r: (r["created_at"], r.get("event_date") or ""))
    return {
        "available": True,
        "total_in_ledger": total,
        "count": len(rows),
        "by_direction": dict(Counter(r["direction"] for r in rows)),
        "by_type": dict(Counter(r["catalyst_type"] for r in rows)),
        "rows": rows,
    }


# --------------------------------------------------------------------------
# part: geo —— 地缘框架状态机
# --------------------------------------------------------------------------
def extract_geo(vault: Path) -> Dict[str, Any]:
    path = vault / GEO_REL
    if not path.exists():
        return {"available": False, "reason": f"not found: {GEO_REL}"}

    raw = load_json(path)
    current = raw.get("current", {}) if isinstance(raw, dict) else {}
    frame = current.get("frame", {}) or {}
    return {
        "available": True,
        "profile": raw.get("profile"),
        "as_of": current.get("as_of") or raw.get("as_of"),
        "carried_from": current.get("carried_from"),
        "headline": current.get("headline"),
        "regime": current.get("regime"),
        "frame_change": current.get("frame_change"),
        "path": frame.get("path"),
        "path_labels": raw.get("path_labels"),
        "primary_theater": frame.get("primary_theater"),
        "escalation_rung": frame.get("escalation_rung"),
        "probabilities": frame.get("probabilities"),
        "chokepoint_status": frame.get("chokepoint_status"),
        "war_theaters": frame.get("war_theaters"),
        "transmission_channels": frame.get("transmission_channels"),
        "signal_watchlist": frame.get("signal_watchlist"),
        "next_nodes": current.get("next_nodes"),
        "falsifiers": current.get("falsifiers"),
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def resolve_window(args: argparse.Namespace) -> tuple[str, str]:
    until = args.until or date.today().isoformat()
    if args.since:
        if args.since > until:
            raise ValueError("--since must not be later than --until")
        return args.since, until
    end = date.fromisoformat(until)
    if args.days:
        return (end - timedelta(days=args.days - 1)).isoformat(), until
    # 默认按"本周"：结束日所在周的周一
    return (end - timedelta(days=end.weekday())).isoformat(), until


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="提取 AlphaVault 指定时间窗内的摄取与结论增量")
    parser.add_argument("--vault", help="金库根目录；缺省读环境变量 ALPHAVAULT_ROOT")
    window = parser.add_mutually_exclusive_group()
    window.add_argument("--since", type=iso_date, help="窗口起始日 YYYY-MM-DD")
    window.add_argument("--days", type=positive_int, help="窗口天数（含结束日）")
    window.add_argument("--week", action="store_true", help="按结束日所在自然周（周一起）取窗口")
    parser.add_argument("--until", type=iso_date, help="窗口结束日 YYYY-MM-DD，默认今天")
    parser.add_argument(
        "--part",
        default="all",
        choices=["all", "registry", "conclusions", "catalysts", "geo"],
        help="只取某一段，默认全取",
    )
    parser.add_argument("--desc-chars", type=positive_int, default=220, help="催化事件描述截断长度")
    parser.add_argument("--indent", type=int, default=None, help="JSON 缩进，便于人读")
    args = parser.parse_args(list(argv) if argv is not None else None)

    vault = resolve_vault(args.vault)
    if vault is None:
        json.dump(
            {"error": "MISSING_CONFIG", "detail": "需要 --vault 或环境变量 ALPHAVAULT_ROOT"},
            sys.stdout,
            ensure_ascii=False,
        )
        print()
        return 2
    if not vault.is_dir():
        json.dump(
            {"error": "VAULT_NOT_FOUND", "detail": str(vault)}, sys.stdout, ensure_ascii=False
        )
        print()
        return 2

    try:
        since, until = resolve_window(args)
    except ValueError as exc:
        parser.error(str(exc))
    out: Dict[str, Any] = {
        "vault": str(vault),
        "window": {"since": since, "until": until},
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }

    want = args.part
    if want in ("all", "registry"):
        out["registry"] = extract_registry(vault, since, until)
    if want in ("all", "conclusions"):
        out["conclusions"] = extract_conclusions(vault, since, until)
    if want in ("all", "catalysts"):
        out["catalysts"] = extract_catalysts(vault, since, until, args.desc_chars)
    if want in ("all", "geo"):
        out["geo"] = extract_geo(vault)

    json.dump(out, sys.stdout, ensure_ascii=False, indent=args.indent)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
