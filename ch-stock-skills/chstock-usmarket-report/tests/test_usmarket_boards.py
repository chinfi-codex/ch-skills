from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
SHARED = SKILL_ROOT.parents[1] / "shared"
sys.path.insert(0, str(SHARED))

SPEC = importlib.util.spec_from_file_location(
    "usmarket_boards", SKILL_ROOT / "scripts" / "usmarket_boards.py"
)
assert SPEC and SPEC.loader
BOARDS = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = BOARDS
SPEC.loader.exec_module(BOARDS)


def payload_missing() -> dict:
    return {"missing": True, "metadata": {}}


def payload_two_indices() -> dict:
    closes = [700.0 + i for i in range(70)]
    records = [
        {"trade_date": f"2026-08-{(i % 28) + 1:02d}", "open": c, "high": c + 2, "low": c - 2, "close": c}
        for i, c in enumerate(closes)
    ]
    return {
        "missing": False,
        "index_snapshots": {
            "QQQ": {
                "ticker": "QQQ", "close": 751.27, "change_pct": 0.4936,
                "five_day_trend_pct": 0.2255, "position_52w": 0.9583, "vol_vs_20d": 0.61,
            },
            "SOXX": {
                "ticker": "SOXX", "close": 559.4, "change_pct": -0.6888,
                "five_day_trend_pct": -5.0093, "position_52w": 0.7529, "vol_vs_20d": 0.74,
            },
        },
        "index_klines": [
            {"ticker": "QQQ", "name": "纳指100", "records": records, "change_pct": 0.4936},
            {"ticker": "SOXX", "name": "半导体ETF", "records": records, "change_pct": -0.6888},
        ],
    }


def ledger() -> list[dict]:
    rows = []
    # 软件：08→09 份额 12%→15.5%（delta +3.5pp），★★，在场
    rows.append({"asof": "2026-10-08", "theme_id": "TH-sw", "name": "软件", "stars": 1, "dollar_vol_share": 0.12, "state": "在场", "in_pool": True})
    rows.append({"asof": "2026-10-09", "theme_id": "TH-sw", "name": "软件", "stars": 2, "dollar_vol_share": 0.155, "state": "在场", "in_pool": True})
    # 存储：09 无份额（None），有历史
    rows.append({"asof": "2026-10-08", "theme_id": "TH-mem", "name": "存储", "stars": 3, "dollar_vol_share": 0.33, "state": "确认主线", "in_pool": True})
    rows.append({"asof": "2026-10-09", "theme_id": "TH-mem", "name": "存储", "stars": None, "dollar_vol_share": None, "state": "退潮", "in_pool": True})
    # 池外新方向：仅 09 当日
    rows.append({"asof": "2026-10-09", "theme_id": "TH-new", "name": "房产平台", "stars": 1, "dollar_vol_share": 0.13, "state": "低位启动", "in_pool": False, "is_new_direction": True})
    return rows


class IndexBoardTests(unittest.TestCase):
    def test_missing_payload_builds_nothing(self) -> None:
        figures, meta = BOARDS.build_usmarket_figures(payload_missing(), [], [], "2026-10-09")
        self.assertEqual(figures, [])
        self.assertEqual(meta, {"index_board": False, "theme_board": False})

    def test_rows_carry_position_trend_volume_and_spark(self) -> None:
        figures, meta = BOARDS.build_usmarket_figures(payload_two_indices(), [], [], "2026-10-09")
        self.assertTrue(meta["index_board"])
        self.assertFalse(meta["theme_board"])
        figure = figures[0]
        self.assertEqual(figure.anchor, ("大盘",))
        self.assertEqual(figure.placement, "section_end")
        html = figure.html
        self.assertIn("QQQ", html)
        self.assertIn("SOXX", html)
        self.assertIn("+0.49", html)   # 当日大数
        self.assertIn("-0.69", html)
        self.assertIn("5日", html)      # 中列徽章
        self.assertIn("量比 0.61", html)  # id 行小字
        self.assertIn("52周 0.96", html)
        self.assertIn("width:95.8%", html)  # 行填充=52周位置
        self.assertEqual(html.count("<svg"), 2)  # 两条走势

    def test_missing_change_renders_dash_without_percent_suffix(self) -> None:
        payload = payload_two_indices()
        payload["index_klines"][0]["change_pct"] = None
        payload["index_snapshots"]["QQQ"]["change_pct"] = None
        figures, _meta = BOARDS.build_usmarket_figures(payload, [], [], "2026-10-09")
        self.assertIn('<div class="ubr-num ">—</div>', figures[0].html)


