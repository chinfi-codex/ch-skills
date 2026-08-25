#!/usr/bin/env python3
"""每日流水线：采集 → 指标 → 快照。给 cron 用的单一入口。

PRD §6.1 的九步在这里落成三段：collect.py 吃掉 fetch/raw/normalize/validate/persist，
metrics.py 吃掉 metrics/alert 判定，本脚本写 snapshot 与 health log 汇总。
第 8 步「告警」只写库不推送——阈值尚未按真实波动率校准，见 config/thresholds.yaml。

刻意不在这里写报告：报告是模型的产物，脚本产出的是证据。

用法：
    python scripts/daily_update.py
    python scripts/daily_update.py --date 2026-08-25 --skip-collect
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict

SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent


def run(cmd: list) -> Dict[str, Any]:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return {"cmd": " ".join(cmd), "returncode": proc.returncode,
            "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-2000:]}


def main() -> int:
    parser = argparse.ArgumentParser(description="GPU 算力监控每日流水线")
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--window", type=int, default=90)
    parser.add_argument("--skip-collect", action="store_true")
    parser.add_argument("--sources", default=None)
    args = parser.parse_args()

    obs_date = args.date
    evidence_path = SKILL_ROOT / "evidence" / f"gpu-{obs_date}.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)

    steps = []
    if not args.skip_collect:
        cmd = [sys.executable, str(SCRIPT_DIR / "collect.py"), "--date", obs_date]
        if args.sources:
            cmd += ["--sources", args.sources]
        steps.append(run(cmd))

    steps.append(run([sys.executable, str(SCRIPT_DIR / "metrics.py"),
                      "--date", obs_date, "--window", str(args.window),
                      "--output", str(evidence_path)]))

    # 采集全挂时指标层拿不到当日数据，但不该因此让整条流水线静默成功
    metrics_ok = steps[-1]["returncode"] == 0 and evidence_path.exists()
    snapshot: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "obs_date": obs_date,
        "window_days": args.window,
        "evidence_path": str(evidence_path.relative_to(SKILL_ROOT)),
        "steps": steps,
        "ok": metrics_ok,
    }
    if metrics_ok:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        health = evidence.get("source_health") or []
        snapshot["source_health"] = [
            {k: h.get(k) for k in ("source", "status", "last_obs_date", "age_days", "fresh")}
            for h in health]
        snapshot["failed_sources"] = [h["source"] for h in health
                                      if h.get("status") not in ("ok", "empty")]
        snapshot["scores"] = {m: b["score"]["value"]
                              for m, b in (evidence.get("models") or {}).items()}
        snapshot["alerts"] = {m: [a["id"] for a in b.get("alerts") or []]
                              for m, b in (evidence.get("models") or {}).items()}
        token = evidence.get("token_market") or {}
        snapshot["token_market"] = {
            "usable": token.get("usable", False),
            "reason": token.get("reason"),
            "anchor_date": token.get("anchor_date"),
            "alignment_lag_days": token.get("alignment_lag_days"),
            "series_days": token.get("series_days"),
            "paid_tokens": (((token.get("volume") or {}).get("paid") or {})
                            .get("latest") or {}).get("value"),
            "nominal_spend_usd": (((token.get("spend") or {})
                                   .get("nominal_usd_per_day") or {})
                                  .get("latest") or {}).get("value"),
            "blended_usd_per_mtok": (((token.get("price") or {}).get("blended") or {})
                                     .get("latest") or {}).get("value"),
            "concentration_warning": ((token.get("concentration") or {})
                                      .get("concentration_warning")),
        }
        history = token.get("history") or {}
        snapshot["token_history"] = {
            "usable": history.get("usable", False),
            "reason": history.get("reason"),
            "weeks": history.get("weeks"),
            "first_week": history.get("first_week"),
            "last_week": history.get("last_week"),
            "volume_index_latest": ((history.get("volume_index") or {})
                                    .get("series") or [{}])[-1].get("value"),
            "structure_effect_latest": ((history.get("structure_effect_index") or {})
                                        .get("series") or [{}])[-1].get("value"),
        }

    out = SKILL_ROOT / "evidence" / "latest_snapshot.json"
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in snapshot.items() if k != "steps"},
                     ensure_ascii=False, indent=2))
    return 0 if metrics_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
