# -*- coding: utf-8 -*-
"""画像衰减重检：对每个策略画像用它的原挖矿口径（mining_spec）在**滚动到最新**的窗口
重跑，检验当初选定的叠加条件在新数据上是否还过闸。

窗口只加长不平移（起点 = 画像 window_start，终点 = 最新交易日），样本只增不减——把
「换了窗口」与「条件衰减」两件事分开，看到的变化才归因于新数据本身。

复用 factor_backtest.run_mining（与线上挖矿完全同一条统计路径）+ _combo_stats（同一护栏）。
脚本只出 then/now 对比与新窗口候选，**不判维持/降级/刷新**——那是模型读证据后的判断，
改画像、bump 版本由人确认。输出 reports/profile_refresh_<asof>.json。
"""
from __future__ import annotations

import json
import sys
import tempfile
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import factor_backtest as fb  # noqa: E402
from returns_core import _r  # noqa: E402

SKILL_ROOT = HERE.parent
PROFILES_DIR = SKILL_ROOT / "references" / "strategy_profiles"

# 挖矿 CLI 默认值（镜像 factor_backtest.main 的 argparse），refresh 只覆盖必要项。
_DEFAULTS = dict(
    group="discount_relaunch", spec=None, warmup=120, asof=None, start=None, end=None,
    entry="close", horizon=5, min_n=20, fetch_workers=4, skip_backfill=True,
    refresh_basic=False, market_cap=80.0, amount=5.0, pct=7.0, discount_min=0.6,
    discount_max=0.85, contraction_max=0.9, expansion_min=2.0, lookback=200,
    low_recency=5, out=None,
)


def _parse_target_cell(cell: Optional[str]) -> tuple:
    """'close_T+5' → ('close', 5)；缺省 close/5。"""
    if not cell or "_T+" not in cell:
        return "close", 5
    entry, hz = cell.split("_T+")
    entry = entry if entry in ("open", "close") else "close"
    try:
        return entry, int(hz)
    except ValueError:
        return entry, 5


def _args_for(profile: Dict[str, Any], tmp_specs: List[Path]) -> Optional[Namespace]:
    """据画像 mining_spec 构造 run_mining 的 args；无 mining_spec 返回 None。"""
    spec = profile.get("mining_spec")
    if not spec:
        return None
    entry, horizon = _parse_target_cell(profile.get("target_cell"))
    win_start = str(profile.get("window_start", "")).replace("-", "") or None
    a = dict(_DEFAULTS)
    a.update(entry=entry, horizon=horizon, start=win_start, end=None,  # end=None → 最新交易日
             skip_backfill=True, min_n=int(spec.get("min_n", _DEFAULTS["min_n"])))
    if spec.get("kind") == "builtin":
        a["group"] = spec.get("group", profile.get("profile_id"))
    elif spec.get("kind") == "spec":
        # replay_custom 从文件读 spec，这里把内联 spec 落临时文件。
        tf = Path(tempfile.mkstemp(suffix=".json", prefix="refresh_spec_")[1])
        tf.write_text(json.dumps(spec.get("spec") or {}, ensure_ascii=False), encoding="utf-8")
        tmp_specs.append(tf)
        a["group"] = "custom"
        a["spec"] = str(tf)
        a["warmup"] = int((spec.get("spec") or {}).get("warmup", _DEFAULTS["warmup"]))
    else:
        return None
    return Namespace(**a)


def _now_stats(sig_df: pd.DataFrame, atoms: List[Dict[str, Any]], obj_col: str, abs_col: str,
               min_n: int, base_obj: Optional[float], split: Optional[str]) -> Dict[str, Any]:
    """在新窗口的信号集上重算一条画像条件（其 all 原子 AND）的表现。

    用真实 min_n 调 _combo_stats（同护栏）；若样本塌到 min_n 以下返回 collapsed 记录。
    """
    mask = pd.Series(True, index=sig_df.index)
    for atom in atoms:
        fac, op, thr = atom.get("factor"), atom.get("op"), atom.get("threshold")
        if fac not in sig_df.columns:
            return {"evaluated": False, "reason": f"缺因子 {fac}"}
        mask &= fb._apply_cond(sig_df[fac], op, thr)
    rec = fb._combo_stats(sig_df[mask], "cond", "profile", obj_col, abs_col, min_n, base_obj, split)
    if rec is None:
        n = int(sig_df[mask][obj_col].notna().sum()) if obj_col in sig_df else int(mask.sum())
        return {"evaluated": True, "passes_guardrails": False, "n_retained": n,
                "note": f"样本塌到 min_n({min_n}) 以下，条件在新窗口失去有效样本"}
    return {"evaluated": True, "n_retained": rec["n_retained"], "obj_mean": rec["obj_mean"],
            "delta_vs_base": rec["delta_vs_base"], "win": rec["win"],
            "oos_first_mean": rec["oos_first_mean"], "oos_second_mean": rec["oos_second_mean"],
            "oos_balance": rec["oos_balance"], "passes_guardrails": rec["passes_guardrails"]}


