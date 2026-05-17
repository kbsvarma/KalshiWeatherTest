from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path

from kalshi_weather.clients import KalshiPublicClient, NceiDailySummariesClient, NwsClimateClient
from kalshi_weather.domain.models import SettlementReportSnapshot
from kalshi_weather.market.payloads import market_definition_from_payload
from kalshi_weather.settlement import build_ncei_proxy_reports, parse_cli_climate_report
from kalshi_weather.settlement.corpus import summarize_proxy_overlap, summarize_validation_entries, validate_settlement_corpus
from kalshi_weather.settlement.rule_parser import SettlementRuleParseError, parse_settlement_rule
from kalshi_weather.tools.backfill_cli_archive import _office_code, _report_matches_station
from kalshi_weather.storage import FileReferenceRegistry, SQLiteStateStore


def _report_key(report: SettlementReportSnapshot) -> tuple[str, int, str]:
    return (
        report.local_standard_window_start.date().isoformat(),
        report.report_version,
        report.raw_text_payload_id,
    )


def _load_local_cli_reports(raw_root: Path, station) -> list[SettlementReportSnapshot]:
    reports: list[SettlementReportSnapshot] = []
    metadata_paths = list(raw_root.glob("nws_cli/*/*/metadata.json"))
    metadata_paths.extend(raw_root.glob("nws_cli_archive/*/*/metadata.json"))
    for metadata_path in metadata_paths:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if str(metadata.get("metadata", {}).get("station_id") or "") != station.station_id:
            continue
        payload_path = Path(str(metadata.get("payload_path") or ""))
        if not payload_path.exists():
            continue
        version = int(metadata.get("request_params", {}).get("version") or 1)
        try:
            report = parse_cli_climate_report(
                payload_path.read_text(encoding="utf-8"),
                station=station,
                raw_text_payload_id=str(metadata.get("payload_hash") or metadata.get("raw_payload_id") or ""),
                source_url=str(metadata.get("source_endpoint") or ""),
                report_version=version,
            )
        except ValueError:
            continue
        reports.append(report)
    return reports


def _load_api_cli_reports(station) -> list[SettlementReportSnapshot]:
    client = NwsClimateClient()
    listing = client.list_products(
        product_type="CLI",
        office=_office_code(station.wfo_office),
        location=station.climate_product_id.removeprefix("CLI"),
        limit=30,
    )
    graph = listing.get("@graph", [])
    if not isinstance(graph, list):
        return []
    grouped: dict[str, list[dict[str, object]]] = {}
    for product in graph:
        product_id = str(product.get("id") or "")
        if not product_id:
            continue
        detail = client.get_product(product_id)
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
        grouped.setdefault(report.local_standard_window_start.date().isoformat(), []).append(
            {"detail": detail, "product_text": product_text}
        )
    reports: list[SettlementReportSnapshot] = []
    for _local_date, items in grouped.items():
        items.sort(key=lambda entry: str((entry["detail"]).get("issuanceTime") or ""))
        for index, item in enumerate(items, start=1):
            detail = item["detail"]
            reports.append(
                parse_cli_climate_report(
                    str(item["product_text"]),
                    station=station,
                    raw_text_payload_id=str(detail.get("id") or ""),
                    source_url=f"https://api.weather.gov/products/{detail['id']}",
                    report_version=index,
                )
            )
    return reports


