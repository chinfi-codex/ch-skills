"""Unit tests for pool_rules (pure) and pool_store (SQLite backend).

Run: python3 -m pytest ch-stock-skills/a-stock-pool-tracker/scripts -q
"""

from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import pool_rules  # noqa: E402

RULES = pool_rules.load_rules()


def _day(k: int) -> str:
    return (date(2026, 9, 1) + timedelta(days=k)).isoformat()


def make_bars(post: list[tuple]) -> tuple[list[dict], dict]:
    """25 flat days at 10, a limit-up entry day (10 → 11), then *post* bars.

    post item: (open, high, low, close, amount_thousand)
    """
    bars, prev = [], 10.0
    for k in range(25):
        bars.append(dict(trade_date=_day(k), open=10, high=10.1, low=9.9, close=10, pre_close=10,
                         pct_chg=0, amount=100000, adj_factor=1.0))
    bars.append(dict(trade_date=_day(25), open=10.2, high=11, low=10.1, close=11, pre_close=10,
                     pct_chg=10, amount=500000, adj_factor=1.0))
    prev = 11.0
    for k, (o, h, low, c, amt) in enumerate(post, start=26):
        bars.append(dict(trade_date=_day(k), open=o, high=h, low=low, close=c, pre_close=prev,
                         pct_chg=round((c / prev - 1) * 100, 2), amount=amt, adj_factor=1.0))
        prev = c
    member = dict(ts_code="600001.SH", name="测试", entry_date=_day(25), source="auto", status="active",
                  limit_price=11, entry_low=10.1, launch_price=10, entry_amount=500000, entry_adj_factor=1.0)
    return bars, member


# --------------------------------------------------------------------------- #
# Entry screen
# --------------------------------------------------------------------------- #
BASE_ROW = dict(ts_code="000001.SZ", name="样本", open=10.3, high=11, low=10.2, close=11, pre_close=10,
                up_limit=11, amount=400000, turnover_rate=8.0, first_time="094512", list_date="20100101")


def screen(**kw):
    return pool_rules.screen_limit_up(dict(BASE_ROW, **kw), RULES, "2026-10-22")


def test_screen_pass():
    ok, reasons, facts = screen()
    assert ok, reasons
    assert facts["amount_yi"] == 4.0 and facts["first_limit_time"] == "094512"


def test_screen_rejects_each_rule():
    assert "one_word" in screen(low=11, open=11)[1]
    assert "amount_below_min" in screen(amount=290000)[1]
    assert "turnover_too_high" in screen(turnover_rate=15.0)[1]
    assert "first_time_after_cutoff" in screen(first_time="103001")[1]
    assert "first_time_missing" in screen(first_time=None)[1]
    assert "st" in screen(name="*ST样本", up_limit=10.5, close=10.5)[1]
    assert "newly_listed" in screen(list_date="20261015")[1]
    assert "not_limit_close" in screen(close=10.9)[1]


def test_screen_boundary_1030_passes():
    assert screen(first_time="103000")[0]


def test_board_limit_fallback():
    # 创业板 20%：pre_close 10 → 12.00
    ok, _, facts = screen(ts_code="300001.SZ", up_limit=None, close=12, high=12)
    assert ok and facts["up_limit"] == 12.0
    assert pool_rules.board_limit_pct("830001.BJ") == 0.30


# --------------------------------------------------------------------------- #
# Exit rules
# --------------------------------------------------------------------------- #
def test_break_entry_low_removes_auto_member():
    bars, member = make_bars([(10.8, 10.9, 10.4, 10.5, 200000), (10.4, 10.5, 9.9, 10.0, 300000)])
    m = pool_rules.compute_metrics(bars, member)
    hits = pool_rules.exit_hits(m, RULES)
    assert any(h["rule"] == "break_entry_low" for h in hits)
    d = pool_rules.decide_status(member, hits, 0, RULES)
    assert d["to"] == "removed" and d["changed"]


def test_manual_member_only_watched():
    bars, member = make_bars([(10.4, 10.5, 9.9, 10.0, 300000)])
    member["source"] = "manual"
    m = pool_rules.compute_metrics(bars, member)
    d = pool_rules.decide_status(member, pool_rules.exit_hits(m, RULES), 0, RULES)
    assert d["to"] == "watch" and d["suggest_remove"]


