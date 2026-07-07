# -*- coding: utf-8 -*-
"""校准对账：回测画像「承诺」 vs 实盘台账「真实样本外战绩」。

这是验证系统的心脏。挖矿画像说某条件 win 58%/rel_mean 1.8，实盘台账里带着这条件选出去的票
真正跑成什么样——两者的差就是过拟合 + 环境漂移的直接测量。

口径（与 strategy_picks.ledger_oos_stats、returns_core 完全一致，不另造尺）：
  - realized = 台账 `h{k}_status='scored'` 行的 relc_k（T+1 尾盘进场、相对匹配基准）。
  - win = relc_k > 0 占比；同看 mean 与 median（均值易被少数大赢家抬高）。
  - promise = **落库时** backtest 快照（matched_conditions[].win/rel_mean、backtest_stats_snapshot.base），
    不是当前画像——画像可能已改版，快照才是"当时的承诺"。
  - gap = realized − promise。

三张对账表：按 condition_id / 按 conviction_tier / 按 group。每行带 n，scored_n<10 标
insufficient_sample。脚本只出差值，**不判"是否失准"**——那是模型读证据后的判断。
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
_BUNDLED_SHARED = HERE / "_shared"
_DEV_SHARED = HERE.parents[2] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))

from db_core import get_connection, placeholder, table_exists  # noqa: E402
import returns_core as rc  # noqa: E402

HK = (3, 5, 10)
MIN_SAMPLE = 10  # scored_n 低于此标 insufficient_sample


def _median(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def _realized_by_horizon(rows: List[dict]) -> Dict[str, Any]:
    """对一组台账行，算各 horizon 的 realized（scored 行的 relc_k）。"""
    out: Dict[str, Any] = {}
    for k in HK:
        rel_col, status_col = f"relc_{k}", f"h{k}_status"
        rels = [rc._f(r.get(rel_col)) for r in rows
                if r.get(status_col) == "scored" and rc._f(r.get(rel_col)) is not None]
        pending = sum(1 for r in rows if r.get(status_col) == "pending")
        if not rels:
            out[f"t{k}"] = {"n": len(rows), "scored_n": 0, "pending_n": pending}
            continue
        out[f"t{k}"] = {
            "n": len(rows),
            "scored_n": len(rels),
            "pending_n": pending,
            "rel_win_pct": round(sum(1 for x in rels if x > 0) / len(rels) * 100, 1),
            "rel_mean": round(sum(rels) / len(rels), 2),
            "rel_median": round(_median(rels), 2),
        }
    return out


def _best_matured_k(realized: Dict[str, Any]) -> Optional[int]:
    mats = [k for k in HK if realized.get(f"t{k}", {}).get("scored_n", 0) > 0]
    return max(mats) if mats else None


def _insufficient(realized: Dict[str, Any]) -> bool:
    k = _best_matured_k(realized)
    if k is None:
        return True
    return realized[f"t{k}"]["scored_n"] < MIN_SAMPLE


def _examples(rows: List[dict], k: int, n: int = 3) -> List[Dict[str, Any]]:
    """按 realized relc_k 取高/低例证（scored 行）。"""
    scored = [r for r in rows if r.get(f"h{k}_status") == "scored" and rc._f(r.get(f"relc_{k}")) is not None]
    if not scored:
        return []
    scored.sort(key=lambda r: rc._f(r.get(f"relc_{k}")), reverse=True)
    picks = scored if len(scored) <= n else [scored[0], scored[len(scored) // 2], scored[-1]]
    return [{
        "ts_code": r.get("ts_code"), "name": r.get("name"),
        "asof": str(r.get("asof")), "tier": r.get("conviction_tier"),
        f"relc_{k}": round(rc._f(r.get(f"relc_{k}")), 2),
    } for r in picks]


def _gap(realized: Dict[str, Any], promise_win: Optional[float], promise_rel: Optional[float]) -> Dict[str, Any]:
    """realized − promise，逐 horizon。promise 是标量（回测口径不分 horizon，画像 target_cell 单值）。"""
    gap: Dict[str, Any] = {}
    for k in HK:
        cell = realized.get(f"t{k}", {})
        if cell.get("scored_n", 0) == 0:
            continue
        g: Dict[str, Any] = {}
        if promise_win is not None and cell.get("rel_win_pct") is not None:
            g["gap_win"] = round(cell["rel_win_pct"] - promise_win, 1)
        if promise_rel is not None and cell.get("rel_mean") is not None:
            g["gap_rel_mean"] = round(cell["rel_mean"] - promise_rel, 2)
        if g:
            gap[f"t{k}"] = g
    return gap


def _fetch_ledger(asof_iso: Optional[str]) -> List[dict]:
    cols = ("asof, ts_code, name, board, conviction_tier, groups_hit, matched_conditions, "
            "backtest_stats_snapshot, "
            + ", ".join(f"relc_{k}, rc_{k}, h{k}_status" for k in HK))
    with get_connection() as conn:
        if not table_exists(conn, "strategy_pick_ledger"):
            return []
        cur = conn.cursor()
        if asof_iso:
            cur.execute(f"SELECT {cols} FROM strategy_pick_ledger WHERE asof <= {placeholder()}", (asof_iso,))
        else:
            cur.execute(f"SELECT {cols} FROM strategy_pick_ledger")
        field_names = [d[0] for d in cur.description]
        return [dict(zip(field_names, r)) for r in cur.fetchall()]


def build_calibration(asof_iso: Optional[str] = None) -> Dict[str, Any]:
    rows = _fetch_ledger(asof_iso)
    matured_horizons = sorted({f"t{k}" for k in HK for r in rows if r.get(f"h{k}_status") == "scored"})

    # ---- 按 condition_id ----
    by_cond_rows: Dict[str, List[dict]] = {}
    cond_meta: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        mc = r.get("matched_conditions") or []
        seen = set()
        for cond in mc:
            cid = cond.get("condition_id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            by_cond_rows.setdefault(cid, []).append(r)
            # promise 快照：取该条件在任一 pick 落库时的 win/rel_mean（画像属性，跨 pick 应一致）。
            m = cond_meta.setdefault(cid, {"label": cond.get("label"), "wins": [], "rels": [], "groups": set()})
            if cond.get("win") is not None:
                m["wins"].append(rc._f(cond.get("win")))
            if cond.get("rel_mean") is not None:
                m["rels"].append(rc._f(cond.get("rel_mean")))
            for g in (r.get("groups_hit") or []):
                m["groups"].add(g)

    by_condition = []
    not_yet_mature_conditions = []
    for cid, crows in sorted(by_cond_rows.items()):
        realized = _realized_by_horizon(crows)
        meta = cond_meta[cid]
        promise_win = _median(meta["wins"]) if meta["wins"] else None
        promise_rel = _median(meta["rels"]) if meta["rels"] else None
        best_k = _best_matured_k(realized)
        if best_k is None:
            not_yet_mature_conditions.append({"condition_id": cid, "label": meta["label"],
                                              "n_picks": len(crows)})
            continue
        by_condition.append({
            "condition_id": cid,
            "label": meta["label"],
            "groups": sorted(meta["groups"]),
            "promise": {"win": promise_win, "rel_mean": promise_rel,
                        "note": "落库时回测快照（画像 target_cell 单值，不分 horizon）"},
            "realized": realized,
            "gap": _gap(realized, promise_win, promise_rel),
            "insufficient_sample": _insufficient(realized),
            "examples": _examples(crows, best_k),
        })
    by_condition.sort(key=lambda c: c["insufficient_sample"])  # 有效样本的排前面

    # ---- 按 conviction_tier ----
    by_tier = []
    for tier in ("strong", "medium", "watch"):
        trows = [r for r in rows if r.get("conviction_tier") == tier]
        if not trows:
            continue
        realized = _realized_by_horizon(trows)
        best_k = _best_matured_k(realized)
        by_tier.append({
            "tier": tier,
            "n_picks": len(trows),
            "realized": realized,
            "insufficient_sample": _insufficient(realized),
            "examples": _examples(trows, best_k) if best_k else [],
        })

    # ---- 按 group ----
    by_group_rows: Dict[str, List[dict]] = {}
    group_promise: Dict[str, Dict[str, List[float]]] = {}
    for r in rows:
        base = (r.get("backtest_stats_snapshot") or {}).get("base") or {}
        for g in (r.get("groups_hit") or []):
            by_group_rows.setdefault(g, []).append(r)
            gp = group_promise.setdefault(g, {"wins": [], "rels": []})
            if base.get("win") is not None:
                gp["wins"].append(rc._f(base.get("win")))
            if base.get("rel_mean") is not None:
                gp["rels"].append(rc._f(base.get("rel_mean")))
    by_group = []
    for g, grows in sorted(by_group_rows.items()):
        realized = _realized_by_horizon(grows)
        gp = group_promise.get(g, {"wins": [], "rels": []})
        promise_win = _median(gp["wins"]) if gp["wins"] else None
        promise_rel = _median(gp["rels"]) if gp["rels"] else None
        best_k = _best_matured_k(realized)
        by_group.append({
            "group": g,
            "n_picks": len(grows),
            "promise_base": {"win": promise_win, "rel_mean": promise_rel},
            "realized": realized,
            "gap": _gap(realized, promise_win, promise_rel),
            "insufficient_sample": _insufficient(realized),
            "examples": _examples(grows, best_k) if best_k else [],
        })

    return {
        "meta": {
            "asof": asof_iso,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "metric": "T+1尾盘进场 relc_k（相对匹配基准）；realized=台账 scored 行，promise=落库时回测快照，gap=realized−promise",
            "ledger_rows": len(rows),
            "matured_horizons": matured_horizons,
            "min_sample_for_sufficient": MIN_SAMPLE,
            "insufficient_sample": len(rows) == 0 or not matured_horizons,
            "caveats": [
                "台账样本随时间累积；早期 horizon（T+5/T+10）常未成熟，只有 T+3 有数属正常。",
                "promise 是样本内回测口径、realized 是样本外实盘，本就该有差；gap 为负不必然是坏画像，先看 n。",
                "win（相对胜率）与 rel_mean（相对均值）口径不同，分开读；均值右偏时同看 median。",
                "tier 表检验分档是否真的分出信息（strong 应优于 watch），不与回测承诺对账（tier 是模型判断无回测标量）。",
                "by_group 的 promise_base 取自各 pick 落库时的 backtest_stats_snapshot.base；一只票命中多组时该 base 会挂到它命中的每个组，故无画像组（如 early_limit_up_1030/monthly_base_breakout）可能蹭到共命中组的 base——读 group 承诺时以有画像组为准。",
            ],
        },
        "by_condition": by_condition,
        "by_tier": by_tier,
        "by_group": by_group,
        "not_yet_mature": {"conditions": not_yet_mature_conditions,
                           "count": len(not_yet_mature_conditions)},
    }
