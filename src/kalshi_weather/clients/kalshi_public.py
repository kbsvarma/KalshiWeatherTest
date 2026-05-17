from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .http import http_get_json


PUBLIC_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiPublicClientError(RuntimeError):
    """Raised when public Kalshi data cannot be fetched safely."""


class KalshiPublicClient:
    def __init__(self, base_url: str = PUBLIC_API_BASE) -> None:
        self.base_url = base_url.rstrip("/")

    def get_series(self, series_ticker: str) -> Mapping[str, Any]:
        return http_get_json(f"{self.base_url}/series/{series_ticker}")

    def get_historical_cutoff(self) -> Mapping[str, Any]:
        return http_get_json(f"{self.base_url}/historical/cutoff")

    def get_series_fee_changes(
        self, series_ticker: str, show_historical: bool = True
    ) -> Mapping[str, Any]:
        return http_get_json(
            f"{self.base_url}/series/fee_changes",
            params={
                "series_ticker": series_ticker,
                "show_historical": str(show_historical).lower(),
            },
        )

    def list_markets(
        self,
        series_ticker: str,
        status: str = "open",
        limit: int = 100,
        cursor: str | None = None,
    ) -> Mapping[str, Any]:
        params: dict[str, Any] = {
            "series_ticker": series_ticker,
            "status": status,
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        return http_get_json(f"{self.base_url}/markets", params=params)

    def get_market(self, market_ticker: str) -> Mapping[str, Any]:
        return http_get_json(f"{self.base_url}/markets/{market_ticker}")

    def get_market_orderbook(self, market_ticker: str) -> Mapping[str, Any]:
        return http_get_json(f"{self.base_url}/markets/{market_ticker}/orderbook")

    def batch_get_market_candlesticks(
        self,
        market_tickers: list[str],
        *,
        start_ts: int,
        end_ts: int,
        period_interval: int,
    ) -> Mapping[str, Any]:
        return http_get_json(
            f"{self.base_url}/markets/candlesticks",
            params={
                "market_tickers": ",".join(market_tickers),
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": period_interval,
            },
        )

    def list_trades(
        self,
        ticker: str | None = None,
        limit: int = 100,
        cursor: str | None = None,
        min_ts: int | None = None,
        max_ts: int | None = None,
    ) -> Mapping[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if cursor:
            params["cursor"] = cursor
        if min_ts is not None:
            params["min_ts"] = min_ts
        if max_ts is not None:
            params["max_ts"] = max_ts
        return http_get_json(f"{self.base_url}/markets/trades", params=params)

    def list_recent_settled_markets(
        self,
        series_ticker: str,
        limit: int = 100,
        cursor: str | None = None,
    ) -> Mapping[str, Any]:
        params: dict[str, Any] = {
            "series_ticker": series_ticker,
            "status": "settled",
            "limit": limit,
        }
        if cursor:
            params["cursor"] = cursor
        return http_get_json(f"{self.base_url}/markets", params=params)

    def iter_recent_settled_markets(
        self,
        series_ticker: str,
        max_records: int = 100,
    ) -> list[Mapping[str, Any]]:
        results: list[Mapping[str, Any]] = []
        cursor: str | None = None
        while len(results) < max_records:
            payload = self.list_recent_settled_markets(
                series_ticker=series_ticker,
                limit=min(100, max_records - len(results)),
                cursor=cursor,
            )
            markets = payload.get("markets", [])
            if not isinstance(markets, list):
                raise KalshiPublicClientError("invalid markets payload")
            results.extend(markets)
            cursor = payload.get("cursor")
            if not cursor or not markets:
                break
        return results[:max_records]

    def iter_open_markets(
        self,
        series_ticker: str,
        max_records: int = 200,
    ) -> list[Mapping[str, Any]]:
        results: list[Mapping[str, Any]] = []
        cursor: str | None = None
        while len(results) < max_records:
            payload = self.list_markets(
                series_ticker=series_ticker,
                status="open",
                limit=min(100, max_records - len(results)),
                cursor=cursor,
            )
            markets = payload.get("markets", [])
            if not isinstance(markets, list):
                raise KalshiPublicClientError("invalid markets payload")
            results.extend(markets)
            cursor = payload.get("cursor")
            if not cursor or not markets:
                break
        return results[:max_records]

    def iter_recent_trades(
        self,
        market_ticker: str,
        max_records: int = 200,
        min_ts: int | None = None,
    ) -> list[Mapping[str, Any]]:
        results: list[Mapping[str, Any]] = []
        cursor: str | None = None
        while len(results) < max_records:
            payload = self.list_trades(
                ticker=market_ticker,
                limit=min(1000, max_records - len(results)),
                cursor=cursor,
                min_ts=min_ts,
            )
            trades = payload.get("trades", [])
            if not isinstance(trades, list):
                raise KalshiPublicClientError("invalid trades payload")
            results.extend(trades)
            cursor = payload.get("cursor")
            if not cursor or not trades:
                break
        return results[:max_records]

    @staticmethod
    def market_close_date(market_payload: Mapping[str, Any]) -> datetime | None:
        close_time = market_payload.get("close_time")
        if not close_time:
            return None
        return datetime.fromisoformat(str(close_time).replace("Z", "+00:00"))
