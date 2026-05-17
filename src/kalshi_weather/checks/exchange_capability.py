from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Mapping
from urllib.request import Request, urlopen


PUBLIC_API_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ORDERBOOK_DOC_URL = "https://docs.kalshi.com/websockets/orderbook-updates"
SERIES_TICKER = "KXHIGHNY"


@dataclass(frozen=True, slots=True)
class ExchangeCapabilityCheckResult:
    checked_at: datetime
    orderbook_schema_ok: bool
    historical_cutoff: Mapping[str, str] | None
    series_fee_type: str | None
    series_fee_multiplier: int | None
    fee_change_count: int | None
    errors: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["checked_at"] = self.checked_at.isoformat()
        return payload


def _get_json(url: str) -> Mapping[str, Any]:
    request = Request(url, headers={"User-Agent": "kalshi-weather-phase1/0.1"})
    with urlopen(request, timeout=15) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _get_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": "kalshi-weather-phase1/0.1"})
    with urlopen(request, timeout=15) as response:  # noqa: S310
        return response.read().decode("utf-8", errors="replace")


def orderbook_doc_has_required_tokens(document_text: str) -> bool:
    required_tokens = (
        "orderbook_snapshot",
        "orderbook_delta",
        "seq",
        "market_ticker",
        "price_dollars",
        "delta_fp",
        "side",
        "ts",
    )
    return all(token in document_text for token in required_tokens)


def parse_series_metadata(payload: Mapping[str, Any]) -> tuple[str | None, int | None]:
    series = payload.get("series", {})
    fee_type = series.get("fee_type")
    fee_multiplier = series.get("fee_multiplier")
    if fee_multiplier is not None:
        fee_multiplier = int(fee_multiplier)
    return fee_type, fee_multiplier


def parse_fee_change_count(payload: Mapping[str, Any]) -> int:
    fee_changes = payload.get("series_fee_change_arr", [])
    return len(fee_changes)


def run_public_exchange_capability_check(
    series_ticker: str = SERIES_TICKER,
) -> ExchangeCapabilityCheckResult:
    errors: list[str] = []

    try:
        orderbook_doc_text = _get_text(ORDERBOOK_DOC_URL)
        orderbook_schema_ok = orderbook_doc_has_required_tokens(orderbook_doc_text)
        if not orderbook_schema_ok:
            errors.append("orderbook docs missing required delta tokens")
    except Exception as exc:  # pragma: no cover - network wrapper
        orderbook_schema_ok = False
        errors.append(f"orderbook docs fetch failed: {exc}")

    historical_cutoff = None
    try:
        historical_cutoff = _get_json(f"{PUBLIC_API_BASE}/historical/cutoff")
    except Exception as exc:  # pragma: no cover - network wrapper
        errors.append(f"historical cutoff fetch failed: {exc}")

    series_fee_type = None
    series_fee_multiplier = None
    try:
        series_payload = _get_json(f"{PUBLIC_API_BASE}/series/{series_ticker}")
        series_fee_type, series_fee_multiplier = parse_series_metadata(series_payload)
    except Exception as exc:  # pragma: no cover - network wrapper
        errors.append(f"series metadata fetch failed: {exc}")

    fee_change_count = None
    try:
        fee_change_payload = _get_json(
            f"{PUBLIC_API_BASE}/series/fee_changes?series_ticker={series_ticker}&show_historical=true"
        )
        fee_change_count = parse_fee_change_count(fee_change_payload)
    except Exception as exc:  # pragma: no cover - network wrapper
        errors.append(f"series fee changes fetch failed: {exc}")

    return ExchangeCapabilityCheckResult(
        checked_at=datetime.now(timezone.utc),
        orderbook_schema_ok=orderbook_schema_ok,
        historical_cutoff=historical_cutoff,
        series_fee_type=series_fee_type,
        series_fee_multiplier=series_fee_multiplier,
        fee_change_count=fee_change_count,
        errors=tuple(errors),
    )


def main() -> None:
    result = run_public_exchange_capability_check()
    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
