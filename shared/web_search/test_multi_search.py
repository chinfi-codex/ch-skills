from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import multi_search as router


def _ok(results: list[dict]) -> dict:
    return {"query": "q", "results": results}


class ParamMappingTests(unittest.TestCase):
    @patch("multi_search.yuanbao_search.yuanbao_search")
    def test_yuanbao_days_maps_to_freshness_and_cnt_snaps_then_trims(
        self, engine: Mock
    ) -> None:
        engine.return_value = _ok([{"title": f"n{i}", "url": f"u{i}"} for i in range(20)])

        result = router.multi_search(
            "q", engines=["yuanbao"], max_results=15, days=7
        )

        self.assertNotIn("error", result)
        kwargs = engine.call_args.kwargs
        self.assertEqual(kwargs["freshness"], "d7")
        self.assertEqual(kwargs["cnt"], 20)  # 15 向上对齐到合法档位
        self.assertEqual(len(result["results"]), 15)  # 拿回 20 条后裁剪
        self.assertTrue(any("裁剪" in n for n in result["notes"]))

    @patch("multi_search.tavily_search.tavily_search")
    def test_tavily_max_results_clamped(self, engine: Mock) -> None:
        engine.return_value = _ok([])

        result = router.multi_search("q", engines=["tavily"], max_results=50)

        self.assertEqual(engine.call_args.kwargs["max_results"], 20)
        self.assertTrue(any("上限" in n for n in result["notes"]))

    @patch("multi_search.deepseek_search.deepseek_search")
    def test_deepseek_domain_constraints_injected_as_syntax(self, engine: Mock) -> None:
        engine.return_value = _ok([])

        router.multi_search(
            "寒武纪",
            engines=["deepseek"],
            include_domains=["stcn.com"],
            exclude_domains=["weibo.com"],
            days=7,
        )

        query = engine.call_args.args[0]
        self.assertIn("site:stcn.com", query)
        self.assertIn("-site:weibo.com", query)
        kwargs = engine.call_args.kwargs
        self.assertNotIn("days", kwargs)  # deepseek 无时间窗参数
        # days 无处安放必须留痕，而不是悄悄丢掉
        # （notes 由 _run_engine 生成，见下一条断言）

    def test_deepseek_time_window_is_disclosed_not_dropped(self) -> None:
        with patch.object(
            router.deepseek_search, "deepseek_search", return_value=_ok([])
        ) as engine:
            result = router.multi_search("q", engines=["deepseek"], days=7)

        self.assertTrue(
            any("未按时间过滤" in n for n in result["notes"]),
            f"notes={result.get('notes')}",
        )
        engine.assert_called_once()

    def test_deepseek_domain_syntax_overflow_skips_engine(self) -> None:
        long_query = "字" * 120
        with patch.object(
            router.deepseek_search, "deepseek_search"
        ) as engine:
            result = router.multi_search(
                long_query, engines=["deepseek"], include_domains=["a.com", "b.com"]
            )

        engine.assert_not_called()
        self.assertEqual(result["error"]["code"], "all_engines_failed")
        self.assertIn("skipped", result["error"]["detail"]["deepseek"])

    def test_yuanbao_cannot_mix_include_and_exclude(self) -> None:
        with patch.object(router.yuanbao_search, "yuanbao_search") as engine:
            result = router.multi_search(
                "q",
                engines=["yuanbao"],
                include_domains=["a.com"],
                exclude_domains=["b.com"],
            )

        engine.assert_not_called()
        self.assertIn("二选一", result["error"]["detail"]["yuanbao"]["skipped"])

    def test_yuanbao_include_more_than_one_domain_skips(self) -> None:
        with patch.object(router.yuanbao_search, "yuanbao_search") as engine:
            result = router.multi_search(
                "q", engines=["yuanbao"], include_domains=["a.com", "b.com"]
            )

        engine.assert_not_called()
        self.assertIn("仅支持一个域名", result["error"]["detail"]["yuanbao"]["skipped"])

    def test_yuanbao_single_include_domain_forwarded_as_is(self) -> None:
        with patch.object(
            router.yuanbao_search, "yuanbao_search", return_value=_ok([])
        ) as engine:
            router.multi_search("q", engines=["yuanbao"], include_domains=["qq.com"])

        self.assertEqual(engine.call_args.kwargs["site"], "qq.com")

    def test_yuanbao_meta_propagates_to_payload(self) -> None:
        with patch.object(
            router.yuanbao_search,
            "yuanbao_search",
            return_value={
                "query": "q",
                "results": [{"title": "t", "url": "u"}],
                "meta": {"version": "flagship", "msg": "ok"},
            },
        ):
            result = router.multi_search("q", engines=["yuanbao"])

        self.assertEqual(result["meta"], {"version": "flagship", "msg": "ok"})

    def test_days_and_dates_are_mutually_exclusive(self) -> None:
        result = router.multi_search(
            "q", engines=["tavily"], days=7, start_date="2026-10-01"
        )
        self.assertEqual(result["error"]["code"], "invalid_parameters")


