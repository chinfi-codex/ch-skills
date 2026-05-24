#!/usr/bin/env python3
"""Orchestrator: assemble an AlphaVault-compatible Evidence Pack for single-day
rise attribution.

This script does NOT analyze, classify, or judge. It only chains atomic
fetchers (AlphaVault registry lookup, Tushare price/sector snapshot, sibling
skill announcement/QA scrapers) and emits a Pack skeleton with empty `claims`
and `analysis` fields. The model fills those in afterward.

Order of operations:

  1. price_window_snapshot.py        → data_points.price_snapshot
  2. query_alphavault_raw.py         → input_sources.alphavault_registry
                                        (skipped cleanly if ALPHAVAULT_ROOT missing)
  3. sibling cninfo announcement     → input_sources.announcements (skippable)
  4. sibling interactive QA          → input_sources.interactive_qa (skippable)

Pack schema follows 00-治理总则.md §标准 Pack 格式:
  pack_type: Evidence Pack
  pack_subtype: AttributionEvidence
  write_permission: false
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[2]  # ch-skills root
SIBLING_CNINFO = REPO_ROOT / "ch-stock-skills" / "chstock-cninfo-announcement" / "scripts" / "cninfo_announcement_search.py"
SIBLING_QA = REPO_ROOT / "ch-stock-skills" / "chstock-interactive-qa" / "scripts" / "interactive_qa_search.py"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="组装个股上涨归因 Evidence Pack（不做判断）")
    parser.add_argument("--name", required=True, help="股票中文简称，用于互动问答搜索")
    parser.add_argument("--ts-code", required=True, help="Tushare ts_code，如 300308.SZ")
    parser.add_argument("--trade-date", required=True, help="目标交易日 YYYY-MM-DD")
    parser.add_argument("--pct-chg", type=float, required=True, help="当日涨幅（百分比，正数）")
    parser.add_argument("--announce-window", type=int, default=7, help="公告 T-N~T+1 窗口大小（默认 7）")
    parser.add_argument("--qa-window", type=int, default=30, help="互动问答 T-N~T+1 窗口大小（默认 30）")
    parser.add_argument("--price-window", type=int, default=5, help="量价 T-N~T+1 窗口（默认 5）")
    parser.add_argument("--alphavault-root", default="", help="AlphaVault 根目录；缺省读 ALPHAVAULT_ROOT")
    parser.add_argument("--registry-pages", action="append", default=[], help="按候选页面过滤来源登记，可重复")
    parser.add_argument("--registry-keywords", action="append", default=[], help="按关键词过滤来源登记，可重复")
    parser.add_argument("--skip-external", action="store_true", help="跳过 sibling 公告/互动问答抓取")
    parser.add_argument("--skip-cninfo", action="store_true")
    parser.add_argument("--skip-qa", action="store_true")
    parser.add_argument("--skip-alphavault", action="store_true")
    parser.add_argument("--output", required=True, help="Pack JSON 输出路径")
    parser.add_argument("--outputs-dir", default="", help="子产物目录，默认与 output 同目录下 _stage/")
    return parser.parse_args()


def stage_dir(args: argparse.Namespace) -> Path:
    if args.outputs_dir:
        d = Path(args.outputs_dir)
    else:
        d = Path(args.output).parent / "_stage"
    d.mkdir(parents=True, exist_ok=True)
    return d


def shift_date(value: str, days: int) -> str:
    dt = datetime.strptime(value, "%Y-%m-%d") + timedelta(days=days)
    return dt.strftime("%Y-%m-%d")


def run_script(cmd: list[str], stage: Path, label: str) -> dict[str, Any]:
    log_path = stage / f"{label}.log"
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True, check=True)
        log_path.write_text(completed.stderr or "", encoding="utf-8")
        return {"status": "ok", "log": str(log_path)}
    except subprocess.CalledProcessError as exc:
        log_path.write_text((exc.stderr or "") + "\n" + (exc.stdout or ""), encoding="utf-8")
        return {"status": "failed", "log": str(log_path), "returncode": exc.returncode}
    except FileNotFoundError as exc:
        log_path.write_text(str(exc), encoding="utf-8")
        return {"status": "missing", "log": str(log_path)}


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def main() -> int:
    args = parse_args()
    stage = stage_dir(args)

    pack: dict[str, Any] = {
        "pack_type": "Evidence Pack",
        "pack_subtype": "AttributionEvidence",
        "task_id": f"rise-attr-{args.ts_code}-{args.trade_date}",
        "created_at": datetime.utcnow().isoformat() + "Z",
        "producer": "chstock-rise-attribution",
        "write_permission": False,
        "input_sources": {},
        "source_paths": [],
        "source_urls": [],
        "related_pages": [],
        "data_points": {
            "name": args.name,
            "ts_code": args.ts_code,
            "trade_date": args.trade_date,
            "pct_chg_reported": args.pct_chg,
        },
        "claims": [],
        "analysis": {
            "instruction": (
                "本字段由模型填充。按 SKILL.md 七大分类逐项判断，给出 主因/次因/辅助/推测/未解释 分档与反例排查。"
            ),
        },
        "conflicts": [],
        "confidence": None,
        "recommended_action": None,
        "raw_storage_path": None,
        "subtasks": [],
        "gaps": [],
    }

    # 1. price snapshot
    price_path = stage / "price_snapshot.json"
    price_cmd = [
        sys.executable,
        str(SCRIPT_DIR / "price_window_snapshot.py"),
        "--ts-code", args.ts_code,
        "--trade-date", args.trade_date,
        "--window", str(args.price_window),
        "--output", str(price_path),
    ]
    pack["subtasks"].append({"step": "price_window_snapshot", **run_script(price_cmd, stage, "price")})
    price_payload = load_json(price_path)
    if price_payload:
        pack["data_points"]["price_snapshot"] = price_payload
        pack["data_points"]["industry"] = price_payload.get("industry")
        pack["data_points"]["limit_up_flag"] = price_payload.get("limit_up_flag")
        pack["data_points"]["industry_peers_on_date"] = price_payload.get("industry_peers_on_date")
    else:
        pack["gaps"].append("price_window_snapshot.py 未产出有效 JSON")

    # 2. AlphaVault registry lookup
    if not args.skip_alphavault:
        av_path = stage / "alphavault_registry.json"
        av_cmd = [
            sys.executable,
            str(SCRIPT_DIR / "query_alphavault_raw.py"),
            "--alphavault-root", args.alphavault_root,
            "--date-from", shift_date(args.trade_date, -args.announce_window),
            "--date-to", shift_date(args.trade_date, +1),
            "--limit", "50",
            "--output", str(av_path),
        ]
        if args.registry_pages:
            for p in args.registry_pages:
                av_cmd += ["--page", p]
        else:
            # default page guess: any wiki/01-个股词条/<name>.md
            av_cmd += ["--page", f"wiki/01-个股词条/{args.name}"]
        for kw in args.registry_keywords or [args.name, args.ts_code.split(".")[0]]:
            av_cmd += ["--keyword", kw]
        pack["subtasks"].append({"step": "query_alphavault_raw", **run_script(av_cmd, stage, "alphavault")})
        av_payload = load_json(av_path)
        if av_payload:
            pack["input_sources"]["alphavault_registry"] = av_payload
            if av_payload.get("skipped") == "alphavault_root_missing":
                pack["gaps"].append("AlphaVault 库内回溯跳过（ALPHAVAULT_ROOT 缺失）")
            else:
                for src in av_payload.get("sources", []):
                    if src.get("文件路径"):
                        pack["source_paths"].append(src["文件路径"])
                    for page in src.get("更新页面") or []:
                        if page not in pack["related_pages"]:
                            pack["related_pages"].append(page)
        else:
            pack["gaps"].append("query_alphavault_raw.py 未产出有效 JSON")
    else:
        pack["gaps"].append("AlphaVault 库内回溯按 --skip-alphavault 跳过")

    # 3. sibling cninfo announcement
    if not args.skip_external and not args.skip_cninfo:
        cn_path = stage / "announcements.json"
        stock_code = args.ts_code.split(".")[0]
        cn_cmd = [
            sys.executable,
            str(SIBLING_CNINFO),
            "--tabtype", "fulltext",
            "--date", f"{shift_date(args.trade_date, -args.announce_window)}~{shift_date(args.trade_date, +1)}",
            "--stock", stock_code,
            "--output", str(cn_path),
        ]
        pack["subtasks"].append({"step": "cninfo_announcement_search", **run_script(cn_cmd, stage, "cninfo")})
        cn_payload = load_json(cn_path)
        if cn_payload:
            pack["input_sources"]["announcements"] = cn_payload
            for ann in cn_payload.get("announcements", []) or []:
                url = ann.get("adjunct_url") or ann.get("announcementURL")
                if url and url not in pack["source_urls"]:
                    pack["source_urls"].append(url)

    # 4. sibling interactive QA
    if not args.skip_external and not args.skip_qa:
        qa_path = stage / "interactive_qa.json"
        qa_cmd = [
            sys.executable,
            str(SIBLING_QA),
            "--company", args.name,
            "--date-from", shift_date(args.trade_date, -args.qa_window),
            "--date-to", shift_date(args.trade_date, +1),
            "--limit", "30",
            "--output", str(qa_path),
        ]
        pack["subtasks"].append({"step": "interactive_qa_search", **run_script(qa_cmd, stage, "qa")})
        qa_payload = load_json(qa_path)
        if qa_payload:
            pack["input_sources"]["interactive_qa"] = qa_payload
            for item in qa_payload.get("items", []) or []:
                url = item.get("url") or item.get("question_url")
                if url and url not in pack["source_urls"]:
                    pack["source_urls"].append(url)

    # write final pack
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(pack, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    summary = {
        "pack_path": str(out),
        "stage_dir": str(stage),
        "subtasks": [
            {"step": t["step"], "status": t.get("status")} for t in pack["subtasks"]
        ],
        "gaps": pack["gaps"],
        "source_paths_count": len(pack["source_paths"]),
        "source_urls_count": len(pack["source_urls"]),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