def _top_new_candidates(evidence: Dict[str, Any], limit: int = 5) -> List[Dict[str, Any]]:
    """新窗口里过闸的叠加候选 top-N（可能出现比旧条件更稳的新解）。"""
    pool = [c for c in (evidence.get("overlay_singles") or []) if c.get("passes_guardrails")]
    pool += [c for c in (evidence.get("overlay_pairs") or []) if c.get("passes_guardrails")]
    pool.sort(key=lambda c: (c.get("oos_balance") or 0, c.get("delta_vs_base") or 0), reverse=True)
    return [{"condition": c["condition"], "n_retained": c["n_retained"],
             "delta_vs_base": c["delta_vs_base"], "win": c.get("win"),
             "oos_balance": c["oos_balance"]} for c in pool[:limit]]


def build_refresh(profiles_dir: Path = PROFILES_DIR) -> Dict[str, Any]:
    tmp_specs: List[Path] = []
    profiles_out: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    latest_asof = None
    try:
        for pf in sorted(Path(profiles_dir).glob("*.json")):
            profile = json.loads(pf.read_text(encoding="utf-8"))
            pid = profile.get("profile_id", pf.stem)
            args = _args_for(profile, tmp_specs)
            if args is None:
                skipped.append({"profile_id": pid, "reason": "无 mining_spec，refresh 跳过（命中只作纯技术观察）"})
                continue
            try:
                res = fb.run_mining(args)
            except SystemExit as exc:
                skipped.append({"profile_id": pid, "reason": f"重跑失败：{exc}"})
                continue
            evidence = res["evidence"]
            asof = res["asof"]
            latest_asof = max(latest_asof, asof) if latest_asof else asof
            entry, horizon = _parse_target_cell(profile.get("target_cell"))
            obj_col = ("relc" if entry == "close" else "relo") + f"_{horizon}"
            abs_col = ("rc" if entry == "close" else "ro") + f"_{horizon}"
            if res["empty"]:
                profiles_out.append({"profile_id": pid, "n_signals_now": 0,
                                     "note": "新窗口无命中，无法重检"})
                continue
            sig_df = res["sig_df"]
            base_obj = evidence["base_cells"].get(f"{entry}_T+{horizon}", {}).get("rel_mean")
            dates = sorted(sig_df["signal_date"].dropna().unique().tolist())
            split = dates[len(dates) // 2] if dates else None
            min_n = int((profile.get("mining_spec") or {}).get("min_n", 20))

            cond_cmp = []
            for cond in profile.get("selected_conditions") or []:
                now = _now_stats(sig_df, cond.get("all") or [], obj_col, abs_col, min_n, base_obj, split)
                cond_cmp.append({
                    "condition_id": cond.get("condition_id"),
                    "label": cond.get("label"),
                    "then": {"n": cond.get("n"), "win": cond.get("win"), "rel_mean": cond.get("rel_mean"),
                             "delta": cond.get("delta"), "oos_balance": cond.get("oos_balance"),
                             "robustness": cond.get("robustness"), "passes_guardrails": cond.get("passes_guardrails")},
                    "now": now,
                })
            profiles_out.append({
                "profile_id": pid,
                "profile_version": profile.get("profile_version"),
                "target_cell": profile.get("target_cell"),
                "window_then": [profile.get("window_start"), profile.get("window_end")],
                "window_now": [evidence["meta"].get("signal_day_window", [None, None])[0], asof],
                "n_signals_then": (profile.get("base") or {}).get("n"),
                "n_signals_now": res["evidence"]["signals_summary"].get("total"),
                "base_rel_mean_now": _r(base_obj),
                "conditions": cond_cmp,
                "new_window_top_candidates": _top_new_candidates(evidence),
            })
    finally:
        for tf in tmp_specs:
            try:
                tf.unlink()
            except OSError:
                pass

    return {
        "meta": {
            "asof": latest_asof,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "what": "画像旧条件在滚动到最新窗口的 then/now 对比（窗口只加长不平移）",
            "guardrail": f"过闸同挖矿：n≥min_n、前后半区各≥{fb.HALF_MIN}且均正、Δ>0、弱/强半区比≥{fb.BALANCE_MIN}",
            "caveats": [
                "窗口只加长，then/now 差异来自新增数据；样本仍属同一大 regime，跨周期才更可信。",
                "now 用画像原 min_n；条件塌到 min_n 以下标 collapsed，是最直白的衰减信号。",
                "脚本只出对比，维持/降级/刷新由模型判、改画像由人确认（见 factor_lab.md）。",
            ],
        },
        "profiles": profiles_out,
        "skipped": skipped,
    }
