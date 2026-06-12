from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


USER_AGENT = "kalshi-weather-phase1/0.1"
RETRYABLE_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


class HttpRequestError(RuntimeError):
    """Raised when an HTTP request fails."""

    def __init__(
        self,
        method: str,
        url: str,
        status_code: int,
        body: str,
        *,
        attempt_count: int,
        retryable: bool,
    ) -> None:
        super().__init__(f"{method} {url} failed with status {status_code}")
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body
        self.attempt_count = attempt_count
        self.retryable = retryable


def _retry_after_seconds(error: HTTPError) -> float | None:
    retry_after = error.headers.get("Retry-After")
    if retry_after is None:
        return None
    try:
        return max(0.0, float(retry_after))
    except ValueError:
        return None


def http_request_text(
    base_url: str,
    *,
    method: str = "GET",
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    payload: Mapping[str, Any] | str | bytes | None = None,
    # 2026-05-23: dropped 20→8 sec. With 3 attempts + backoff the worst-case
    # per endpoint is 8+0.5+8+1+8 = ~25s vs the old ~62s. With ~50 HTTP calls
    # per cycle across providers, the old budget could plausibly compound to
    # the 41-min hang observed on Mac.
    timeout_seconds: int = 8,
    max_attempts: int = 3,
    backoff_base_seconds: float = 0.5,
    sleep_fn: Callable[[float], None] | None = None,
) -> str:
    url = base_url
    if params:
        url = f"{base_url}?{urlencode(params)}"
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)

    data: bytes | None = None
    if payload is not None:
        if isinstance(payload, bytes):
            data = payload
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            request_headers.setdefault("Content-Type", "application/json")
            data = json.dumps(payload, sort_keys=True).encode("utf-8")

    sleeper = sleep_fn or time.sleep
    normalized_method = method.upper()
    last_error: HttpRequestError | None = None
    for attempt in range(1, max(1, max_attempts) + 1):
        request = Request(url, data=data, method=normalized_method, headers=request_headers)
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            retryable = exc.code in RETRYABLE_STATUS_CODES
            last_error = HttpRequestError(
                normalized_method,
                url,
                exc.code,
                body,
                attempt_count=attempt,
                retryable=retryable,
            )
            if retryable and attempt < max_attempts:
                sleep_seconds = _retry_after_seconds(exc)
                if sleep_seconds is None:
                    sleep_seconds = backoff_base_seconds * (2 ** (attempt - 1))
                sleeper(sleep_seconds)
                continue
            raise last_error from exc
        except (URLError, OSError) as exc:
            last_error = HttpRequestError(
                normalized_method,
                url,
                0,
                str(exc),
                attempt_count=attempt,
                retryable=True,
            )
            if attempt < max_attempts:
                sleeper(backoff_base_seconds * (2 ** (attempt - 1)))
                continue
            raise last_error from exc
    if last_error is not None:
        raise last_error
    raise HttpRequestError(normalized_method, url, 0, "unreachable", attempt_count=0, retryable=False)


def http_get_text(base_url: str, params: Mapping[str, Any] | None = None) -> str:
    return http_request_text(base_url, method="GET", params=params)


def http_get_json(base_url: str, params: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    return json.loads(http_get_text(base_url, params=params))


def http_request_json(
    base_url: str,
    *,
    method: str,
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    payload: Mapping[str, Any] | str | bytes | None = None,
) -> Mapping[str, Any]:
    return json.loads(
        http_request_text(
            base_url,
            method=method,
            params=params,
            headers=headers,
            payload=payload,
        )
    )
