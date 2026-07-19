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


if __name__ == "__main__":
    unittest.main()