def test_below_ma20_streak_ignores_pre_entry_days():
    # 入池前一路阴跌、涨停当天仍在 MA20 下方：入池当天不应进预警
    bars, member = make_bars([])
    for k, b in enumerate(bars[:25]):
        b.update(close=14 - k * 0.15, high=14.1 - k * 0.15, low=13.9 - k * 0.15)
    bars[24].update(close=10, low=9.9)
    m = pool_rules.compute_metrics(bars, member)
    assert m["above_ma20"] is False and m["below_ma20_streak"] == 0
    assert not pool_rules.exit_hits(m, RULES)


def test_watch_escalates_then_repairs():
    member = dict(status="watch", source="auto")
    hit = [{"rule": "below_ma20", "level": "watch", "fact": "x"}]
    assert pool_rules.decide_status(member, hit, 1, RULES)["to"] == "watch"
    assert pool_rules.decide_status(member, hit, 2, RULES)["to"] == "removed"
    assert pool_rules.decide_status(member, [], 2, RULES)["to"] == "active"


def test_adjusted_anchor_survives_ex_dividend():
    # 10 送 10：入池后因子翻倍、原始价腰斩；复权后不应误判跌破
    bars, member = make_bars([(5.6, 5.8, 5.5, 5.7, 200000)])
    bars[-1]["pre_close"] = 5.5
    bars[-1]["adj_factor"] = 2.0
    m = pool_rules.compute_metrics(bars, member)
    assert m["entry_low"] == 5.05 and m["close"] == 5.7
    assert not any(h["rule"] == "break_entry_low" for h in pool_rules.exit_hits(m, RULES))


# --------------------------------------------------------------------------- #
# Entry signals
# --------------------------------------------------------------------------- #
def test_breakout_after_consolidation():
    bars, member = make_bars([
        (11.2, 11.8, 11.1, 11.5, 300000),   # 平台高点 11.8
        (11.4, 11.5, 11.1, 11.3, 150000),
        (11.3, 11.4, 11.0, 11.2, 120000),
        (11.2, 11.5, 11.1, 11.4, 110000),
        (11.5, 12.2, 11.4, 12.0, 400000),   # 放量站上 11.8
    ])
    m = pool_rules.compute_metrics(bars, member)
    sigs = pool_rules.entry_signals(m, RULES)
    assert [s["type"] for s in sigs] == ["breakout"]
    assert sigs[0]["trigger_price"] == 11.8 and sigs[0]["invalidation_price"] == 11.45


def test_no_breakout_without_consolidation():
    bars, member = make_bars([(11.2, 11.8, 11.1, 11.5, 300000), (11.6, 12.3, 11.5, 12.2, 500000)])
    m = pool_rules.compute_metrics(bars, member)
    assert not [s for s in pool_rules.entry_signals(m, RULES) if s["type"] == "breakout"]


def test_shrinking_pullback_to_limit_price():
    bars, member = make_bars([
        (11.5, 12.5, 11.4, 12.4, 400000),
        (12.3, 12.4, 11.8, 11.9, 200000),
        (11.9, 12.0, 11.5, 11.6, 150000),
        (11.5, 11.6, 11.1, 11.3, 60000),    # 缩量踩到涨停价 11 上方 2% 内
    ])
    m = pool_rules.compute_metrics(bars, member)
    sigs = [s for s in pool_rules.entry_signals(m, RULES) if s["type"] == "pullback"]
    assert sigs and sigs[0]["support"] == "limit_price"
    assert sigs[0]["invalidation_price"] == 10.67


def test_heavy_volume_drop_is_not_pullback():
    bars, member = make_bars([
        (11.5, 12.5, 11.4, 12.4, 400000),
        (12.3, 12.4, 11.0, 11.2, 600000),   # 放量下杀，不是缩量回踩
    ])
    m = pool_rules.compute_metrics(bars, member)
    assert not [s for s in pool_rules.entry_signals(m, RULES) if s["type"] == "pullback"]


def test_relimit_detected():
    bars, member = make_bars([(11.2, 12.1, 11.1, 12.1, 400000)])
    assert pool_rules.compute_metrics(bars, member)["relimit_today"]


