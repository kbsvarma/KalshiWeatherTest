from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
import re

from kalshi_weather.clients import NwsClimateClient
from kalshi_weather.ingestion.contracts import RawPayloadRecord
from kalshi_weather.settlement import parse_cli_climate_report
from kalshi_weather.storage import FileRawStore, FileReferenceRegistry


def _office_code(wfo_office: str) -> str:
    return wfo_office if wfo_office.startswith("K") else f"K{wfo_office}"


def _normalize_station_text(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", " ", value.upper()).strip()


def _station_match_aliases(station) -> tuple[str, ...]:
    aliases = {
        _normalize_station_text(station.station_name),
        _normalize_station_text(station.climate_product_id),
        _normalize_station_text(station.metar_code),
    }
    normalized_name = _normalize_station_text(station.station_name)
    if " INTERNATIONAL AIRPORT" in normalized_name:
        aliases.add(normalized_name.replace(" INTERNATIONAL AIRPORT", " AIRPORT"))
        aliases.add(normalized_name.replace(" INTERNATIONAL AIRPORT", ""))
    if " INTL AIRPORT" in normalized_name:
        aliases.add(normalized_name.replace(" INTL AIRPORT", " AIRPORT"))
        aliases.add(normalized_name.replace(" INTL AIRPORT", ""))
    if " AIRPORT" in normalized_name:
        aliases.add(normalized_name.replace(" AIRPORT", ""))
    aliases.update(
        {
            "nyc-central-park": ("CENTRAL PARK", "NEW YORK CITY"),
            "phl-airport": ("PHILADELPHIA", "PHILADELPHIA PA"),
            "aus-bergstrom": ("AUSTIN BERGSTROM", "AUSTIN"),
            "den-airport": ("DENVER", "DENVER CO"),
            "bos-logan": ("BOSTON", "BOSTON MA"),
            "mia-airport": ("MIAMI", "MIAMI FL"),
            "chi-midway": ("CHICAGO MIDWAY", "MIDWAY"),
            "lax-airport": ("LOS ANGELES AIRPORT", "LAX"),
        }.get(station.station_id, ())
    )
    return tuple(alias for alias in aliases if alias)


def _report_matches_station(product_text: str, station) -> bool:
    normalized_text = _normalize_station_text(product_text)
    return any(alias in normalized_text for alias in _station_match_aliases(station))


def _load_api_archive(climate_client: NwsClimateClient, station) -> list[dict[str, object]]:
    listing = climate_client.list_products(
        product_type="CLI",
        office=_office_code(station.wfo_office),
        location=station.climate_product_id.removeprefix("CLI"),
        limit=30,
    )
    products = listing.get("@graph", [])
    if not isinstance(products, list):
        return []
    product_payloads: list[dict[str, object]] = []
    for product in products:
        product_id = str(product.get("id") or "")
        if not product_id:
            continue
        detail = climate_client.get_product(product_id)
        product_text = str(detail.get("productText") or "")
        if not _report_matches_station(product_text, station):
            continue
        try:
            report = parse_cli_climate_report(
                product_text,
                station=station,
                raw_text_payload_id=product_id,
                source_url=f"https://api.weather.gov/products/{product_id}",
                report_version=1,
            )
        except ValueError:
            continue
        product_payloads.append(
            {
                "detail": detail,
                "product_text": product_text,
                "source_endpoint": f"https://api.weather.gov/products/{product_id}",
                "issuance_time": detail.get("issuanceTime"),
                "version": report.report_version,
            }
        )
    return product_payloads


def _load_version_archive(climate_client: NwsClimateClient, station, max_versions: int = 120) -> list[dict[str, object]]:
    if os.getenv("KALSHI_WEATHER_ENABLE_VERSION_ARCHIVE", "1") != "1":
        return []
    issuedby = station.climate_product_id.removeprefix("CLI")
    product_payloads: list[dict[str, object]] = []
    consecutive_errors = 0
    for version in range(1, max_versions + 1):
        source_endpoint = (
            "https://forecast.weather.gov/product.php"
            f"?site={station.wfo_office}&issuedby={issuedby}&product=CLI&format=TXT&version={version}&glossary=0"
        )
        try:
            product_text = climate_client.fetch_cli_text(
                site=station.wfo_office,
                issuedby=issuedby,
                version=version,
            )
            consecutive_errors = 0
        except Exception:
            consecutive_errors += 1
            if version >= 10 and consecutive_errors >= 3:
                break
            continue
        try:
            parse_cli_climate_report(
                product_text,
                station=station,
                raw_text_payload_id=f"{station.climate_product_id}:{version}",
                source_url=source_endpoint,
                report_version=version,
            )
        except ValueError:
            continue
        product_payloads.append(
            {
                "detail": {"id": f"{station.climate_product_id}:{version}"},
                "product_text": product_text,
                "source_endpoint": source_endpoint,
                "issuance_time": None,
                "version": version,
            }
        )
    return product_payloads


def main() -> None:
    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    climate_client = NwsClimateClient()
    raw_store = FileRawStore("data/raw")
    stored = 0
    settlement_day_count = 0
    city_reports = []
    for context in registry.iter_city_contexts(seed):
        station = context.station
        product_payloads = _load_api_archive(climate_client, station) + _load_version_archive(
            climate_client, station
        )

        grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
        for item in product_payloads:
            report = parse_cli_climate_report(
                str(item["product_text"]),
                station=station,
                raw_text_payload_id="preflight",
                source_url=str(item["source_endpoint"]),
                report_version=int(item["version"]),
            )
            grouped[report.local_standard_window_start.date().isoformat()].append(item)

        city_stored = 0
        dates = []
        for local_date, items in grouped.items():
            dates.append(local_date)
            items.sort(key=lambda entry: str((entry["detail"]).get("issuanceTime") or ""))
            for index, item in enumerate(items, start=1):
                detail = item["detail"]
                product_text = str(item["product_text"])
                payload_hash = sha256(product_text.encode("utf-8")).hexdigest()
                raw_store.write(
                    RawPayloadRecord(
                        source_name="nws_cli_archive",
                        source_endpoint=str(item["source_endpoint"]),
                        request_params={
                            "product_id": detail["id"],
                            "version": int(item["version"] or index),
                            "office": detail.get("issuingOffice") or station.wfo_office,
                        },
                        transport_status=200,
                        payload_hash=payload_hash,
                        parser_version="cli_parser_v1",
                        ingest_time=datetime.now(timezone.utc),
                        event_time=None,
                        payload=product_text,
                        metadata={
                            "city_id": context.city_profile.city_id,
                            "station_id": station.station_id,
                            "climate_product_id": station.climate_product_id,
                            "product_id": detail["id"],
                            "issuance_time": item.get("issuance_time"),
                        },
                    )
                )
                city_stored += 1
                stored += 1
        settlement_day_count += len(grouped)
        city_reports.append(
            {
                "city_id": context.city_profile.city_id,
                "station_id": station.station_id,
                "climate_product_id": station.climate_product_id,
                "stored_report_count": city_stored,
                "settlement_day_count": len(grouped),
                "local_dates": sorted(dates),
            }
        )
    print(
        json.dumps(
            {
                "stored_report_count": stored,
                "settlement_day_count": settlement_day_count,
                "city_reports": city_reports,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
