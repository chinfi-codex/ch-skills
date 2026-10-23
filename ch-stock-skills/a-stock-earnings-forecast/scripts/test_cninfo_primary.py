from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent))

import cninfo_client  # noqa: E402
import cninfo_enrich  # noqa: E402


class CninfoDiscoveryTests(unittest.TestCase):
    def test_announcement_epoch_is_always_interpreted_in_beijing(self) -> None:
        # This epoch is 2026-07-18 00:00 in Shanghai but still 07-17 in UTC.
        with patch.object(
            cninfo_client.time,
            "localtime",
            return_value=time.struct_time((2026, 7, 17, 16, 0, 0, 4, 198, 0)),
        ):
            self.assertEqual(cninfo_client._epoch_to_date(1784304000000), "2026-07-18")

    def test_period_title_variants_and_noise(self) -> None:
        period = "20260630"
        self.assertTrue(cninfo_client.is_forecast_announcement_title(
            "厦门信达股份有限公司二〇二六年半年度业绩预告", period,
        ))
        self.assertTrue(cninfo_client.is_forecast_announcement_title(
            "首开股份2026年上半年业绩预告", period,
        ))
        self.assertTrue(cninfo_client.is_forecast_announcement_title(
            "九牧王2026年半度业绩预减的公告", period,
        ))
        self.assertTrue(cninfo_client.is_forecast_announcement_title(
            "样例公司2026年1-6月业绩预告", period,
        ))
        self.assertFalse(cninfo_client.is_forecast_announcement_title(
            "2026年半年度业绩快报", period,
        ))

    def test_security_code_mapping_rejects_non_equity_prefixes(self) -> None:
        self.assertEqual(cninfo_client.to_ts_code("300394"), "300394.SZ")
        self.assertEqual(cninfo_client.to_ts_code("688205"), "688205.SH")
        self.assertEqual(cninfo_client.to_ts_code("920809"), "920809.BJ")
        self.assertIsNone(cninfo_client.to_ts_code("110059"))  # 沪市转债
        self.assertIsNone(cninfo_client.to_ts_code("500001"))  # 基金

    @patch("cninfo_client._post_json")
    def test_category_discovery_keeps_a_share_forecast_only(self, post_json) -> None:
        post_json.return_value = {
            "announcements": [
                {"announcementId": "1", "secCode": "300394", "secName": "天孚通信",
                 "announcementTitle": "2026年<em>半年度</em>业绩预告",
                 "announcementTime": 1784304000000, "adjunctUrl": "finalpage/a.pdf"},
                {"announcementId": "2", "secCode": "200512", "secName": "闽灿坤B",
                 "announcementTitle": "2026年半年度业绩预告",
                 "announcementTime": 1784304000000, "adjunctUrl": "finalpage/b.pdf"},
                {"announcementId": "3", "secCode": "600131", "secName": "国网信通",
                 "announcementTitle": "2026年半年度业绩快报",
                 "announcementTime": 1784304000000, "adjunctUrl": "finalpage/c.pdf"},
            ]
        }
        rows = cninfo_client.list_forecast_announcements("20260630", "2026-07-18~2026-07-18")
        self.assertEqual([row["ts_code"] for row in rows], ["300394.SZ"])
        self.assertEqual(rows[0]["title"], "2026年半年度业绩预告")


class CninfoParsingTests(unittest.TestCase):
    def test_parent_kf_and_yoy_become_forecast_row(self) -> None:
        text = """
        单位：万元
        归属于上市公司股东的净利润 盈利：112,405.16万元–130,389.99万元
        比上年同期增长：25.00%-45.00%
        扣除非经常性损益后的净利润 盈利：108,905.16万元–128,389.99万元
        比上年同期增长：25.56%-48.02%
        三、业绩变动原因说明：本期高速光器件需求增长。
        四、其他相关说明
        """
        rec = {
            "ts_code": "300394.SZ", "found": True,
            "announcement": {"ann_date": "20260718", "title": "2026年半年度业绩预告"},
            "parsed": cninfo_enrich.parse_forecast_text(text, "2026年半年度业绩预告"),
        }
        row = cninfo_enrich.forecast_row_from_enrich(rec, "20260630")
        self.assertIsNotNone(row)
        self.assertAlmostEqual(row["net_profit_min"], 112405.0, places=2)
        self.assertAlmostEqual(row["net_profit_max"], 130390.0, places=2)
        self.assertEqual(row["p_change_min"], 25.0)
        self.assertEqual(row["p_change_max"], 45.0)
        self.assertIn("高速光器件需求增长", row["change_reason"])

    def test_unsigned_loss_table_keeps_negative_sign(self) -> None:
        parsed = cninfo_enrich.parse_forecast_text(
            "单位：万元 归属于上市公司股东的净利润 亏损：2,500万元至3,000万元 "
            "扣除非经常性损益后的净利润 亏损：3,100万元至3,600万元",
            "2026年半年度业绩预告",
        )
        self.assertEqual(parsed["parent_net_yi"]["low"], -0.3)
        self.assertEqual(parsed["parent_net_yi"]["high"], -0.25)
        self.assertEqual(parsed["kf_net_profit_yi"]["low"], -0.36)
        self.assertEqual(parsed["kf_net_profit_yi"]["high"], -0.31)


