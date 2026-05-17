from __future__ import annotations

from io import BytesIO
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from kalshi_weather.clients.http import HttpRequestError, http_request_text


class _FakeResponse:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> bytes:
        return self._text.encode("utf-8")

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class HttpClientTest(unittest.TestCase):
    def test_http_request_text_retries_retryable_http_error(self) -> None:
        sleeps: list[float] = []
        too_many = HTTPError(
            "https://example.test",
            429,
            "Too Many Requests",
            {"Retry-After": "1"},
            BytesIO(b"rate limited"),
        )
        with patch(
            "kalshi_weather.clients.http.urlopen",
            side_effect=[too_many, _FakeResponse('{"ok": true}')],
        ):
            payload = http_request_text(
                "https://example.test",
                max_attempts=2,
                sleep_fn=sleeps.append,
            )
        self.assertEqual(payload, '{"ok": true}')
        self.assertEqual(sleeps, [1.0])

    def test_http_request_text_raises_after_retryable_network_failure_budget(self) -> None:
        sleeps: list[float] = []
        with patch(
            "kalshi_weather.clients.http.urlopen",
            side_effect=URLError("temporary failure"),
        ):
            with self.assertRaises(HttpRequestError) as caught:
                http_request_text(
                    "https://example.test",
                    max_attempts=2,
                    backoff_base_seconds=0.25,
                    sleep_fn=sleeps.append,
                )
        self.assertEqual(caught.exception.status_code, 0)
        self.assertEqual(caught.exception.attempt_count, 2)
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(sleeps, [0.25])

    def test_http_request_text_does_not_retry_non_retryable_http_error(self) -> None:
        not_found = HTTPError(
            "https://example.test",
            404,
            "Not Found",
            {},
            BytesIO(b"missing"),
        )
        with patch(
            "kalshi_weather.clients.http.urlopen",
            side_effect=not_found,
        ):
            with self.assertRaises(HttpRequestError) as caught:
                http_request_text("https://example.test", max_attempts=3, sleep_fn=lambda _seconds: None)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(caught.exception.attempt_count, 1)
        self.assertFalse(caught.exception.retryable)


if __name__ == "__main__":
    unittest.main()
