from __future__ import annotations

from decimal import Decimal
import json

from kalshi_weather.clients import HttpRequestError, KalshiPublicClient, KalshiPublicClientError
from kalshi_weather.engines import reconcile_shadow_position
from kalshi_weather.storage import SQLiteStateStore


def main() -> None:
    store = SQLiteStateStore("data/state/runtime.sqlite3")
    client = KalshiPublicClient()
    positions = store.list_shadow_position_payloads()
    results = []
    city_reports: dict[str, dict[str, object]] = {}
    for payload in positions:
        if payload.get("lifecycle_status") != "OPEN":
            continue
        city_id = str(payload.get("city_id") or "")
        market_ticker = str(payload.get("market_ticker") or "")
        city_reports.setdefault(
            city_id,
            {
                "city_id": city_id,
                "open_position_count": 0,
                "settled_position_count": 0,
                "total_settled_pnl_dollars": "0",
                "results": [],
            },
        )
        city_reports[city_id]["open_position_count"] = int(city_reports[city_id]["open_position_count"]) + 1
        market_payload = None
        fetch_notes: list[str] = []
        try:
            market = client.get_market(market_ticker)
            market_payload = market.get("market") or market
        except (HttpRequestError, KalshiPublicClientError) as exc:
            fetch_notes.append(f"market_fetch_failed:{exc.__class__.__name__}")
        result = reconcile_shadow_position(
            store,
            city_id=city_id,
            market_payload=market_payload,
        )
        notes = tuple(result.notes) + tuple(fetch_notes)
        if result.settled:
            city_reports[city_id]["settled_position_count"] = int(city_reports[city_id]["settled_position_count"]) + 1
            total_pnl = Decimal(str(city_reports[city_id]["total_settled_pnl_dollars"]))
            city_reports[city_id]["total_settled_pnl_dollars"] = str(
                result.total_settled_pnl_dollars + total_pnl
            )
        result_payload = {
            "city_id": city_id,
            "market_ticker": result.market_ticker,
            "settled": result.settled,
            "total_settled_pnl_dollars": str(result.total_settled_pnl_dollars),
            "updated_fill_count": result.updated_fill_count,
            "notes": notes,
        }
        city_reports[city_id]["results"].append(result_payload)
        results.append(
            result_payload
        )
    print(json.dumps({"results": results, "city_reports": city_reports}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
