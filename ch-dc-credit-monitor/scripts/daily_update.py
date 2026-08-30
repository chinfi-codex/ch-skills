#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""给 cron 用的流水线：collect → metrics → 写 snapshot。

**不渲染 HTML，也不写报告。** 那两步需要模型的 verdict，而 cron 里没有模型。
渲染留给人（或模型）在读完证据之后手动跑，见 SKILL.md 的工作流程第 5–6 步。
这条边界是有意的：自动出图很容易变成自动出结论。

用法：
    python scripts/daily_update.py
    python scripts/daily_update.py --skip-collect     # 只重算指标
    python scripts/daily_update.py --no-sec           # 市场层单跑，快
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import collect as collect_mod       # noqa: E402
import metrics as metrics_mod       # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="DC 信用监控的每日流水线。")
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--no-sec", action="store_true")
    parser.add_argument("--window", type=int, default=90)
    args = parser.parse_args()

    out = {"started_at": dt.datetime.now(dt.timezone.utc).isoformat()}

    if not args.skip_collect:
        out["collect"] = collect_mod.run(with_sec=not args.no_sec, dry_run=False)
    else:
        out["collect"] = "skipped"

    evidence = metrics_mod.build(window_days=args.window)
    path = SKILL_ROOT / "evidence" / f"dc-{evidence['asof']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    snapshot = SKILL_ROOT / "evidence" / "latest.json"
    snapshot.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    out.update({
        "evidence": str(path),
        "snapshot": str(snapshot),
        "asof": evidence["asof"],
        "curve_lag_days": evidence["anchors"]["curve_lag_days"],
        "issuers": len(evidence["issuers"]),
        "events": len(evidence["events"]),
        "quality": evidence["quality_summary"],
        "next_step": ("读证据 → 按生成报告时的本地执行日写 "
                      "reports/dc-<report_date>.md（frontmatter 另记 "
                      f"data_asof: {evidence['asof']}）→ render_report_html.py "
                      "--evidence 当前证据 --input 那份报告"),
    })
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