def _load_version_archive_reports(station, max_versions: int = 80) -> list[SettlementReportSnapshot]:
    if os.getenv("KALSHI_WEATHER_ENABLE_VERSION_ARCHIVE", "0") != "1":
        return []
    client = NwsClimateClient()
    issuedby = station.climate_product_id.removeprefix("CLI")
    reports: list[SettlementReportSnapshot] = []
    consecutive_errors = 0
    for version in range(1, max_versions + 1):
        source_url = (
            "https://forecast.weather.gov/product.php"
            f"?site={station.wfo_office}&issuedby={issuedby}&product=CLI&format=TXT&version={version}&glossary=0"
        )
        try:
            text = client.fetch_cli_text(
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
            reports.append(
                parse_cli_climate_report(
                    text,
                    station=station,
                    raw_text_payload_id=f"{station.climate_product_id}:{version}",
                    source_url=source_url,
                    report_version=version,
                )
            )
        except ValueError:
            continue
    return reports


def _market_local_dates(market_payloads, station) -> list[str]:
    local_dates: set[str] = set()
    for payload in market_payloads:
        try:
            market_definition = market_definition_from_payload(payload)
            rule = parse_settlement_rule(market_definition, station)
        except SettlementRuleParseError:
            continue
        local_dates.add(rule.local_standard_window_start.date().isoformat())
    return sorted(local_dates)


def _load_ncei_proxy_reports(station, market_payloads) -> list[SettlementReportSnapshot]:
    if not station.ncei_station_id:
        return []
    local_dates = _market_local_dates(market_payloads, station)
    if not local_dates:
        return []
    client = NceiDailySummariesClient()
    rows = client.fetch_daily_summaries(
        station_id=station.ncei_station_id,
        start_date=local_dates[0],
        end_date=local_dates[-1],
    )
    source_url_template = (
        "https://www.ncei.noaa.gov/access/services/data/v1"
        f"?dataset=daily-summaries&stations={station.ncei_station_id}"
        "&startDate={date}&endDate={date}&format=json&units=standard"
    )
    return build_ncei_proxy_reports(
        station=station,
        rows=rows,
        source_url_template=source_url_template,
    )


def _merge_reports(*report_groups: list[SettlementReportSnapshot]) -> list[SettlementReportSnapshot]:
    merged: dict[tuple[str, int, str], SettlementReportSnapshot] = {}
    for reports in report_groups:
        for report in reports:
            merged[_report_key(report)] = report
    return list(merged.values())


def main() -> None:
    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    raw_root = Path("data/raw")
    store = SQLiteStateStore("data/state/runtime.sqlite3")
    all_entries = []
    city_reports = []
    for context in registry.iter_city_contexts(seed):
        station = context.station
        city_profile = context.city_profile
        series = context.series_definition
        settled_markets = list(KalshiPublicClient().iter_recent_settled_markets(series.series_ticker, max_records=300))
        threshold_markets = [
            payload
            for payload in settled_markets
            if "-T" in str(payload.get("ticker") or "")
        ]
        local_reports = _load_local_cli_reports(raw_root, station)
        use_api = (not local_reports) or (os.getenv("KALSHI_WEATHER_VALIDATE_WITH_API", "1") == "1")
        api_reports = _load_api_cli_reports(station) if use_api else []
        version_reports = _load_version_archive_reports(station)
        reports = _merge_reports(local_reports, api_reports, version_reports)
        proxy_reports = _load_ncei_proxy_reports(station, threshold_markets)
        entries = validate_settlement_corpus(
            city_id=city_profile.city_id,
            market_payloads=threshold_markets,
            station=station,
            reports=reports,
            proxy_reports=proxy_reports,
        )
        store.clear_settlement_validation_records(city_profile.city_id)
        for entry in entries:
            store.save_settlement_validation_record(entry.to_dict())
        all_entries.extend(entry.to_dict() for entry in entries)
        proxy_overlap = summarize_proxy_overlap(reports, proxy_reports)
        city_reports.append(
            {
                "city_id": city_profile.city_id,
                "series_ticker": series.series_ticker,
                "settled_market_count": len(settled_markets),
                "threshold_market_count": len(threshold_markets),
                "local_report_count": len(local_reports),
                "api_report_count": len(api_reports),
                "version_report_count": len(version_reports),
                "report_count": len(reports),
                "proxy_report_count": len(proxy_reports),
                "proxy_overlap": proxy_overlap,
                "summary": summarize_validation_entries(entry.to_dict() for entry in entries),
                "sample_entries": [entry.to_dict() for entry in entries[:5]],
            }
        )

    summary = summarize_validation_entries(all_entries)
    output = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "city_reports": city_reports,
    }
    print(json.dumps(output, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