# --------------------------------------------------------------------------- #
# Store (SQLite)
# --------------------------------------------------------------------------- #
def test_store_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_DB_BACKEND", "sqlite")
    monkeypatch.setenv("ALPHA_SQLITE_DIR", str(tmp_path))
    for mod in ("db_core", "pool_store"):
        sys.modules.pop(mod, None)
    import pool_store

    pool_store.ensure_schema()
    pool_store.add_member(dict(ts_code="600001.SH", entry_date="2026-10-22", name="测试", source="auto",
                               limit_price=11, entry_low=10.1, launch_price=10, entry_amount=500000))
    try:
        pool_store.add_member(dict(ts_code="600001.SH", entry_date="2026-10-23"))
        raise AssertionError("duplicate open member accepted")
    except pool_store.PoolError:
        pass
    pool_store.upsert_daily("600001.SH", "2026-10-22", "2026-10-23", "active", {"close": 11.2}, [], [])
    pool_store.upsert_daily("600001.SH", "2026-10-22", "2026-10-23", "watch", {"close": 11.0}, [], [])
    assert pool_store.set_verdict("600001.SH", "2026-10-23", "分歧") == 1
    hist = pool_store.daily_history("600001.SH", "2026-10-22")
    assert len(hist) == 1 and hist[0]["metrics"]["close"] == 11.0 and hist[0]["verdict"] == "分歧"
    pool_store.add_event("600001.SH", "2026-10-22", "2026-10-23", "relimit", "再涨停")
    pool_store.add_event("600001.SH", "2026-10-22", "2026-10-23", "relimit", "再涨停")
    kinds = [e["kind"] for e in pool_store.events("600001.SH")]
    assert kinds == ["entry", "relimit"]
    pool_store.update_member("600001.SH", "2026-10-22", status="removed", removed_date="2026-10-23")
    assert pool_store.open_member("600001.SH") is None
    assert not pool_store.theme_context("2026-10-23")["available"]
    sys.modules.pop("pool_store", None)
    sys.modules.pop("db_core", None)
    os.environ.pop("ALPHA_SQLITE_DIR", None)


def test_track_member_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("ALPHA_DB_BACKEND", "sqlite")
    monkeypatch.setenv("ALPHA_SQLITE_DIR", str(tmp_path))
    for mod in ("db_core", "pool_store", "pool_data", "pool_track"):
        sys.modules.pop(mod, None)
    import pool_data
    import pool_store
    import pool_track

    bars, member = make_bars([
        (11.5, 12.5, 11.4, 12.4, 400000),
        (12.3, 12.4, 11.8, 11.9, 200000),
        (11.9, 12.0, 11.5, 11.6, 150000),
        (11.5, 11.6, 11.1, 11.3, 60000),
    ])
    asof = bars[-1]["trade_date"]
    monkeypatch.setattr(pool_data, "bars", lambda code, start, end: {"source": "test", "rows": bars})
    pool_store.ensure_schema()
    pool_store.add_member(dict(member))
    stored = pool_store.open_member("600001.SH")

    out = pool_track.track_member(stored, asof, RULES, True, {})
    assert out["decision"]["to"] == "active"
    assert [s["type"] for s in out["signals"]] == ["pullback"]
    pool_track.track_member(pool_store.open_member("600001.SH"), asof, RULES, True, {})  # 重跑幂等
    kinds = [e["kind"] for e in pool_store.events("600001.SH")]
    assert kinds == ["entry", "signal"]

    # 次日跌破涨停日最低价 → 自动移除
    bars.append(dict(trade_date=_day(30), open=11.0, high=11.0, low=9.9, close=10.0, pre_close=11.3,
                     pct_chg=-11.5, amount=300000, adj_factor=1.0))
    out = pool_track.track_member(pool_store.open_member("600001.SH"), _day(30), RULES, True, {})
    assert out["decision"]["to"] == "removed"
    assert pool_store.open_member("600001.SH") is None
    removed = pool_store.list_members(("removed",))[0]
    assert removed["removed_date"] == _day(30) and "涨停日最低价" in removed["removed_reason"]
    for mod in ("db_core", "pool_store", "pool_data", "pool_track"):
        sys.modules.pop(mod, None)
