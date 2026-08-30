#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史回补 —— 能补的补满，补不了的说清楚为什么，而不是留一个空着的格子。

**只有基准层能回补。** 这不是懒，是源的形状决定的：

* **FRED（国债曲线 + IG/HY 指数 OAS）可以补。** fredgraph.csv 给的是完整序列，
  想要多久就有多久。这一层以前根本没落库——collect 取完在内存里算完利差就扔，
  metrics 每次重新打一次 FRED。于是判据 1 的市场 beta 依赖实时网络调用，
  锚点日的指数 OAS 也无处可查。
* **SPDR 价格层补不了。** 端点只有一个 `holdings-daily-us-en-{ticker}.xlsx`，
  永远返回当日快照。2026-08-30 实测：加 `?date=20260630` 拿回来的文件与不加
  参数的**字节完全相同**（sha1 b291bf2621a5，377065 字节），日期参数被忽略，
  没有归档路径。逐 CUSIP 的历史价格也没有免费源（见 sources.yaml 的
  not_available：TRACE 逐笔成交的 FINRA 固定收益 group 全部 404）。
  所以发行人层的 G-spread 序列**只能从开始采集那天往后长**。
* **派生层（drv.\\*）跟着价格层走。** 它是从当日价格算出来的，价格没有历史，
  派生量就没有历史。补基准层不会凭空造出梯级历史。

用法：
    python scripts/backfill.py                  # 回补 200 天基准层
    python scripts/backfill.py --days 400
    python scripts/backfill.py --dry-run        # 只看能补多少，不落库
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import db_adapter as db                                   # noqa: E402
from collectors import fred                               # noqa: E402
from collectors.base import load_config                   # noqa: E402

# 补不了的层：写在这里而不是散在注释里，因为回补摘要要把它们打出来——
# 「为什么梯级只有两天」这个问题不该每次都重新查一遍源。
NOT_BACKFILLABLE = [
    {"layer": "spdr 价格层（px.clean / yld.*）",
     "why": ("端点只返回当日持仓快照；`?date=` 参数被忽略（实测字节相同），"
             "无归档路径，逐 CUSIP 历史价格亦无免费源")},
    {"layer": "派生层（drv.*）",
     "why": "从当日价格算出，价格无历史则派生无历史；补基准层不会凭空造出梯级历史"},
    {"layer": "sec 基本面 / GPU 抵押 / SPV 台账",
     "why": "按申报时点披露，本身就不是日频序列；已有的历史点在库里"},
]


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def run(days: int = 200, dry_run: bool = False) -> Dict[str, Any]:
    """回补基准层。返回摘要，含补不了的层与原因。"""
    run_id = uuid.uuid4().hex[:12]
    sources_cfg = load_config("sources.yaml")
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    summary: Dict[str, Any] = {
        "run_id": run_id, "days": days, "since": since,
        "not_backfillable": NOT_BACKFILLABLE,
    }

    if not dry_run:
        db.init_schema()

    started = _now()
    t0 = time.monotonic()
    try:
        # 多要 30 天余量：FRED 序列开头几天可能全是 "."（假期/未发布），
        # 少要会让 200 天的窗口实际不足 200 天。
        bench = fred.fetch_benchmarks(sources_cfg, history_days=days + 30)
        rows: List[Dict[str, Any]] = fred.benchmark_rows(bench, since=since)
        status = "ok" if rows else "empty"
        curve_days = sorted({r["asof_date"] for r in rows
                             if r["instrument_key"] == fred.UST_KEY})
        index_days = sorted({r["asof_date"] for r in rows
                             if r["instrument_key"] == fred.INDEX_KEY})
        note = (f"国债曲线 {len(curve_days)} 天，指数 OAS {len(index_days)} 天，"
                f"覆盖 {curve_days[0] if curve_days else '—'} → "
                f"{curve_days[-1] if curve_days else '—'}")
    except Exception as exc:                              # noqa: BLE001
        rows, status = [], "failed"
        curve_days = index_days = []
        note = f"FRED 取不到：{exc}"

    written = 0
    if rows and not dry_run:
        written = db.upsert_observations(rows)

    summary["fred"] = {
        "status": status, "note": note,
        "rows": len(rows), "written": written,
        "curve_days": len(curve_days), "index_days": len(index_days),
        "first_day": curve_days[0] if curve_days else None,
        "last_day": curve_days[-1] if curve_days else None,
        "seconds": round(time.monotonic() - t0, 1),
    }

    if not dry_run:
        db.record_run({"run_id": run_id, "source_id": "fred_backfill",
                       "obs_date": curve_days[-1] if curve_days else None,
                       "started_at": started, "ended_at": _now(),
                       "status": status, "rows_written": written,
                       "basket_fingerprint": None,
                       "note": f"回补 {days} 天：{note}"})
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="回补 DC 信用监控的历史基准层。")
    parser.add_argument("--days", type=int, default=200,
                        help="回补多少个日历日（默认 200）")
    parser.add_argument("--dry-run", action="store_true", help="不落库")
    args = parser.parse_args()
    print(json.dumps(run(args.days, args.dry_run), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