class ParentBaseParsingTests(unittest.TestCase):
    """Prior-year base layouts taken from real 2026Q3 forecast PDFs."""

    def test_szse_table_with_units_yields_validated_base(self) -> None:
        parsed = cninfo_enrich.parse_forecast_text(
            "证券代码：000626 证券简称：远大控股 项目 本报告期 上年同期 "
            "归属于上市公司股东的净利润 盈利：31,856 万元–35,977 万元 盈利：3,218 万元 "
            "比上年同期增长：890%–1,018% 扣除非经常性损益后的净利润 亏损：825 万元–1,587 万元",
            "2026年前三季度业绩预告",
        )
        self.assertEqual(parsed["parent_yoy_pct"]["high"], 1018.0)  # thousands separator
        self.assertEqual(parsed["parent_base_yi"]["value"], 0.3218)
        self.assertEqual(parsed["parent_base_yi"]["confidence"], "high")
        self.assertEqual(parsed["sec_name"], "远大控股")
        row = cninfo_enrich.forecast_row_from_enrich({
            "ts_code": "000626.SZ", "found": True, "parsed": parsed,
            "announcement": {"ann_date": "20261015", "title": "2026年前三季度业绩预告"},
        }, "20260930")
        self.assertAlmostEqual(row["last_parent_net"], 3218.0, places=2)

    def test_run_together_unitless_cells_split_on_two_decimals(self) -> None:
        parsed = cninfo_enrich.parse_forecast_text(
            "单位：万元 归属于上市公司股东的净利润 36,314.00～46,314.0019,218.33"
            "比上年同期增长88.96%～140.99%",
            "2026年前三季度业绩预告",
        )
        self.assertEqual(parsed["parent_base_yi"]["value"], 1.9218)
        self.assertEqual(parsed["parent_base_yi"]["confidence"], "high")

    def test_dash_range_and_fullwidth_yoy_dash(self) -> None:
        parsed = cninfo_enrich.parse_forecast_text(
            "归属于上市公司股东的净利润 盈利：55600万元–64200万元 盈利：42810.59万元 "
            "比上年同期增长：30%－50%",
            "2026年前三季度业绩预告",
        )
        self.assertEqual((parsed["parent_yoy_pct"]["low"], parsed["parent_yoy_pct"]["high"]), (30.0, 50.0))
        self.assertEqual(parsed["parent_base_yi"]["value"], 4.2811)

    def test_sse_prior_period_section(self) -> None:
        parsed = cninfo_enrich.parse_forecast_text(
            "预计归属于上市公司股东的净利润约为人民币1,310.00亿元至1,325.00亿元。"
            "二、上年同期业绩情况和财务状况 公司2025年前三季度营业收入为320.84亿元，"
            "归属于母公司所有者的净利润为-52.80亿元。",
            "2026年前三季度业绩预告",
        )
        self.assertEqual(parsed["parent_base_yi"]["value"], -52.8)
        self.assertEqual(parsed["parent_base_yi"]["source"], "prior_period_section")

    def test_sse_change_prose_derives_base_from_both_ends(self) -> None:
        parsed = cninfo_enrich.parse_forecast_text(
            "预计2026年前三季度实现归属于母公司所有者的净利润362,000万元到368,000万元，"
            "与上年同期（法定披露数据）相比，将增加208,000万元到214,000万元。",
            "2026年前三季度业绩预增公告",
        )
        self.assertEqual(parsed["parent_base_yi"]["value"], 15.4)
        self.assertEqual(parsed["parent_base_yi"]["source"], "derived_from_change")

    def test_base_contradicting_disclosed_yoy_is_dropped(self) -> None:
        parsed = cninfo_enrich.parse_forecast_text(
            "归属于上市公司股东的净利润 盈利：10,000万元–12,000万元 盈利：1,000万元 "
            "比上年同期增长：20%–40%",
            "2026年前三季度业绩预告",
        )
        self.assertIsNone(parsed["parent_base_yi"])

    def test_restructured_extra_column_is_ambiguous(self) -> None:
        parsed = cninfo_enrich.parse_forecast_text(
            "单位：万元 上年同期（重组前）上年同期（重组后）归属于上市公司股东的净利润"
            "25,000.00～29,000.006,402.9316,575.74比上年同期（重组前）增长290.45%～352.92%",
            "2026年前三季度业绩预告",
        )
        self.assertIsNone(parsed["parent_base_yi"])


if __name__ == "__main__":
    unittest.main()
