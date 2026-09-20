#!/usr/bin/env python3
"""宏观风险记分卡（确定性阈值判断，无 LLM）。

输入宏观行情 JSON（默认取 chstock-macro-monitor 的 `macro_monitor.py market` 输出，
也可传任意含 BRENT/WTI/US_TREASURY_10Y/USD_CNY/BTC 等键的 JSON），输出命中项与风险等级。

设计约束（AGENTS.md：手脑分离）：
- 本脚本只做确定性阈值命中判断与计数，不解释宏观含义、不给仓位结论。
- 风险等级如何影响仓位档位，由模型依据 references/methodology/position_matrix.md 判定。

用法：
  python3 scripts/macro_risk_scorecard.py --asof 20260918
  python3 scripts/macro_risk_scorecard.py --input /tmp/macro_out.json --asof 20260918
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SKILL_ROOT = Path(__file__).resolve().parent.parent
MACRO_MONITOR = (
    Path.home() / ".zcode" / "skills" / "chstock-macro-monitor" / "scripts" / "macro_monitor.py"
)

# 阈值全部为确定性地缘/流动性冲击信号，宁缺不伪造：
# 单点绝对水平 + 可计算时的变化幅都给出，缺变化序列时只按绝对水平命中并在 note 说明。
DEFAULT_RULES = [
    {
        "key": "brent_level",
        "label": "Brent 原油绝对高位（供给/地缘冲击报价器）",
        "field": "BRENT",
        "op": ">=",
        "threshold": 100.0,
        "severity": "high",
    },
    {
        "key": "wti_level",
        "label": "WTI 原油绝对高位",
        "field": "WTI",
        "op": ">=",
        "threshold": 95.0,
        "severity": "medium",
    },
    {
        "key": "us10y_level",
        "label": "美债 10Y 收益率绝对高位（折现率压力）",
        "field": "US_TREASURY_10Y",
        "op": ">=",
        "threshold": 4.5,
        "severity": "high",
    },
    {
        "key": "usdcny_weak",
        "label": "USD/CNY 走弱至弱方区间（汇率/流动性压力）",
        "field": "USD_CNY",
        "op": ">=",
        "threshold": 7.2,
        "severity": "medium",
    },
]

SEVERITY_WEIGHT = {"high": 2, "medium": 1, "low": 1}


def _load_macro(input_path: Optional[str]) -> Dict[str, Any]:
    """返回 {sources, data}；优先用传入文件，否则现拉 macro_monitor。"""
    if input_path:
        payload = json.loads(Path(input_path).read_text(encoding="utf-8"))
    else:
        if not MACRO_MONITOR.exists():
            return {"sources": {"macro_monitor": "MISSING_SCRIPT"}, "data": {}}
        proc = subprocess.run(
            [sys.executable, str(MACRO_MONITOR), "market"],
            capture_output=True, text=True, timeout=180,
        )
        if proc.returncode != 0:
            return {"sources": {"macro_monitor": f"ERROR exit={proc.returncode}"}, "data": {}}
        payload = json.loads(proc.stdout)
    return {
        "sources": payload.get("sources", {}),
        "data": payload.get("data", {}),
    }


def _to_float(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def build_scorecard(asof: str, input_path: Optional[str]) -> Dict[str, Any]:
    macro = _load_macro(input_path)
    data = macro["data"]
    sources = macro["sources"]

    hits: List[Dict[str, Any]] = []
    evaluated = 0
    for rule in DEFAULT_RULES:
        raw = data.get(rule["field"])
        val = _to_float(raw)
        if val is None:
            hits.append({
                "key": rule["key"], "label": rule["label"], "field": rule["field"],
                "value": None, "threshold": rule["threshold"], "hit": False,
                "severity": rule["severity"], "note": "数据缺失，按未命中处理",
            })
            continue
        evaluated += 1
        if rule["op"] == ">=":
            hit = val >= rule["threshold"]
        elif rule["op"] == "<=":
            hit = val <= rule["threshold"]
        else:
            hit = False
        hits.append({
            "key": rule["key"], "label": rule["label"], "field": rule["field"],
            "value": round(val, 4), "threshold": rule["threshold"], "hit": hit,
            "severity": rule["severity"],
        })

    fired = [h for h in hits if h["hit"]]
    score = sum(SEVERITY_WEIGHT.get(h["severity"], 1) for h in fired)
    # 等级只描述风险计分，不描述仓位动作（动作由 position_matrix 判）。
    if score == 0:
        level = "低"
    elif score <= 2:
        level = "中"
    else:
        level = "高"

    return {
        "available": evaluated > 0,
        "asof": asof,
        "data_sources": sources,
        "data_caveat": (
            "宏观行情为抓取时点最新值；跨日时可能不是 asof 当日收盘快照，"
            "用于结构演示与风险标记，不用于精确历史回测。"
        ),
        "rules_evaluated": evaluated,
        "score": score,
        "risk_level": level,
        "fired_count": len(fired),
        "fired_keys": [h["key"] for h in fired],
        "hits": hits,
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="宏观风险记分卡（确定性阈值判断）")
    parser.add_argument("--asof", required=True, help="分析日期 YYYYMMDD 或 YYYY-MM-DD")
    parser.add_argument("--input", default=None, help="宏观 JSON 文件；缺省时现拉 macro_monitor")
    parser.add_argument("--output", default=None, help="可选：把结果写入该 JSON 文件")
    args = parser.parse_args(argv)

    card = build_scorecard(args.asof, args.input)
    text = json.dumps(card, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