class ThemeBoardTests(unittest.TestCase):
    def test_sorted_by_share_then_stars_with_nulls_last(self) -> None:
        figures, meta = BOARDS.build_usmarket_figures(payload_missing(), ledger(), ledger(), "2026-10-09")
        self.assertTrue(meta["theme_board"])
        html = figures[0].html
        order = [html.index(name) for name in ("软件", "房产平台", "存储")]
        self.assertEqual(order, sorted(order))  # 15.5% > 13% > None
        self.assertIn("▲ +3.5", html)           # 软件 12%→15.5% 隔日 pp 变动
        self.assertIn("新方向", html)            # 池外新方向标签
        self.assertIn("池外", html)
        self.assertIn("退潮", html)              # 状态 pill
        self.assertIn("★★", html)               # 软件两颗星
        # 存储当日 share=None：大数为 — 且无 % 尾巴，徽章给 —
        self.assertIn(">—</div>", html)

    def test_delta_uses_most_recent_prior_day_with_share(self) -> None:
        rows = [
            {"asof": "2026-10-07", "theme_id": "TH-x", "name": "X", "dollar_vol_share": 0.10},
            # 08 缺份额——不可作基准
            {"asof": "2026-10-08", "theme_id": "TH-x", "name": "X", "dollar_vol_share": None},
            {"asof": "2026-10-09", "theme_id": "TH-x", "name": "X", "dollar_vol_share": 0.13},
        ]
        figures, _ = BOARDS.build_usmarket_figures(payload_missing(), rows[-1:], rows, "2026-10-09")
        self.assertIn("▲ +3", figures[0].html)   # vs 10-07 的 10%
        self.assertIn("较 2026-10-07", figures[0].html)

    def test_three_star_row_gets_hot_border(self) -> None:
        rows = [{"asof": "2026-10-09", "theme_id": "TH-h", "name": "热", "stars": 3, "dollar_vol_share": 0.61, "state": "确认主线", "in_pool": True}]
        figures, _ = BOARDS.build_usmarket_figures(payload_missing(), rows, rows, "2026-10-09")
        self.assertIn('class="ubr hot"', figures[0].html)

    def test_row_cap_and_empty_input(self) -> None:
        rows = [
            {"asof": "2026-10-09", "theme_id": f"TH-{i}", "name": f"主题{i:02d}", "stars": 0, "dollar_vol_share": 0.01 * i, "in_pool": True}
            for i in range(14)
        ]
        figures, meta = BOARDS.build_usmarket_figures(payload_missing(), rows, rows, "2026-10-09")
        self.assertTrue(meta["theme_board"])
        html = figures[0].html
        row_count = html.count('class="ubr"') + html.count('class="ubr hot"')
        self.assertEqual(row_count, 10)
        figures, meta = BOARDS.build_usmarket_figures(payload_missing(), [], rows, "2026-10-09")
        self.assertFalse(meta["theme_board"])

    def test_sparkline_uses_theme_id_across_renames(self) -> None:
        rows = [
            {"asof": "2026-10-08", "theme_id": "TH-r", "name": "旧名字", "dollar_vol_share": 0.05},
            {"asof": "2026-10-09", "theme_id": "TH-r", "name": "新名字", "dollar_vol_share": 0.08},
        ]
        figures, _ = BOARDS.build_usmarket_figures(payload_missing(), rows[-1:], rows, "2026-10-09")
        html = figures[0].html
        self.assertIn("新名字", html)
        self.assertIn("▲ +3", html)   # 改名后历史仍续上
        self.assertIn("<svg", html)


if __name__ == "__main__":
    unittest.main()
