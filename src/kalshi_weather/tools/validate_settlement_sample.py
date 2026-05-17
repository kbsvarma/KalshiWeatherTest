from __future__ import annotations

from dataclasses import asdict
import json

from kalshi_weather.clients.kalshi_public import KalshiPublicClient
from kalshi_weather.clients.nws import NwsClimateClient
from kalshi_weather.domain.models import MarketDefinition
from kalshi_weather.ingestion.adapters import NwsCliAdapter
from kalshi_weather.settlement.rule_parser import parse_settlement_rule
from kalshi_weather.settlement.validation import validate_market_against_report
from kalshi_weather.storage.reference_registry import FileReferenceRegistry


def _market_definition_from_payload(payload: dict[str, object]) -> MarketDefinition:
    from datetime import datetime

    def _dt(name: str) -> datetime:
        value = str(payload[name])
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    return MarketDefinition(
        market_ticker=str(payload["ticker"]),
        event_ticker=str(payload.get("event_ticker") or ""),
        series_ticker=str(payload.get("series_ticker") or ""),
        market_type="binary_threshold",
        threshold_f=None,
        operator="",
        open_time=_dt("open_time"),
        close_time=_dt("close_time"),
        settlement_ts=None,
        rules_primary=str(payload.get("rules_primary") or ""),
        rules_secondary=None,
        price_level_structure=str(payload.get("price_level_structure") or ""),
        price_ranges=(),
        can_close_early=bool(payload.get("can_close_early") or False),
        is_provisional=bool(payload.get("is_provisional") or False),
        status=str(payload.get("status") or ""),
    )


def main() -> None:
    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    kalshi = KalshiPublicClient()
    city_reports = []
    for context in registry.iter_city_contexts(seed):
        settled = kalshi.iter_recent_settled_markets(
            context.series_definition.series_ticker,
            max_records=1,
        )
        if not settled:
            city_reports.append(
                {
                    "city_id": context.city_profile.city_id,
                    "series_ticker": context.series_definition.series_ticker,
                    "error": "no settled markets returned",
                }
            )
            continue

        market_payload = dict(settled[0])
        market = _market_definition_from_payload(market_payload)
        rule = parse_settlement_rule(market, context.station)

        issuedby = context.station.climate_product_id.removeprefix("CLI")
        cli_adapter = NwsCliAdapter(
            NwsClimateClient(),
            station=context.station,
            issuedby=issuedby,
            version=1,
        )
        cli_report = cli_adapter.normalize(cli_adapter.fetch_raw())[0].record
        result = validate_market_against_report(market_payload, rule, cli_report)
        city_reports.append(
            {
                "city_id": context.city_profile.city_id,
                "series_ticker": context.series_definition.series_ticker,
                "validation": asdict(result),
            }
        )
    print(json.dumps({"city_reports": city_reports}, default=str, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
