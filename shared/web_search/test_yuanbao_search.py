from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import yuanbao_search as search


def _mock_response(body: dict) -> Mock:
    response = Mock(status_code=200)
    response.json.return_value = body
    return response


def _page(**overrides: object) -> str:
    page = {
        "title": "寒武纪发布业绩预告",
        "date": "2026-10-20",
        "url": "https://example.com/news/1",
        "passage": "passage text",
        "content": "content text",
        "site": "example.com",
        "score": 0.87,
    }
    page.update(overrides)
    return json.dumps(page, ensure_ascii=False)


class YuanbaoSearchContractTests(unittest.TestCase):
    @patch("requests.post")
    def test_pages_json_strings_are_normalized(self, post: Mock) -> None:
        post.return_value = _mock_response(
            {"Response": {"Query": "寒武纪", "Pages": [_page()], "RequestId": "req-1"}}
        )

        result = search.yuanbao_search(
            "寒武纪", cnt=20, secret_id="id-x", secret_key="key-x"
        )

        self.assertNotIn("error", result)
        self.assertEqual(set(result), {"query", "results"})
        self.assertEqual(
            set(result["results"][0]),
            {"title", "url", "published_date", "content", "site", "score"},
        )
        self.assertEqual(result["results"][0]["published_date"], "2026-10-20")
        self.assertEqual(result["results"][0]["content"], "content text")

        payload = json.loads(post.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual(payload, {"Query": "寒武纪", "Cnt": 20})
        headers = post.call_args.kwargs["headers"]
        self.assertEqual(headers["X-TC-Action"], "SearchPro")
        self.assertEqual(headers["X-TC-Version"], "2025-05-08")
        self.assertTrue(headers["Authorization"].startswith("TC3-HMAC-SHA256 "))

    @patch("requests.post")
    def test_content_falls_back_to_passage(self, post: Mock) -> None:
        post.return_value = _mock_response(
            {"Response": {"Pages": [_page(content="")]}}
        )

        result = search.yuanbao_search("寒武纪", secret_id="id-x", secret_key="key-x")

        self.assertEqual(result["results"][0]["content"], "passage text")

    @patch("requests.post")
    def test_optional_filters_are_forwarded(self, post: Mock) -> None:
        post.return_value = _mock_response({"Response": {"Pages": []}})

        search.yuanbao_search(
            "光模块 订单",
            mode=2,
            site="exclude:qq.com|sohu.com",
            industry="finance",
            freshness="d7",
            secret_id="id-x",
            secret_key="key-x",
        )

        payload = json.loads(post.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual(
            payload,
            {
                "Query": "光模块 订单",
                "Mode": 2,
                "Site": "exclude:qq.com|sohu.com",
                "Industry": "finance",
                "Freshness": "d7",
            },
        )

    @patch("requests.post")
    def test_freshness_must_match_api_grammar(self, post: Mock) -> None:
        for bad in ("d31", "m13", "y6", "2026-13-01", "2026/01/01"):
            with self.subTest(freshness=bad):
                result = search.yuanbao_search(
                    "query", freshness=bad, secret_id="id-x", secret_key="key-x"
                )
                self.assertEqual(result["error"]["code"], "invalid_parameters")
        post.assert_not_called()

    @patch("requests.post")
    def test_site_accepts_at_most_five_domains(self, post: Mock) -> None:
        post.return_value = _mock_response({"Response": {"Pages": []}})

        ok = search.yuanbao_search(
            "query",
            site="exclude:a.com|b.com|c.com|d.com|e.com",
            secret_id="id-x",
            secret_key="key-x",
        )
        self.assertNotIn("error", ok)

        too_many = search.yuanbao_search(
            "query",
            site="a.com|b.com|c.com|d.com|e.com|f.com",
            secret_id="id-x",
            secret_key="key-x",
        )
        self.assertEqual(too_many["error"]["code"], "invalid_parameters")
        post.assert_called_once()

    @patch("requests.post")
    def test_api_error_is_passed_through(self, post: Mock) -> None:
        post.return_value = _mock_response(
            {
                "Response": {
                    "Error": {
                        "Code": "ResourceNotFound",
                        "Message": "未开通元宝搜索服务",
                    },
                    "RequestId": "req-2",
                }
            }
        )

        result = search.yuanbao_search("query", secret_id="id-x", secret_key="key-x")

        self.assertEqual(result["error"]["code"], "ResourceNotFound")
        self.assertIn("未开通", result["error"]["detail"])

    @patch("requests.post")
    def test_http_failure_is_structured(self, post: Mock) -> None:
        post.return_value = Mock(status_code=403, text="denied")

        result = search.yuanbao_search("query", secret_id="id-x", secret_key="key-x")

        self.assertEqual(result["error"]["code"], "http_403")

    @patch("yuanbao_search.get_tencent_credentials", return_value=("", ""))
    @patch("requests.post")
    def test_missing_credentials_returns_structured_error(
        self, post: Mock, _creds: Mock
    ) -> None:
        result = search.yuanbao_search("query")

        self.assertEqual(result["error"]["code"], "missing_api_key")
        self.assertIn("TENCENTCLOUD_SECRET_ID", result["error"]["detail"])
        post.assert_not_called()


class Tc3SignatureTests(unittest.TestCase):
    def test_signature_is_deterministic_and_well_formed(self) -> None:
        fixed_ts = 1_760_000_000
        with patch.object(search.time, "time", return_value=fixed_ts):
            first = search.build_tc3_headers('{"Query":"q"}', "sid", "skey", int(search.time.time()))
            second = search.build_tc3_headers('{"Query":"q"}', "sid", "skey", int(search.time.time()))

        self.assertEqual(first, second)
        auth = first["Authorization"]
        self.assertTrue(auth.startswith("TC3-HMAC-SHA256 Credential=sid/"))
        self.assertIn("wsa/tc3_request", auth)
        self.assertIn(
            "SignedHeaders=content-type;host;x-tc-action", auth
        )
        self.assertEqual(first["X-TC-Timestamp"], str(fixed_ts))
        self.assertEqual(first["X-TC-Action"], "SearchPro")

    def test_headers_sign_the_exact_payload_bytes(self) -> None:
        payload = json.dumps({"Query": "寒武纪"}, ensure_ascii=False)
        headers = search.build_tc3_headers(payload, "sid", "skey", 1_760_000_000)

        # 同一 payload 不同签名输入应产生不同签名，证明签名确实覆盖了 payload
        other = search.build_tc3_headers(payload + " ", "sid", "skey", 1_760_000_000)
        self.assertNotEqual(headers["Authorization"], other["Authorization"])


if __name__ == "__main__":
    unittest.main()
