from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import http_utils  # noqa: E402


def make_opener(script):
    """Fake transport: plays back scripted (status, headers, body) tuples or
    raises scripted exceptions. Returns (opener, calls)."""
    calls = []

    def opener(url, *, method, headers, body, timeout):
        calls.append({"url": url, "method": method, "headers": dict(headers)})
        index = min(len(calls) - 1, len(script) - 1)
        step = script[index]
        if isinstance(step, Exception):
            raise step
        status, resp_headers, resp_body = step
        return http_utils.RawResponse(status=status, headers=resp_headers, body=resp_body)

    return opener, calls


class FakeSleep:
    def __init__(self):
        self.delays = []

    def __call__(self, seconds):
        self.delays.append(seconds)


class FetchRetryTests(unittest.TestCase):
    def test_success_on_first_attempt(self):
        opener, calls = make_opener([(200, {}, "hello")])
        text = http_utils.fetch_text("http://x/", opener=opener, sleep=FakeSleep())
        self.assertEqual(text, "hello")
        self.assertEqual(len(calls), 1)

    def test_server_error_retries_with_exponential_backoff(self):
        opener, calls = make_opener(
            [(500, {}, "boom"), (500, {}, "boom"), (200, {}, "ok")]
        )
        sleep = FakeSleep()
        text = http_utils.fetch_text("http://x/", opener=opener, sleep=sleep)
        self.assertEqual(text, "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(sleep.delays), 2)
        # 1s then 2s base, plus up to 0.75s jitter on each.
        self.assertTrue(1.0 <= sleep.delays[0] < 1.76)
        self.assertTrue(2.0 <= sleep.delays[1] < 2.76)

    def test_gives_up_after_max_attempts(self):
        opener, calls = make_opener([(500, {}, "boom")] * 10)
        result = http_utils.fetch_safe("http://x/", opener=opener, sleep=FakeSleep())
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "server_error")
        self.assertEqual(len(calls), 4)

    def test_429_honours_retry_after(self):
        opener, calls = make_opener(
            [(429, {"Retry-After": "5"}, "slow down"), (200, {}, "ok")]
        )
        sleep = FakeSleep()
        text = http_utils.fetch_text("http://x/", opener=opener, sleep=sleep)
        self.assertEqual(text, "ok")
        self.assertEqual(sleep.delays, [5.0])

    def test_429_has_own_retry_budget(self):
        opener, calls = make_opener([(429, {}, "slow down")] * 10)
        sleep = FakeSleep()
        result = http_utils.fetch_safe("http://x/", opener=opener, sleep=sleep)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "rate_limited")
        # initial attempt + 2 rate-limit retries, then stop despite max_attempts=4
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(sleep.delays), 2)

    def test_other_4xx_not_retried(self):
        for status, category in (
            (401, "unauthorized"),
            (403, "forbidden"),
            (404, "not_found"),
            (400, "unknown"),
        ):
            opener, calls = make_opener([(status, {}, "no")] * 5)
            sleep = FakeSleep()
            result = http_utils.fetch_safe("http://x/", opener=opener, sleep=sleep)
            self.assertFalse(result.ok, status)
            self.assertEqual(result.error, category, status)
            self.assertEqual(len(calls), 1, status)
            self.assertEqual(sleep.delays, [], status)

    def test_timeout_uses_independent_network_budget(self):
        opener, calls = make_opener(
            [
                http_utils.TransportTimeout("t1"),
                http_utils.TransportTimeout("t2"),
                http_utils.TransportTimeout("t3"),
                (200, {}, "ok"),
            ]
        )
        sleep = FakeSleep()
        result = http_utils.fetch_safe("http://x/", opener=opener, sleep=sleep)
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "timeout")
        # initial + 2 network retries, then stop
        self.assertEqual(len(calls), 3)

    def test_connection_error_then_success(self):
        opener, calls = make_opener(
            [http_utils.TransportError("dns failed"), (200, {}, "ok")]
        )
        result = http_utils.fetch_safe("http://x/", opener=opener, sleep=FakeSleep())
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "ok")
        self.assertEqual(len(calls), 2)

    def test_fetch_response_raises_typed_error(self):
        opener, _ = make_opener([(403, {}, "no")])
        with self.assertRaises(http_utils.FetchError) as ctx:
            http_utils.fetch_response("http://x/", opener=opener, sleep=FakeSleep())
        self.assertEqual(ctx.exception.category, "forbidden")
        self.assertEqual(ctx.exception.status, 403)
        self.assertIn("forbidden", str(ctx.exception))

    def test_fetch_safe_never_raises_on_opener_bug(self):
        def broken_opener(url, *, method, headers, body, timeout):
            raise ValueError("not a transport error")

        result = http_utils.fetch_safe(
            "http://x/", opener=broken_opener, sleep=FakeSleep()
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error, "unknown")
        self.assertIn("ValueError", result.error_message)

    def test_fetch_json_parses_body(self):
        opener, _ = make_opener([(200, {}, '{"a": 1}')])
        payload = http_utils.fetch_json("http://x/", opener=opener, sleep=FakeSleep())
        self.assertEqual(payload, {"a": 1})

    def test_post_method_and_default_user_agent(self):
        opener, calls = make_opener([(200, {}, "ok")])

        def check(url, *, method, headers, body, timeout):
            self.assertEqual(method, "POST")
            self.assertEqual(body, b"{}")
            self.assertIn("User-Agent", headers)
            return http_utils.RawResponse(status=200, headers={}, body="ok")

        http_utils.fetch_text("http://x/", body=b"{}", opener=check, sleep=FakeSleep())
        self.assertEqual(len(calls), 0)  # check opener used, not make_opener's


class TokenBucketTests(unittest.TestCase):
    def make_bucket(self, rate, burst):
        state = {"now": 0.0}
        sleeps = []

        def clock():
            return state["now"]

        def sleep(seconds):
            sleeps.append(seconds)
            state["now"] += seconds

        bucket = http_utils.TokenBucket(rate, burst, clock=clock, sleep=sleep)
        return bucket, state, sleeps

    def test_burst_then_paced(self):
        bucket, state, sleeps = self.make_bucket(rate=2.0, burst=2)
        bucket.acquire()
        bucket.acquire()
        self.assertEqual(sleeps, [])  # burst served immediately
        bucket.acquire()  # must wait 0.5s for one token at 2/s
        self.assertEqual(sleeps, [0.5])
        self.assertAlmostEqual(state["now"], 0.5)

    def test_tokens_refill_over_time(self):
        bucket, state, sleeps = self.make_bucket(rate=1.0, burst=1)
        bucket.acquire()
        state["now"] += 3.0  # 3s elapsed -> capped at capacity 1
        bucket.acquire()
        self.assertEqual(sleeps, [])

    def test_thread_safety(self):
        bucket = http_utils.TokenBucket(1000.0, 50)
        threads = [threading.Thread(target=bucket.acquire) for _ in range(50)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_limiter_registry_shares_and_configures(self):
        first = http_utils.get_limiter("example.com")
        second = http_utils.get_limiter("example.com")
        self.assertIs(first, second)
        replaced = http_utils.configure_limiter("example.com", 5.0, burst=3)
        self.assertIs(http_utils.get_limiter("example.com"), replaced)
        self.assertEqual(replaced.rate, 5.0)
        self.assertEqual(replaced.capacity, 3.0)


if __name__ == "__main__":
    unittest.main()
