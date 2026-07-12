# -*- coding: utf-8 -*-
"""策略选股：把因子复盘（策略画像）接进每日复盘的后处理脚本。

这是 build_panel 之外的独立后处理，**不污染日报核心证据路径**。三个子命令：

  context   读当日 evidence 的特征分组命中 + overlap，对照 references/strategy_profiles/
            里被验证过的叠加条件做确定性匹配，拼上「回测画像表现」与（分开展示的）
            「样本外台账表现」，写 module6_strategy_candidates.json。脚本只铺候选证据、
            **不定信心档**——强/中/观察由模型在聚合阶段写第 6 节时判定。
  score     把过去票里已成熟的 horizon（T+3/5/10）回填真实前向相对收益（复用 returns_core，
            与回测同尺）。分 horizon 独立状态，幂等：只填成熟且可算的列，重跑不重复计数。
  record    模型定稿第 6 节后，把确认的选股（含画像指纹 + 当时特征/条件快照）upsert 进
            strategy_pick_ledger，积累样本外战绩。

脑/手边界：脚本只做确定性匹配、收益计算、落库校验；选谁、信心几档、归因措辞、
持续性待验证条件，全部是模型的活。画像 JSON 由模型按 factor_mining 选解后写/改，
本脚本只加载 + schema 校验 + 算 hash + 判过期，不替模型选条件。

存储：策略画像 = references/strategy_profiles/*.json（进 git，canonical）；
实盘台账 = PG strategy_pick_ledger（init_alpha_data.sql §14，本脚本亦自带 ensure 建表）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # market_panel, returns_core
_BUNDLED_SHARED = HERE / "_shared"
_DEV_SHARED = HERE.parents[2] / "shared" / "data"
sys.path.insert(0, str(_BUNDLED_SHARED if _BUNDLED_SHARED.exists() else _DEV_SHARED))

from db_core import BACKEND, Backend, get_connection, placeholder, table_exists  # noqa: E402
import returns_core as rc  # noqa: E402

SKILL_ROOT = HERE.parent
DEFAULT_PROFILES_DIR = SKILL_ROOT / "references" / "strategy_profiles"
DEFAULT_REPORTS_DIR = SKILL_ROOT / "reports"

TIERS = ("strong", "medium", "watch")
HK = (3, 5, 10)
TERMINAL_STATUS = ("scored", "expired")
TS_CODE_RE = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")

# 当日可评估的因子：来自 evidence 命中行 + add_numeric_features 目标日行。
# t1_gap 是 T+1 的未来值，当日选股时不可知，刻意排除（只在回测里用）。
_EVIDENCE_FACTORS = (
    "pct_chg", "total_mv_100m_yuan", "turnover_rate", "volume_ratio",
    "amount_100m_yuan", "close", "discount_after_high", "close_vs_prev_high",
)
_COMPUTED_FACTORS = (
    "close_position_120d", "pre_ret_5d", "amount_vs_prev5_ratio",
    "drawdown_120_high", "amount_ratio_20d", "ret_5d", "ret_20d",
)
_OPS = {
    ">": lambda a, b: a > b, ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b, "<=": lambda a, b: a <= b, "==": lambda a, b: a == b,
}


# --------------------------------------------------------------------------- #
# Ledger DDL (mirror init_alpha_data.sql §14; self-provision so a brand-new
# table never depends on the operator re-running the schema file)
# --------------------------------------------------------------------------- #
def _horizon_cols(k: int) -> str:
    return (
        f"t{k}_date DATE, t{k}_close DOUBLE PRECISION, "
        f"ro_{k} DOUBLE PRECISION, rc_{k} DOUBLE PRECISION, "
        f"relo_{k} DOUBLE PRECISION, relc_{k} DOUBLE PRECISION, relc_{k}_w DOUBLE PRECISION, "
        f"h{k}_status TEXT NOT NULL DEFAULT 'pending', scored_at_{k} TIMESTAMPTZ"
    )


LEDGER_DDL = f"""
CREATE TABLE IF NOT EXISTS strategy_pick_ledger (
    asof                    DATE NOT NULL,
    ts_code                 TEXT NOT NULL,
    name                    TEXT,
    board                   TEXT,
    benchmark               TEXT,
    benchmark_wide          TEXT,
    groups_hit              JSONB,
    conviction_tier         TEXT NOT NULL,
    in_main_line            TEXT,
    rationale               TEXT,
    matched_conditions      JSONB,
    feature_snapshot        JSONB,
    profile_fingerprints    JSONB,
    backtest_stats_snapshot JSONB,
    source_evidence         TEXT,
    source_report           TEXT,
    t1_date                 DATE,
    t1_open                 DOUBLE PRECISION,
    t1_close                DOUBLE PRECISION,
    {_horizon_cols(3)},
    {_horizon_cols(5)},
    {_horizon_cols(10)},
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (asof, ts_code)
)
"""


def ensure_tables(conn: Any) -> None:
    cur = conn.cursor()
    cur.execute(LEDGER_DDL)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_strategy_pick_tier ON strategy_pick_ledger(conviction_tier)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_strategy_pick_h3_status ON strategy_pick_ledger(h3_status)")


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def norm_iso(value: str) -> str:
    raw = str(value or "").strip().replace("-", "").replace("/", "")
    if not re.match(r"^\d{8}$", raw):
        raise SystemExit(f"无法解析日期: {value!r}（期望 YYYYMMDD 或 YYYY-MM-DD）")
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


def ymd(value: str) -> str:
    return norm_iso(value).replace("-", "")


def _f(x: Any) -> Optional[float]:
    return rc._f(x)


def _json_param(value: Any) -> Any:
    if value is None:
        return None
    if BACKEND == Backend.POSTGRESQL:
        from psycopg2.extras import Json
        return Json(value, dumps=lambda v: json.dumps(v, ensure_ascii=False))
    return json.dumps(value, ensure_ascii=False)


def _json_load_maybe(value: Any) -> Any:
    if isinstance(value, (list, dict)) or value is None:
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _fetch_all(conn: Any, sql: str, params: tuple = ()) -> List[dict]:
    cur = conn.cursor()
    cur.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# --------------------------------------------------------------------------- #
# Strategy profiles（references/strategy_profiles/*.json，canonical 真相源）
# --------------------------------------------------------------------------- #
def load_profiles(profiles_dir: Path) -> Dict[str, dict]:
    """加载所有画像，附 profile_hash（按文件内容算 sha256）。键 = profile_id。"""
    out: Dict[str, dict] = {}
    if not profiles_dir.is_dir():
        return out
    for path in sorted(profiles_dir.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        try:
            prof = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"画像 {path.name} 不是合法 JSON：{exc}")
        pid = str(prof.get("profile_id") or path.stem)
        prof["profile_id"] = pid
        prof["profile_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        prof["source_path"] = str(path.relative_to(SKILL_ROOT)) if str(path).startswith(str(SKILL_ROOT)) else str(path)
        _validate_profile(prof, path.name)
        out[pid] = prof
    return out


def _validate_profile(prof: dict, fname: str) -> None:
    if not prof.get("target_cell"):
        raise SystemExit(f"画像 {fname} 缺 target_cell")
    for i, cond in enumerate(prof.get("selected_conditions") or []):
        atoms = cond.get("all") or []
        if not atoms:
            raise SystemExit(f"画像 {fname} selected_conditions[{i}] 缺 all（条件原子列表）")
        for a in atoms:
            if a.get("op") not in _OPS:
                raise SystemExit(f"画像 {fname} 条件 {cond.get('condition_id')} 操作符非法：{a.get('op')!r}")
            if a.get("factor") is None or a.get("threshold") is None:
                raise SystemExit(f"画像 {fname} 条件 {cond.get('condition_id')} 缺 factor/threshold")


def profile_coverage(prof: Optional[dict], asof_iso: str) -> dict:
    """画像可用性：calibrated / stale / missing（缺失/过期显式降级，不阻断日报）。"""
    if prof is None:
        return {"status": "missing", "note": "暂无策略画像，命中只作纯观察、不拼历史胜率"}
    prof_asof = prof.get("asof")
    max_age = int(prof.get("max_age_days") or 60)
    age_days = None
    stale = False
    if prof_asof:
        try:
            age_days = (datetime.strptime(asof_iso, "%Y-%m-%d") - datetime.strptime(norm_iso(prof_asof), "%Y-%m-%d")).days
            stale = age_days > max_age
        except ValueError:
            pass
    return {
        "status": "stale" if stale else "calibrated",
        "profile_version": prof.get("profile_version"),
        "profile_asof": prof_asof,
        "profile_hash": prof.get("profile_hash"),
        "target_cell": prof.get("target_cell"),
        "robustness": prof.get("robustness"),
        "age_days": age_days,
        "max_age_days": max_age,
        "note": (f"画像已过期（{age_days}天 > {max_age}天上限），建议重新挖矿；本期仅供参考" if stale else None),
    }


# --------------------------------------------------------------------------- #
# Condition evaluation（确定性匹配，脚本只判真假、不下结论）
# --------------------------------------------------------------------------- #
def eval_atom(atom: dict, feats: Dict[str, Any]) -> Optional[bool]:
    val = _f(feats.get(atom["factor"]))
    if val is None:
        return None  # 因子缺失 → 无法评估
    return _OPS[atom["op"]](val, float(atom["threshold"]))


def eval_condition(cond: dict, feats: Dict[str, Any]) -> str:
    """一条叠加条件（多原子 AND）在某股当日特征上的判定：match / fail / cannot_evaluate。"""
    results = [eval_atom(a, feats) for a in cond.get("all", [])]
    if any(r is None for r in results):
        return "cannot_evaluate"
    return "match" if all(results) else "fail"


def _atom_label(atom: dict) -> str:
    return f"{atom['factor']} {atom['op']} {atom['threshold']:g}"


def _cond_label(cond: dict) -> str:
    return cond.get("label") or " ∧ ".join(_atom_label(a) for a in cond.get("all", []))


# --------------------------------------------------------------------------- #
# Feature enrichment：候选股当日特征（evidence 命中行 + add_numeric_features 目标日行）
# --------------------------------------------------------------------------- #
def enrich_features(groups: dict, asof_ymd: str) -> Dict[str, Dict[str, Any]]:
    """对所有命中股，合并 evidence 携带的因子与计算因子，得到当日 feature_snapshot。"""
    ev_feats: Dict[str, Dict[str, Any]] = {}
    for gkey, gval in groups.items():
        if not isinstance(gval, dict):
            continue
        for cand in gval.get("candidates") or []:
            code = cand.get("ts_code")
            if not code:
                continue
            snap = ev_feats.setdefault(code, {})
            for fac in _EVIDENCE_FACTORS:
                if cand.get(fac) is not None and snap.get(fac) is None:
                    snap[fac] = _f(cand.get(fac))
            if cand.get("name") and "name" not in snap:
                snap["name"] = cand.get("name")

    codes = sorted(ev_feats.keys())
    if not codes:
        return ev_feats

    # add_numeric_features 需要个股完整历史；候选股仅几十只，重算很便宜。
    import market_panel as mp
    daily = rc.load_daily(codes)
    if not daily.empty:
        feats = mp.add_numeric_features(daily)
        feats["trade_date"] = feats["trade_date"].astype(str)
        target = feats[feats["trade_date"] == asof_ymd]
        idx = target.set_index("ts_code")
        for code in codes:
            if code in idx.index:
                row = idx.loc[code]
                for fac in _COMPUTED_FACTORS:
                    if fac in row and pd.notna(row[fac]):
                        ev_feats[code].setdefault(fac, _f(row[fac]))
    return ev_feats


# --------------------------------------------------------------------------- #
# Out-of-sample ledger 战绩（与回测画像表现分开展示）
# --------------------------------------------------------------------------- #
def ledger_oos_stats(conn: Any, asof_iso: str) -> dict:
    rows = _fetch_all(
        conn,
        "SELECT asof, conviction_tier, relc_5, rc_5, h5_status FROM strategy_pick_ledger "
        f"WHERE h5_status = {placeholder()} AND asof < {placeholder()}",
        ("scored", asof_iso),
    )

    def agg(subset: List[dict]) -> dict:
        rels = [_f(r["relc_5"]) for r in subset if _f(r["relc_5"]) is not None]
        abss = [_f(r["rc_5"]) for r in subset if _f(r["rc_5"]) is not None]
        if not rels:
            return {"n": len(subset), "scored_n": 0}
        rels.sort()
        return {
            "n": len(subset),
            "scored_n": len(rels),
            "t5_rel_win_pct": round(sum(1 for x in rels if x > 0) / len(rels) * 100, 1),
            "t5_rel_mean": round(sum(rels) / len(rels), 2),
            "t5_rel_median": round(rels[len(rels) // 2], 2),
            "t5_abs_mean": round(sum(abss) / len(abss), 2) if abss else None,
        }

    by_tier = {t: agg([r for r in rows if r["conviction_tier"] == t]) for t in TIERS}
    return {
        "metric": "T+1尾盘→T+5 相对匹配基准（relc_5），仅统计已 score 的样本外票",
        "overall": agg(rows),
        "by_tier": by_tier,
        "caveat": "样本外台账，样本随时间累积；N 小时只作参考、不与回测画像表现混为一谈。",
    }


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #
def _load_evidence(path: Path) -> Tuple[dict, str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    # 完整 evidence pack 用 feature_group_analysis_samples；module5 上下文 JSON 用
    # feature_group_analysis。两者都接，方便 runner 直接传 evidence、也可单独传 module5。
    fga = payload.get("feature_group_analysis") or payload.get("feature_group_analysis_samples")
    if not fga or not fga.get("groups"):
        raise SystemExit(f"{path} 中找不到 feature_group_analysis(_samples)（请传 evidence 或 module5 JSON）")
    meta = payload.get("metadata") or {}
    asof = meta.get("resolved_trade_date") or meta.get("asof_input")
    if not asof:
        raise SystemExit(f"{path} metadata 缺 resolved_trade_date")
    return fga, ymd(asof)


def cmd_context(args: argparse.Namespace) -> int:
    fga, asof_y = _load_evidence(Path(args.evidence))
    asof_iso = norm_iso(asof_y)
    groups = fga.get("groups") or {}
    overlap_hits = fga.get("overlap_hits") or []
    profiles = load_profiles(Path(args.profiles))

    feats_by_code = enrich_features(groups, asof_y)

    # 每只命中股命中了哪些组
    groups_hit_by_code: Dict[str, List[str]] = {}
    label_by_group = {}
    for gkey, gval in groups.items():
        if not isinstance(gval, dict):
            continue
        label_by_group[gkey] = (gval.get("filter_criteria", {}) or {}).get("__label__") or gkey
        for cand in gval.get("candidates") or []:
            code = cand.get("ts_code")
            if code:
                groups_hit_by_code.setdefault(code, []).append(gkey)

    overlap_codes = {h.get("ts_code") for h in overlap_hits}

    coverage = {gkey: profile_coverage(profiles.get(gkey), asof_iso) for gkey in groups
                if isinstance(groups.get(gkey), dict)}

    candidates_out: List[dict] = []
    for code, gkeys in sorted(groups_hit_by_code.items()):
        feats = feats_by_code.get(code, {})
        name = feats.get("name")
        total_mv_wan = (feats.get("total_mv_100m_yuan") or 0) * 1e4 or None
        per_group = []
        n_matched_total = 0
        best = None  # (win, rel_mean) of best matched condition
        for gkey in gkeys:
            prof = profiles.get(gkey)
            cov = coverage.get(gkey, {})
            if prof is None or cov.get("status") == "missing":
                per_group.append({"group": gkey, "profile": "missing",
                                  "note": "该组暂无策略画像，仅技术命中、不拼历史胜率"})
                continue
            matched, unmatched, cannot = [], [], []
            for cond in prof.get("selected_conditions") or []:
                verdict = eval_condition(cond, feats)
                rec = {
                    "condition_id": cond.get("condition_id"),
                    "label": _cond_label(cond),
                    "n": cond.get("n"), "win": cond.get("win"),
                    "rel_mean": cond.get("rel_mean"), "delta": cond.get("delta"),
                    "oos_balance": cond.get("oos_balance"),
                    "target_cell": cond.get("target_cell") or prof.get("target_cell"),
                }
                if verdict == "match":
                    matched.append(rec)
                    n_matched_total += 1
                    score = (rec.get("win") or 0, rec.get("rel_mean") or 0)
                    if best is None or score > best:
                        best = score
                elif verdict == "fail":
                    unmatched.append(rec)
                else:
                    cannot.append(rec)
            per_group.append({
                "group": gkey,
                "profile_status": cov.get("status"),
                "profile_version": cov.get("profile_version"),
                "profile_target_cell": prof.get("target_cell"),
                "base_backtest": prof.get("base"),
                "robustness": prof.get("robustness"),
                "matched_conditions": matched,
                "unmatched_conditions": unmatched,
                "cannot_evaluate": cannot,
            })
        candidates_out.append({
            "ts_code": code,
            "name": name,
            "board": rc.board_of(code),
            "benchmark": rc.matched_benchmark(code, total_mv_wan),
            "benchmark_wide": rc.WIDE_BASE,
            "groups_hit": gkeys,
            "cross_group": code in overlap_codes or len(gkeys) >= 2,
            "cross_group_count": len(gkeys),
            "feature_snapshot": feats,
            "profiles": per_group,
            # 中性排序键（脚本不定信心档）：满足有效条件数 → 最优条件胜率/相对均值 → 交叉命中数
            "neutral_rank_key": [n_matched_total, (best or (0, 0))[0], (best or (0, 0))[1], len(gkeys)],
        })

    candidates_out.sort(key=lambda c: c["neutral_rank_key"], reverse=True)

    with get_connection() as conn:
        ensure_tables(conn)
        oos = ledger_oos_stats(conn, asof_iso)

    payload = {
        "meta": {
            "asof": asof_iso,
            "source_evidence": str(Path(args.evidence)),
            "model_responsibility": (
                "脚本只铺确定性候选证据（命中组别、满足/未满足/无法评估的被验证条件及其历史 N/胜率/"
                "Δ/oos_balance、当日因子值、交叉命中、样本外台账战绩、中性排序键）。是否选入、信心几档"
                "（强/中/观察）、主线归属、持续性待验证条件、归因措辞全部由模型在聚合阶段写第 6 节时判定，"
                "脚本不调用 LLM、不定信心档、不出买卖建议。"
            ),
            "profiles_coverage": coverage,
            "caveats": [
                "回测画像表现与样本外台账表现分开看，不要混成一个胜率。",
                "画像缺失/过期的分组按未标定降级，命中只作纯技术观察。",
                "信心分档须由模型对照 3.1 主线表（主线归属）与稳健度判定；技术命中不等于可操作。",
            ],
        },
        "oos_ledger_stats": oos,
        "candidate_count": len(candidates_out),
        "candidates": candidates_out,
    }

    out_path = Path(args.out) if args.out else (
        DEFAULT_REPORTS_DIR / f"module_context_{asof_y}" / "module6_strategy_candidates.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    _print_json({"ok": True, "asof": asof_iso, "candidates": len(candidates_out),
                 "calibrated_groups": [g for g, c in coverage.items() if c.get("status") == "calibrated"],
                 "out": str(out_path)})
    return 0


# --------------------------------------------------------------------------- #
# score：把过去票里已成熟的 horizon 回填真实前向收益（分 horizon 幂等）
# --------------------------------------------------------------------------- #
def cmd_score(args: argparse.Namespace) -> int:
    asof_iso = norm_iso(args.asof)
    calendar = rc.load_calendar()
    cal_idx = {d: i for i, d in enumerate(calendar)}
    latest_i = len(calendar) - 1
    expire_grace = int(args.expire_grace)

    with get_connection() as conn:
        ensure_tables(conn)
        # 取任一 horizon 还没到终态的票
        cond = " OR ".join([f"h{k}_status NOT IN ('scored','expired')" for k in HK])
        rows = _fetch_all(
            conn,
            f"SELECT asof, ts_code, benchmark, feature_snapshot, "
            f"h3_status, h5_status, h10_status FROM strategy_pick_ledger WHERE ({cond})",
        )
        if not rows:
            _print_json({"ok": True, "asof": asof_iso, "scored": 0, "note": "无待回填票"})
            return 0

        sig_rows = []
        for r in rows:
            feats = _json_load_maybe(r["feature_snapshot"]) or {}
            sig_rows.append({
                "ts_code": r["ts_code"],
                "signal_date": ymd(str(r["asof"])),
                "total_mv_100m_yuan": _f(feats.get("total_mv_100m_yuan")),
                "benchmark": r["benchmark"],
            })
        sig_df = pd.DataFrame(sig_rows)
        fwd = rc.compute_forward_returns(sig_df, calendar)
        fwd_idx = {(row.ts_code, row.signal_date): row for row in fwd.itertuples()}

        cur = conn.cursor()
        ph = placeholder()
        updated = {"scored": 0, "missing_price": 0, "expired": 0, "still_pending": 0}
        now = datetime.now()
        for r in rows:
            code = r["ts_code"]
            sd = ymd(str(r["asof"]))
            frow = fwd_idx.get((code, sd))
            sets: List[str] = []
            params: List[Any] = []
            for k in HK:
                if r[f"h{k}_status"] in TERMINAL_STATUS:
                    continue
                tk = calendar[cal_idx[sd] + k] if (sd in cal_idx and cal_idx[sd] + k < len(calendar)) else None
                if tk is None:
                    updated["still_pending"] += 1
                    continue  # 未成熟（T+k 尚无交易日）
                rc_k = _f(getattr(frow, f"rc_{k}", None)) if frow is not None else None
                tk_date = getattr(frow, f"t{k}_date", None) if frow is not None else None
                if rc_k is not None and tk_date:
                    fill = {
                        f"t{k}_date": norm_iso(tk_date),
                        f"t{k}_close": _f(getattr(frow, f"t{k}_close", None)),
                        f"ro_{k}": _f(getattr(frow, f"ro_{k}", None)),
                        f"rc_{k}": rc_k,
                        f"relo_{k}": _f(getattr(frow, f"relo_{k}", None)),
                        f"relc_{k}": _f(getattr(frow, f"relc_{k}", None)),
                        f"relc_{k}_w": _f(getattr(frow, f"relc_{k}_w", None)),
                        f"h{k}_status": "scored",
                        f"scored_at_{k}": now,
                    }
                    for col, val in fill.items():
                        sets.append(f"{col} = {ph}")
                        params.append(val)
                    updated["scored"] += 1
                else:
                    # 成熟了但价格缺（停牌/数据缺）；超出宽限期标 expired，否则留待重试
                    aged = latest_i - cal_idx.get(tk, latest_i)
                    status = "expired" if aged > expire_grace else "missing_price"
                    sets.append(f"h{k}_status = {ph}"); params.append(status)
                    updated[status if status == "expired" else "missing_price"] += 1
            if sets:
                sets.append(f"updated_at = {ph}"); params.append(now)
                params.extend([r["asof"], code])
                cur.execute(
                    f"UPDATE strategy_pick_ledger SET {', '.join(sets)} WHERE asof = {ph} AND ts_code = {ph}",
                    tuple(params),
                )

    _print_json({"ok": True, "asof": asof_iso, **updated})
    return 0


# --------------------------------------------------------------------------- #
# record：模型定稿后落库当日选股（带画像指纹 + 当时特征/条件快照）
# --------------------------------------------------------------------------- #
def cmd_record(args: argparse.Namespace) -> int:
    if args.input == "-":
        payload = json.load(sys.stdin)
    else:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
    asof_iso = norm_iso(args.asof or payload.get("asof") or "")
    asof_y = asof_iso.replace("-", "")
    picks = payload.get("picks", [])
    if not picks:
        raise SystemExit("输入中无 picks，无事可写")
    source_report = payload.get("source_report", "")
    source_evidence = payload.get("source_evidence", "")

    calendar = rc.load_calendar()
    cal_idx = {d: i for i, d in enumerate(calendar)}
    t1_date = None
    if asof_y in cal_idx and cal_idx[asof_y] + 1 < len(calendar):
        t1_date = norm_iso(calendar[cal_idx[asof_y] + 1])

    errors: List[str] = []
    plans: List[dict] = []
    seen = set()
    for i, p in enumerate(picks):
        code = str(p.get("ts_code") or "")
        if not TS_CODE_RE.match(code):
            errors.append(f"picks[{i}]: ts_code {code!r} 不符合 NNNNNN.SH/SZ/BJ")
            continue
        if code in seen:
            errors.append(f"picks[{i}]: {code} 重复")
            continue
        seen.add(code)
        tier = p.get("conviction_tier")
        if tier not in TIERS:
            errors.append(f"picks[{i}]: conviction_tier {tier!r} 须为 {TIERS}")
            continue
        feats = p.get("feature_snapshot") or {}
        total_mv_wan = (_f(feats.get("total_mv_100m_yuan")) or 0) * 1e4 or None
        plans.append({
            "asof": asof_iso, "ts_code": code, "name": p.get("name"),
            "board": p.get("board") or rc.board_of(code),
            "benchmark": p.get("benchmark") or rc.matched_benchmark(code, total_mv_wan),
            "benchmark_wide": p.get("benchmark_wide") or rc.WIDE_BASE,
            "groups_hit": p.get("groups_hit"),
            "conviction_tier": tier,
            "in_main_line": p.get("in_main_line"),
            "rationale": p.get("rationale"),
            "matched_conditions": p.get("matched_conditions"),
            "feature_snapshot": feats,
            "profile_fingerprints": p.get("profile_fingerprints"),
            "backtest_stats_snapshot": p.get("backtest_stats_snapshot"),
            "source_evidence": p.get("source_evidence") or source_evidence,
            "source_report": p.get("source_report") or source_report,
            "t1_date": t1_date,
        })

    if errors:
        _print_json({"ok": False, "asof": asof_iso, "errors": errors})
        raise SystemExit(1)

    cols = ["asof", "ts_code", "name", "board", "benchmark", "benchmark_wide",
            "groups_hit", "conviction_tier", "in_main_line", "rationale",
            "matched_conditions", "feature_snapshot", "profile_fingerprints",
            "backtest_stats_snapshot", "source_evidence", "source_report", "t1_date"]
    json_cols = {"groups_hit", "matched_conditions", "feature_snapshot",
                 "profile_fingerprints", "backtest_stats_snapshot"}
    ph = placeholder()
    with get_connection() as conn:
        ensure_tables(conn)
        cur = conn.cursor()
        for p in plans:
            vals = [_json_param(p[c]) if c in json_cols else p[c] for c in cols]
            updates = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in ("asof", "ts_code"))
            cur.execute(
                f"INSERT INTO strategy_pick_ledger ({', '.join(cols)}) "
                f"VALUES ({', '.join([ph] * len(cols))}) "
                f"ON CONFLICT (asof, ts_code) DO UPDATE SET {updates}, updated_at = NOW()",
                tuple(vals),
            )

    _print_json({"ok": True, "asof": asof_iso, "written": len(plans),
                 "by_tier": {t: sum(1 for p in plans if p["conviction_tier"] == t) for t in TIERS}})
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("context", help="读 evidence 命中 × 画像，写 module6_strategy_candidates.json（不定信心档）")
    p.add_argument("--evidence", required=True, help="evidence_YYYYMMDD_utf8.json 或 module5 JSON 路径")
    p.add_argument("--profiles", default=str(DEFAULT_PROFILES_DIR), help="策略画像目录")
    p.add_argument("--out", default=None, help="输出 JSON 路径（默认写入对应 module_context 目录）")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("score", help="回填过去票已成熟 horizon 的真实前向相对收益（分 horizon 幂等）")
    p.add_argument("--asof", required=True, help="当前交易日 YYYYMMDD（成熟判定基准）")
    p.add_argument("--expire-grace", default=15, help="成熟后仍缺价多少交易日判 expired（停止重试）")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("record", help="把模型定稿的当日选股 upsert 进实盘台账（带快照/指纹）")
    p.add_argument("--input", required=True, help="strategy_picks JSON 路径，- 表示 stdin")
    p.add_argument("--asof", default=None, help="覆盖输入中的 asof")
    p.set_defaults(func=cmd_record)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
