from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cninfo_announcement_search as search  # noqa: E402


class OrgIdLookupTests(unittest.TestCase):
    def test_service_failure_raises_after_retries(self) -> None:
        with patch.object(search.requests, "post", side_effect=requests.ConnectionError("503")) as post, \
                patch.object(search.time, "sleep"):
            with self.assertRaises(search.OrgIdLookupError):
                search.get_cninfo_orgid("300604")
        self.assertEqual(post.call_count, 3)

    def test_unknown_code_is_not_an_error(self) -> None:
        class Resp:
            def raise_for_status(self) -> None:
                pass

            def json(self):
                return []

        with patch.object(search.requests, "post", return_value=Resp()):
            self.assertIsNone(search.get_cninfo_orgid("999999"))


if __name__ == "__main__":
    unittest.main()