class ExecutionModeTests(unittest.TestCase):
    def test_failover_moves_to_next_engine_on_error(self) -> None:
        with patch.object(
            router.tavily_search, "tavily_search", return_value={"error": {"code": "http_500"}}
        ) as tav, patch.object(
            router.deepseek_search,
            "deepseek_search",
            return_value=_ok([{"title": "t", "url": "u", "published_date": None, "snippet": "s",
                               "summary": "m", "content": "", "site_name": None,
                               "rerank_score": 1.0, "authority": 2}]),
        ) as ds:
            result = router.multi_search("q", engines=["tavily", "deepseek"])

        tav.assert_called_once()
        ds.assert_called_once()
        self.assertEqual(result["engine"], "deepseek")
        self.assertEqual(result["attempts"]["tavily"]["error"]["code"], "http_500")
        self.assertEqual(
            set(result["results"][0]),
            {"title", "url", "published_date", "snippet", "content",
             "summary", "site_name", "rerank_score", "authority"},
        )

    def test_failover_falls_through_empty_results(self) -> None:
        with patch.object(
            router.tavily_search, "tavily_search", return_value=_ok([])
        ) as tav, patch.object(
            router.deepseek_search,
            "deepseek_search",
            return_value=_ok([{"title": "t", "url": "u", "published_date": None, "snippet": "s",
                               "summary": "m", "content": "", "site_name": None,
                               "rerank_score": 1.0, "authority": 2}]),
        ) as ds:
            result = router.multi_search("q", engines=["tavily", "deepseek"])

        ds.assert_called_once()
        self.assertEqual(result["engine"], "deepseek")
        self.assertIn("tavily", result["attempts"])

    def test_failover_all_failed_is_structured(self) -> None:
        with patch.object(
            router.tavily_search, "tavily_search", return_value={"error": {"code": "http_500"}}
        ), patch.object(
            router.deepseek_search, "deepseek_search", return_value={"error": {"code": "http_401"}}
        ):
            result = router.multi_search("q", engines=["tavily", "deepseek"])

        self.assertEqual(result["error"]["code"], "all_engines_failed")
        self.assertEqual(result["error"]["detail"]["tavily"]["error"]["code"], "http_500")
        self.assertEqual(result["error"]["detail"]["deepseek"]["error"]["code"], "http_401")

    def test_fanout_runs_all_engines_and_isolates_failures(self) -> None:
        with patch.object(
            router.tavily_search, "tavily_search", return_value=_ok([{"title": "a", "url": "ua"}])
        ) as tav, patch.object(
            router.deepseek_search, "deepseek_search", return_value={"error": {"code": "http_401"}}
        ) as ds, patch.object(
            router.yuanbao_search, "yuanbao_search", return_value=_ok([{"title": "b", "url": "ub"}])
        ) as yb:
            result = router.multi_search(
                "q", engines=["tavily", "deepseek", "yuanbao"], mode="fanout"
            )

        self.assertEqual(tav.call_count, 1)
        self.assertEqual(ds.call_count, 1)
        self.assertEqual(yb.call_count, 1)
        self.assertEqual(result["mode"], "fanout")
        self.assertEqual(len(result["engines"]["tavily"]["results"]), 1)
        self.assertEqual(
            result["engines"]["deepseek"]["error"]["code"], "http_401"
        )
        self.assertEqual(len(result["engines"]["yuanbao"]["results"]), 1)
        self.assertNotIn("error", result)


class CliParseTests(unittest.TestCase):
    def test_engine_string_is_validated(self) -> None:
        with self.assertRaises(argparse.ArgumentTypeError):
            router._parse_engines("bing")
        self.assertEqual(router._parse_engines("all"), ["tavily", "deepseek", "yuanbao"])
        self.assertEqual(
            router._parse_engines("tavily, deepseek"), ["tavily", "deepseek"]
        )

    def test_main_forces_fanout_for_all(self) -> None:
        with patch.object(router, "multi_search", return_value=_ok([])) as ms:
            router.main(["q", "--engine", "all"])
        self.assertEqual(ms.call_args.kwargs["mode"], "fanout")
        self.assertEqual(ms.call_args.kwargs["engines"], ["tavily", "deepseek", "yuanbao"])


if __name__ == "__main__":
    unittest.main()
