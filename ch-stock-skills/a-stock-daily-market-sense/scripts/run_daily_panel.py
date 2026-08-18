#!/usr/bin/env python3
"""
Stable runner for daily A-share market evidence generation.

This wrapper avoids Windows shell redirection issues by calling the panel
builder directly, clearing broken local proxy variables, and writing
both the full evidence pack and a compact report_context JSON.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import market_panel


SCRIPT_ROOT = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_ROOT.parent
DEFAULT_REPORTS_DIR = SKILL_ROOT / "reports"


def clear_proxy_env(env: dict) -> dict:
    cleaned = env.copy()
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        cleaned.pop(key, None)
    cleaned["PYTHONIOENCODING"] = "utf-8"
    return cleaned


def sibling_stderr_path(evidence_path: Path) -> Path:
    return evidence_path.with_name(f"{evidence_path.stem}.stderr.log")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run market_panel.py safely and write UTF-8 evidence/context files."
    )
    parser.add_argument(
        "--cleanup",
        default=None,
        metavar="YYYYMMDD",
        help="Delete intermediate artifacts (evidence/kline/stderr/context/module_context) for the date, keep report md/html, then exit.",
    )
    parser.add_argument("--asof", default=None, help="Analysis date, YYYYMMDD or YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--offset", type=int, default=0, help="Trading-day offset from asof.")
    parser.add_argument("--allow-future", action="store_true", help="Allow positive offset for post-hoc checks.")
    parser.add_argument("--lookback", type=int, default=120, help="Number of trading days to load.")
    parser.add_argument("--index", default="000300.SH", help="Benchmark index ts_code.")
    parser.add_argument("--sample-limit", type=int, default=40, help="Generic candidate sample limit.")
    parser.add_argument("--market-trend-days", type=int, default=90, help="Market-trend window.")
    parser.add_argument("--index-kline-days", type=int, default=market_panel.DEFAULT_INDEX_KLINE_DAYS, help="Index candlestick window for HTML output.")
    parser.add_argument("--sleep", type=float, default=0.12, help="Sleep seconds between uncached API calls.")
    parser.add_argument("--fetch-workers", type=int, default=6, help="Max worker threads for cache/API fetching.")
    parser.add_argument("--no-cache", action="store_true", help="Disable cache in market_panel.py.")
    parser.add_argument("--refresh-cache", action="store_true", help="Force refetch and overwrite cache.")
    parser.add_argument("--with-limit", action="store_true", help="Fetch official limit_list_d stats.")
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR), help="Directory for generated files.")
    parser.add_argument("--evidence-out", default=None, help="Full evidence JSON output path.")
    parser.add_argument("--context-out", default=None, help="Legacy compact report_context JSON path; written only when set.")
    parser.add_argument("--module-context-dir", default=None, help="Directory for module-level subagent JSON files.")
    parser.add_argument("--no-module-context", action="store_true", help="Skip module-level subagent JSON files.")
    parser.add_argument("--no-extreme-state", action="store_true", help="Skip the extreme_state card (bottom washout score / top crowding score).")
    parser.add_argument("--extreme-backfill", type=int, default=0, help="Backfill N trading days of extreme-state metrics before scoring (first run: 300).")
    parser.add_argument("--stderr-out", default=None, help="Stderr log output path.")
    parser.add_argument("--money-context-limit", type=int, default=80, help="Money-effect rows in context.")
    parser.add_argument("--decline-context-limit", type=int, default=20, help="Volume-decline rows in context.")
    parser.add_argument("--feature-context-limit", type=int, default=20, help="Module 5 feature-group rows per subgroup in context.")
    parser.add_argument("--amount-context-limit", type=int, default=20, help="Top-amount rows in context.")
    return parser


def build_panel_argv(args: argparse.Namespace, extra_args: List[str]) -> List[str]:
    argv = [
        "panel",
        "--lookback",
        str(args.lookback),
        "--index",
        args.index,
        "--sample-limit",
        str(args.sample_limit),
        "--market-trend-days",
        str(args.market_trend_days),
        "--index-kline-days",
        str(args.index_kline_days),
        "--sleep",
        str(args.sleep),
        "--fetch-workers",
        str(args.fetch_workers),
        "--offset",
        str(args.offset),
    ]
    if args.asof:
        argv.extend(["--asof", args.asof])
    if args.allow_future:
        argv.append("--allow-future")
    if args.no_cache:
        argv.append("--no-cache")
    if args.refresh_cache:
        argv.append("--refresh-cache")
    if args.with_limit:
        argv.append("--with-limit")
    argv.extend(extra_args)
    return argv


def default_stem(asof: Optional[str]) -> str:
    date = market_panel.normalize_date(asof) if asof else datetime.now().strftime("%Y%m%d")
    return f"evidence_{date}_utf8"


def refresh_and_verify_market_chart_data(resolved_date: str) -> Path:
    """Rebuild the HTML chart derivative from PostgreSQL and gate its date.

    The market-history update can legitimately be a no-op when the requested
    row is already present.  The chart JSON is still a derived artifact and
    therefore must be refreshed independently on every report run.
    """
    # 窗口锚在报告日：回溯历史日报时若锚在全表末尾，目标日不在记录里会被门禁拦下
    chart_path = market_panel.write_market_history_json(end_date=resolved_date)
    payload = json.loads(chart_path.read_text(encoding="utf-8"))
    records = payload.get("records") or []
    record_dates = {
        str(row.get("trade_date") or "")
        for row in records
        if isinstance(row, dict)
    }
    window_end = str((payload.get("metadata") or {}).get("window_end") or "")
    if resolved_date not in record_dates:
        raise RuntimeError(
            "market chart data freshness gate failed: "
            f"required trade_date={resolved_date}, window_end={window_end or 'missing'}, "
            f"path={chart_path}"
        )
    return chart_path


@contextlib.contextmanager
def patched_environment(env: Dict[str, str]):
    original = os.environ.copy()
    try:
        os.environ.clear()
        os.environ.update(env)
        yield
    finally:
        os.environ.clear()
        os.environ.update(original)


def build_trend_state_card(resolved_date: str) -> Dict[str, Any]:
    """趋势轴状态卡（PG market_history 五计数器 + 六档状态机），失败不阻断研报。"""
    try:
        import trend_state_card

        return trend_state_card.build_block(resolved_date)
    except Exception as exc:
        return {"available": False, "reason": f"trend_state_card failed: {exc}"}


def build_extreme_state_card(resolved_date: str, backfill: int = 0) -> Dict[str, Any]:
    """极值状态卡（底部出清分 + 顶部拥挤分），失败不阻断研报。"""
    try:
        import extreme_state_card

        return extreme_state_card.build_block(resolved_date, backfill=backfill)
    except Exception as exc:
        return {"available": False, "reason": f"extreme_state_card failed: {exc}"}


def write_module_contexts(
    evidence: dict,
    module_dir: Path,
    trend_card: Optional[Dict[str, Any]] = None,
    extreme_state: Optional[Dict[str, Any]] = None,
) -> None:
    module_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in market_panel.build_module_contexts(evidence).items():
        if name == "module1_market_trend":
            if trend_card is not None:
                payload["trend_state_card"] = trend_card
            if extreme_state is not None:
                payload["extreme_state"] = extreme_state
        (module_dir / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def cleanup_intermediates(reports_dir: Path, date: str) -> Dict[str, Any]:
    """Deterministically remove intermediate artifacts for one date.

    Final deliverables (report_YYYYMMDD.md / .html) are never touched.
    """
    date = market_panel.normalize_date(date)
    removed: List[str] = []
    missing: List[str] = []
    candidates = [
        reports_dir / f"evidence_{date}_utf8.json",
        reports_dir / f"evidence_{date}_utf8.stderr.log",
        reports_dir / f"kline_{date}.json",
        reports_dir / f"report_context_{date}.json",
        reports_dir / f"lifecycle_{date}.json",
        # Backward-compatible cleanup for artifacts produced before the
        # strategy-pick / factor-lab lifecycle was removed.  Current runs no
        # longer create these files, but upgrades should not strand old
        # intermediates in reports/.
        reports_dir / f"strategy_picks_{date}.json",
        reports_dir / f"calibration_{date}.json",
        reports_dir / f"profile_refresh_{date}.json",
        reports_dir / f"weekly_factor_pack_{date}.json",
    ]
    for path in candidates:
        if path.exists():
            path.unlink()
            removed.append(str(path))
        else:
            missing.append(str(path))
    # 因子挖掘决策包 + 明细（研究线临时产物，按组名 × 该日期成对清理）
    for extra in sorted(reports_dir.glob(f"factor_mining_*_{date}*.json")):
        extra.unlink()
        removed.append(str(extra))
    module_dir = reports_dir / f"module_context_{date}"
    if module_dir.is_dir():
        removed.extend(
            str(child)
            for child in sorted(module_dir.rglob("*"))
            if child.is_file() or child.is_symlink()
        )
        shutil.rmtree(module_dir)
        removed.append(str(module_dir))
    else:
        missing.append(str(module_dir))
    return {
        "date": date,
        "removed": removed,
        "not_found": missing,
        "kept": [
            str(reports_dir / f"report_{date}.md"),
            str(reports_dir / f"report_{date}.html"),
        ],
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args, extra_args = parser.parse_known_args(argv)

    reports_dir = Path(args.reports_dir)

    if args.cleanup:
        result = cleanup_intermediates(reports_dir, args.cleanup)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    reports_dir.mkdir(parents=True, exist_ok=True)

    preliminary_evidence = Path(args.evidence_out) if args.evidence_out else reports_dir / f"{default_stem(args.asof)}.json"
    stderr_path = Path(args.stderr_out) if args.stderr_out else sibling_stderr_path(preliminary_evidence)

    env = clear_proxy_env(os.environ)
    panel_argv = build_panel_argv(args, extra_args)
    stderr_buffer = io.StringIO()
    try:
        panel_args = market_panel.build_arg_parser().parse_args(panel_argv)
        with patched_environment(env), contextlib.redirect_stderr(stderr_buffer):
            evidence = market_panel.build_panel(panel_args)
    finally:
        stderr_path.write_text(stderr_buffer.getvalue(), encoding="utf-8")

    resolved_date = evidence.get("metadata", {}).get("resolved_trade_date") or market_panel.normalize_date(args.asof)
    # 极值轴与趋势轴正交：前者抓两端拐点（快变量），后者描述所处阶段（慢变量）
    extreme_state: Optional[Dict[str, Any]] = None
    if not args.no_extreme_state:
        extreme_state = build_extreme_state_card(resolved_date, args.extreme_backfill)
        evidence["extreme_state"] = extreme_state
    # 趋势卡也进 evidence：HTML 的 20 日档位图要从 evidence 读轨迹，而模块级 JSON
    # 只给模型看、渲染器不读它
    trend_card: Optional[Dict[str, Any]] = None
    if not args.no_module_context:
        trend_card = build_trend_state_card(resolved_date)
        evidence["trend_state_card"] = trend_card
    market_chart_path = refresh_and_verify_market_chart_data(resolved_date)
    evidence_path = Path(args.evidence_out) if args.evidence_out else reports_dir / f"evidence_{resolved_date}_utf8.json"
    if args.stderr_out is None:
        stderr_path = sibling_stderr_path(evidence_path)
        stderr_path.write_text(stderr_buffer.getvalue(), encoding="utf-8")

    # stock_kline_records is display-layer data for the HTML renderer only
    # (historically ~half the evidence size); it ships as a sibling file so
    # the evidence pack the model reads stays lean.
    kline_payload = evidence.pop("stock_kline_records", None)
    kline_path = reports_dir / f"kline_{resolved_date}.json"
    if kline_payload is not None:
        kline_path.write_text(json.dumps(kline_payload, ensure_ascii=False), encoding="utf-8")

    evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")

    context_path = None
    if args.context_out:
        # Legacy compact context, emitted only on explicit request.
        report_context = market_panel.build_report_context(
            evidence,
            money_limit=args.money_context_limit,
            decline_limit=args.decline_context_limit,
            feature_limit=args.feature_context_limit,
            amount_limit=args.amount_context_limit,
        )
        context_path = Path(args.context_out)
        context_path.write_text(json.dumps(report_context, ensure_ascii=False, indent=2), encoding="utf-8")

    module_context_dir = None
    if not args.no_module_context:
        module_context_dir = Path(args.module_context_dir) if args.module_context_dir else reports_dir / f"module_context_{resolved_date}"
        write_module_contexts(evidence, module_context_dir, trend_card, extreme_state)

    print(json.dumps({
        "resolved_trade_date": resolved_date,
        "trend_state": (trend_card or {}).get("state") if (trend_card or {}).get("available") else (trend_card or {}).get("reason"),
        "extreme_state": (
            f"出清 {(extreme_state or {}).get('washout', {}).get('score')}/6"
            f"｜顶部 {(extreme_state or {}).get('top', {}).get('score')}/5"
            if (extreme_state or {}).get("available") else (extreme_state or {}).get("reason")
        ),
        "evidence": str(evidence_path),
        "stock_klines": str(kline_path) if kline_payload is not None else None,
        "report_context": str(context_path) if context_path else None,
        "module_context_dir": str(module_context_dir) if module_context_dir else None,
        "market_chart_data": str(market_chart_path),
        "stderr": str(stderr_path),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1)
