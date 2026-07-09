# -*- coding: utf-8 -*-
"""因子实验室：因子挖掘的验证与生命周期入口（慢循环）。

子命令：
  experiments  查最近挖矿实验 / 人工判分（本批已实现）
  calibrate    回测画像承诺 vs 实盘台账真实战绩对账（Phase 2，占位）
  refresh      对每个画像用原 spec 在最新窗口重跑，检验旧条件是否还过闸（Phase 3，占位）
  weekly       refresh + calibrate + experiments 汇总成一份周度体检包（Phase 5，占位）

脑/手边界：脚本只铺确定性证据与对账，不判"采纳/维持/降级/刷新"、不定信心档——那些是模型
读证据后的判断，落 git / 落台账由人确认。存储走 shared/data/db_core。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import factor_experiment as fx  # noqa: E402

SKILL_ROOT = HERE.parent
REPORTS_DIR = SKILL_ROOT / "reports"


def _print_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _norm_asof(asof: Optional[str]) -> Optional[str]:
    if not asof:
        return None
    raw = str(asof).replace("-", "").replace("/", "")
    if len(raw) != 8 or not raw.isdigit():
        raise SystemExit(f"--asof 期望 YYYYMMDD，收到 {asof!r}")
    return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"


def _write_pack(payload: Any, path: Path) -> None:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    kb = len(json.dumps(payload, ensure_ascii=False)) / 1024
    print(f"[pack] {path}  ({kb:.1f} KB)")


def cmd_experiments(args: argparse.Namespace) -> int:
    if args.set_verdict:
        res = fx.set_verdict(args.set_verdict, args.verdict, args.note, args.promoted)
        print("[set-verdict] 已更新（仅 verdict/verdict_note/promoted_profile 三列）：")
        _print_json(res)
        return 0
    rows = fx.list_experiments(recent=args.recent)
    if not rows:
        print("（实验台账为空——先跑一次 factor_backtest.py 挖矿）")
        return 0
    print(f"最近 {len(rows)} 条挖矿实验（脚本只列事实，采不采纳看 verdict 列，由人判）：\n")
    for r in rows:
        vd = r["verdict"] or "未判"
        promoted = f" → {r['promoted_profile']}" if r["promoted_profile"] else ""
        print(f"  {r['group_key']}@{r['window_end']}  spec={r['spec_hash'][:8]}  "
              f"命中{r['n_signals']}/{r['n_unique_stocks']}只  "
              f"过闸 单{r['n_singles_passed']}/配{r['n_pairs_passed']}  "
              f"[{vd}]{promoted}")
        if r["verdict_note"]:
            print(f"        note: {r['verdict_note']}")
    print("\n判分：factor_lab.py experiments --set-verdict <group>@<window>@<hash前缀> "
          "--verdict adopted|rejected|observing [--note ... --promoted profile@version]")
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    import factor_calibrate as fc
    asof_iso = _norm_asof(args.asof)
    pack = fc.build_calibration(asof_iso)
    tag = (asof_iso or "all").replace("-", "")
    _write_pack(pack, REPORTS_DIR / f"calibration_{tag}.json")
    m = pack["meta"]
    print(f"\n=== 校准对账（台账 {m['ledger_rows']} 行，成熟 horizon {m['matured_horizons'] or '无'}）===")
    if m["insufficient_sample"]:
        print("  台账尚无成熟样本——校准暂无有效对账，属正常（样本随时间累积）。")
    for row in pack["by_tier"]:
        k = next((kk for kk in ("t3", "t5", "t10") if row["realized"].get(kk, {}).get("scored_n")), None)
        cell = row["realized"].get(k, {}) if k else {}
        flag = " (样本不足)" if row["insufficient_sample"] else ""
        line = f"  tier {row['tier']:<7} picks={row['n_picks']}"
        if cell.get("scored_n"):
            line += f"  {k} scored={cell['scored_n']} 相对胜率={cell.get('rel_win_pct')}% 相对均值={cell.get('rel_mean')}"
        print(line + flag)
    print("\n差值解读交模型：见 references/methodology/factor_lab.md（Phase 5 补）。")
    return 0


def cmd_refresh(args: argparse.Namespace) -> int:
    import factor_refresh as fr
    pack = fr.build_refresh()
    asof = pack["meta"].get("asof") or "latest"
    _write_pack(pack, REPORTS_DIR / f"profile_refresh_{str(asof).replace('-', '')}.json")
    print(f"\n=== 画像衰减重检（窗口滚到 {asof}）===")
    for p in pack["profiles"]:
        print(f"  [{p['profile_id']}] 命中 then={p.get('n_signals_then')} → now={p.get('n_signals_now')}")
        for c in p.get("conditions", []):
            now = c["now"]
            if not now.get("evaluated"):
                state = f"未评估({now.get('reason')})"
            elif "note" in now and now.get("passes_guardrails") is False and "n_retained" in now:
                state = f"塌陷 n={now['n_retained']}（不再过闸）"
            else:
                state = f"now 过闸={now.get('passes_guardrails')} Δ={now.get('delta_vs_base')} 均衡={now.get('oos_balance')} n={now.get('n_retained')}"
            print(f"      {c['label']}: then过闸={c['then'].get('passes_guardrails')} → {state}")
    for s in pack.get("skipped", []):
        print(f"  (跳过 {s['profile_id']}: {s['reason']})")
    print("\n维持/降级/刷新交模型判：见 references/methodology/factor_lab.md（Phase 5 补）。")
    return 0


WEEKLY_CAP_BYTES = 150 * 1024


def _compact_size(payload: Any) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def _enforce_cap(pack: dict, cap: int = WEEKLY_CAP_BYTES) -> dict:
    """周包 ≤cap：超限就按梯度砍例证（决策证据保留），并在 meta 标 truncated。"""
    if _compact_size(pack) <= cap:
        return pack
    trims = []
    # 梯度 1：校准三表例证各留 1 条
    for tbl in ("by_condition", "by_tier", "by_group"):
        for row in pack.get("calibration", {}).get(tbl, []):
            if isinstance(row.get("examples"), list) and len(row["examples"]) > 1:
                row["examples"] = row["examples"][:1]
    trims.append("calibration.examples→1")
    if _compact_size(pack) > cap:
        # 梯度 2：refresh 新候选各留 3 条
        for p in pack.get("profile_refresh", {}).get("profiles", []):
            if isinstance(p.get("new_window_top_candidates"), list):
                p["new_window_top_candidates"] = p["new_window_top_candidates"][:3]
        trims.append("refresh.new_candidates→3")
    if _compact_size(pack) > cap:
        # 梯度 3：清空所有例证
        for tbl in ("by_condition", "by_tier", "by_group"):
            for row in pack.get("calibration", {}).get(tbl, []):
                row.pop("examples", None)
        trims.append("calibration.examples→0")
    pack["meta"]["truncated"] = True
    pack["meta"]["truncation_steps"] = trims
    pack["meta"]["final_size_kb"] = round(_compact_size(pack) / 1024, 1)
    return pack


def cmd_weekly(args: argparse.Namespace) -> int:
    import factor_calibrate as fc
    import factor_refresh as fr
    from datetime import datetime, timezone

    asof_iso = _norm_asof(args.asof)
    refresh = fr.build_refresh()
    calib = fc.build_calibration(asof_iso)
    exps = fx.list_experiments(recent=args.recent)
    asof = asof_iso or refresh["meta"].get("asof") or calib["meta"].get("asof") or "latest"

    pack = {
        "meta": {
            "asof": asof,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "what": "周度因子体检：校准对账 + 画像衰减重检 + 实验台账摘要，一个决策包读完",
            "read_order": "先 calibration（承诺 vs 兑现）→ profile_refresh（旧条件还过闸吗）→ recent_experiments（挖过什么）",
            "methodology": "references/methodology/factor_lab.md",
            "truncated": False,
        },
        "calibration": calib,
        "profile_refresh": refresh,
        "recent_experiments": exps,
    }
    pack = _enforce_cap(pack)
    tag = str(asof).replace("-", "")
    _write_pack(pack, REPORTS_DIR / f"weekly_factor_pack_{tag}.json")
    cm, rm = calib["meta"], refresh["meta"]
    print(f"\n=== 周度因子体检（{asof}）===")
    print(f"  校准：台账 {cm['ledger_rows']} 行，成熟 {cm['matured_horizons'] or '无'}"
          + ("（样本不足，如实标注）" if cm.get("insufficient_sample") else ""))
    print(f"  画像重检：{len(rm and refresh['profiles'])} 个画像，跳过 {len(refresh['skipped'])}")
    decayed = [(p["profile_id"], c["label"]) for p in refresh["profiles"]
               for c in p.get("conditions", [])
               if c["then"].get("passes_guardrails") and c["now"].get("passes_guardrails") is False]
    if decayed:
        print("  ⚠ 衰减条件（then过闸→now不过闸，交模型判降级/移除）：")
        for pid, lbl in decayed:
            print(f"      [{pid}] {lbl}")
    print(f"  实验台账：最近 {len(exps)} 条（未判 {sum(1 for e in exps if not e['verdict'])}）")
    if pack["meta"]["truncated"]:
        print(f"  （包超 150KB，已按 {pack['meta']['truncation_steps']} 截断例证）")
    print("\n写周报：读本包 + references/methodology/factor_lab.md，产出 reports/factor_weekly_<asof>.md。")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="因子实验室：验证与生命周期入口")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("experiments", help="查最近挖矿实验 / 人工判分")
    p.add_argument("--recent", type=int, default=20, help="列最近 N 条")
    p.add_argument("--set-verdict", default=None, metavar="SEL",
                   help="<group_key>@<window_end>@<spec_hash前缀> 判分该实验")
    p.add_argument("--verdict", default=None, choices=list(fx.VERDICT_ENUM),
                   help="adopted / rejected / observing（留空=清空判定）")
    p.add_argument("--note", default=None, help="判分理由")
    p.add_argument("--promoted", default=None, help="提级到的画像 id@version")
    p.set_defaults(func=cmd_experiments)

    for name, fn, helptext in (
        ("calibrate", cmd_calibrate, "回测承诺 vs 实盘台账对账"),
        ("refresh", cmd_refresh, "画像旧条件在最新窗口重检"),
        ("weekly", cmd_weekly, "周度体检汇总（refresh+calibrate+experiments）"),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--asof", default=None, help="YYYYMMDD（默认最新交易日）")
        if name == "weekly":
            sp.add_argument("--recent", type=int, default=20, help="实验台账摘要取最近 N 条")
        sp.set_defaults(func=fn)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
