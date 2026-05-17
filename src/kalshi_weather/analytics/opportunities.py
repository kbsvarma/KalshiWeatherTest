from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    return datetime.fromisoformat(str(value))


_DECISION_PRIORITY = {
    "TAKER_ALLOWED": 0,
    "WATCH": 1,
    "MAKER_ONLY": 2,
    "NO_TRADE": 3,
    "REDUCE": 4,
    "EXIT": 5,
    "CANCEL_PENDING": 6,
    "HALT_CITY": 7,
    "HALT_GLOBAL": 8,
}

_THRESHOLD_TICKER_PATTERN = re.compile(r"^(?P<event>.+)-T(?P<threshold>-?\d+)$")
# Range/bracket markets: -B<midpoint>.5 (e.g., -B85.5 = high in [85, 86]).
_RANGE_TICKER_PATTERN = re.compile(r"^(?P<event>.+)-B(?P<midpoint>-?\d+(?:\.\d+)?)$")
# Date extraction supports both -T threshold and -B range tickers.
_MARKET_DATE_PATTERN = re.compile(
    r"-(?P<yy>\d{2})(?P<month>[A-Z]{3})(?P<day>\d{2})-[TB]"
)
_MONTH_BY_CODE = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def _market_key_parts(market_ticker: str) -> tuple[str, Decimal | None]:
    match = _THRESHOLD_TICKER_PATTERN.match(market_ticker)
    if not match:
        return market_ticker, None
    return match.group("event"), Decimal(match.group("threshold"))


def parse_market_date(market_ticker: str) -> date | None:
    match = _MARKET_DATE_PATTERN.search(market_ticker)
    if not match:
        return None
    month = _MONTH_BY_CODE.get(match.group("month"))
    if month is None:
        return None
    try:
        return date(2000 + int(match.group("yy")), month, int(match.group("day")))
    except ValueError:
        return None


def _local_decision_date(
    payload: Mapping[str, Any],
    timezone_by_city: Mapping[str, str] | None,
) -> date | None:
    as_of_time = _parse_datetime(payload.get("as_of_time"))
    if as_of_time is None:
        return None
    city_id = str(payload.get("city_id") or "")
    timezone_name = (timezone_by_city or {}).get(city_id)
    if timezone_name:
        try:
            return as_of_time.astimezone(ZoneInfo(timezone_name)).date()
        except Exception:
            pass
    return as_of_time.date()


def _filter_market_day_mode(
    decision_payloads: list[dict[str, Any]],
    *,
    market_day_mode: str,
    timezone_by_city: Mapping[str, str] | None,
) -> list[dict[str, Any]]:
    if market_day_mode == "all":
        return decision_payloads

    filtered: list[dict[str, Any]] = []
    next_available_by_city: dict[str, date] = {}
    if market_day_mode == "next_available":
        for payload in decision_payloads:
            city_id = str(payload.get("city_id") or "")
            market_date = parse_market_date(str(payload.get("market_ticker") or ""))
            local_date = _local_decision_date(payload, timezone_by_city)
            if not city_id or market_date is None or local_date is None or market_date <= local_date:
                continue
            current = next_available_by_city.get(city_id)
            if current is None or market_date < current:
                next_available_by_city[city_id] = market_date

    for payload in decision_payloads:
        market_date = parse_market_date(str(payload.get("market_ticker") or ""))
        local_date = _local_decision_date(payload, timezone_by_city)
        if market_date is None or local_date is None:
            continue
        if market_day_mode == "open_day":
            if market_date == local_date:
                filtered.append(payload)
        elif market_day_mode == "next_available":
            city_id = str(payload.get("city_id") or "")
            if city_id and market_date == next_available_by_city.get(city_id):
                filtered.append(payload)
    return filtered


def _annotate_cross_threshold_inconsistencies(items: list[dict[str, Any]]) -> dict[str, int]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in items:
        event_key, threshold_value = _market_key_parts(str(item.get("market_ticker") or ""))
        item["threshold_event_key"] = event_key
        item["threshold_value_f"] = str(threshold_value) if threshold_value is not None else None
        item["cross_threshold_model_inconsistency"] = False
        item["cross_threshold_market_inconsistency"] = False
        item["cross_threshold_neighbor_count"] = 0
        if threshold_value is None:
            continue
        grouped.setdefault((str(item.get("city_id") or ""), event_key), []).append(item)

    model_count = 0
    market_count = 0
    tolerance = Decimal("0.02")
    for grouped_items in grouped.values():
        if len(grouped_items) < 2:
            continue
        grouped_items.sort(key=lambda item: Decimal(str(item["threshold_value_f"])))
        for lower, higher in zip(grouped_items, grouped_items[1:], strict=False):
            lower["cross_threshold_neighbor_count"] += 1
            higher["cross_threshold_neighbor_count"] += 1
            lower_p_yes = _decimal((lower.get("path_state") or {}).get("p_yes"))
            higher_p_yes = _decimal((higher.get("path_state") or {}).get("p_yes"))
            if (
                lower_p_yes is not None
                and higher_p_yes is not None
                and higher_p_yes > (lower_p_yes + tolerance)
            ):
                if not lower["cross_threshold_model_inconsistency"]:
                    model_count += 1
                if not higher["cross_threshold_model_inconsistency"]:
                    model_count += 1
                lower["cross_threshold_model_inconsistency"] = True
                higher["cross_threshold_model_inconsistency"] = True
            lower_yes_ask = _decimal((lower.get("microstructure_summary") or {}).get("best_yes_ask"))
            higher_yes_ask = _decimal((higher.get("microstructure_summary") or {}).get("best_yes_ask"))
            if (
                lower_yes_ask is not None
                and higher_yes_ask is not None
                and higher_yes_ask > (lower_yes_ask + tolerance)
            ):
                if not lower["cross_threshold_market_inconsistency"]:
                    market_count += 1
                if not higher["cross_threshold_market_inconsistency"]:
                    market_count += 1
                lower["cross_threshold_market_inconsistency"] = True
                higher["cross_threshold_market_inconsistency"] = True
    return {
        "cross_threshold_model_inconsistency_count": model_count,
        "cross_threshold_market_inconsistency_count": market_count,
    }


def select_latest_market_decisions(
    decision_payloads: list[dict[str, Any]],
    *,
    city_id: str | None = None,
) -> list[dict[str, Any]]:
    latest_by_market: dict[tuple[str, str], dict[str, Any]] = {}
    for payload in decision_payloads:
        payload_city_id = str(payload.get("city_id") or "")
        if city_id and payload_city_id != city_id:
            continue
        market_ticker = str(payload.get("market_ticker") or "")
        if not payload_city_id or not market_ticker:
            continue
        as_of_time = _parse_datetime(payload.get("as_of_time"))
        if as_of_time is None:
            continue
        key = (payload_city_id, market_ticker)
        current = latest_by_market.get(key)
        current_time = _parse_datetime(current.get("as_of_time")) if current else None
        if current_time is None or as_of_time >= current_time:
            latest_by_market[key] = payload
    return sorted(
        latest_by_market.values(),
        key=lambda item: (
            str(item.get("city_id") or ""),
            str(item.get("market_ticker") or ""),
        ),
    )


def _latest_cycle_market_decisions(
    decision_payloads: list[dict[str, Any]],
    *,
    cycle_window_minutes: int,
) -> list[dict[str, Any]]:
    latest_city_time: dict[str, datetime] = {}
    for payload in decision_payloads:
        payload_city_id = str(payload.get("city_id") or "")
        as_of_time = _parse_datetime(payload.get("as_of_time"))
        if not payload_city_id or as_of_time is None:
            continue
        current = latest_city_time.get(payload_city_id)
        if current is None or as_of_time > current:
            latest_city_time[payload_city_id] = as_of_time
    filtered: list[dict[str, Any]] = []
    cycle_window_seconds = max(cycle_window_minutes, 1) * 60
    for payload in decision_payloads:
        payload_city_id = str(payload.get("city_id") or "")
        as_of_time = _parse_datetime(payload.get("as_of_time"))
        latest_time = latest_city_time.get(payload_city_id)
        if payload_city_id and as_of_time is not None and latest_time is not None:
            age_seconds = (latest_time - as_of_time).total_seconds()
            if 0 <= age_seconds <= cycle_window_seconds:
                filtered.append(payload)
    return select_latest_market_decisions(filtered)


def select_scannable_market_decisions(
    decision_payloads: list[dict[str, Any]],
    *,
    cycle_window_minutes: int = 10,
    market_day_mode: str = "all",
    timezone_by_city: Mapping[str, str] | None = None,
    open_city_ids: set[str] | None = None,
    exclude_open_cities: bool = False,
) -> list[dict[str, Any]]:
    latest_payloads = _latest_cycle_market_decisions(
        decision_payloads,
        cycle_window_minutes=cycle_window_minutes,
    )
    filtered_payloads = _filter_market_day_mode(
        latest_payloads,
        market_day_mode=market_day_mode,
        timezone_by_city=timezone_by_city,
    )
    if exclude_open_cities:
        blocked_city_ids = {str(city_id) for city_id in (open_city_ids or set())}
        filtered_payloads = [
            payload
            for payload in filtered_payloads
            if str(payload.get("city_id") or "") not in blocked_city_ids
        ]
    return filtered_payloads


def build_opportunity_board(
    decision_payloads: list[dict[str, Any]],
    *,
    qualification_states: Mapping[str, str] | None = None,
    limit: int | None = 10,
    cycle_window_minutes: int = 10,
    market_day_mode: str = "all",
    timezone_by_city: Mapping[str, str] | None = None,
    open_city_ids: set[str] | None = None,
    exclude_open_cities: bool = False,
) -> dict[str, Any]:
    latest_payloads = select_scannable_market_decisions(
        decision_payloads,
        cycle_window_minutes=cycle_window_minutes,
        market_day_mode=market_day_mode,
        timezone_by_city=timezone_by_city,
        open_city_ids=open_city_ids,
        exclude_open_cities=exclude_open_cities,
    )
    items: list[dict[str, Any]] = []
    for payload in latest_payloads:
        edge = payload.get("edge_summary", {})
        micro = payload.get("microstructure_summary", {})
        confidence = edge.get("confidence_summary", {})
        city_id = str(payload.get("city_id") or "")
        final_decision = str(payload.get("final_decision") or "")
        executable_ev = _decimal(edge.get("selected_executable_ev")) or Decimal("-999")
        trade_confidence = _decimal(confidence.get("overall_trade_confidence")) or Decimal("0")
        tradability = _decimal(micro.get("selected_taker_tradability_score")) or Decimal("0")
        as_of_time = _parse_datetime(payload.get("as_of_time"))
        market_date = parse_market_date(str(payload.get("market_ticker") or ""))
        local_market_date = _local_decision_date(payload, timezone_by_city)
        items.append(
            {
                "city_id": city_id,
                "market_ticker": str(payload.get("market_ticker") or ""),
                "as_of_time": payload.get("as_of_time"),
                "market_date": market_date.isoformat() if market_date is not None else None,
                "local_decision_date": local_market_date.isoformat() if local_market_date is not None else None,
                "final_decision": final_decision,
                "selected_side": edge.get("selected_side"),
                "selected_executable_ev": edge.get("selected_executable_ev"),
                "selected_raw_edge": edge.get("selected_raw_edge"),
                "selected_taker_tradability_score": micro.get("selected_taker_tradability_score"),
                "overall_trade_confidence": confidence.get("overall_trade_confidence"),
                "model_confidence": confidence.get("model_confidence"),
                "execution_confidence": confidence.get("execution_confidence"),
                "governance_confidence": confidence.get("governance_confidence"),
                "confidence_reasons": tuple(confidence.get("confidence_reasons") or ()),
                "explanation_codes": tuple(payload.get("explanation_codes") or ()),
                "risk_summary": payload.get("risk_summary") or {},
                "regime_summary": payload.get("regime_summary") or {},
                "qualification_state": (qualification_states or {}).get(city_id),
                "_sort_key": (
                    _DECISION_PRIORITY.get(final_decision, 99),
                    0 if ((payload.get("path_state") or {}).get("p_yes")) is not None else 1,
                    -float(executable_ev),
                    -float(trade_confidence),
                    -float(tradability),
                    -(as_of_time.timestamp() if as_of_time is not None else 0.0),
                ),
                "path_state": payload.get("path_state") or {},
                "microstructure_summary": payload.get("microstructure_summary") or {},
            }
        )
    inconsistency_summary = _annotate_cross_threshold_inconsistencies(items)
    for item in items:
        item["_sort_key"] = (
            item["_sort_key"][0],
            0
            if item["cross_threshold_market_inconsistency"] or item["cross_threshold_model_inconsistency"]
            else 1,
            *item["_sort_key"][1:],
        )
    items.sort(key=lambda item: item["_sort_key"])
    city_best: dict[str, dict[str, Any]] = {}
    for item in items:
        item_copy = dict(item)
        item_copy.pop("_sort_key", None)
        item_copy.pop("path_state", None)
        item_copy.pop("microstructure_summary", None)
        city_best.setdefault(item_copy["city_id"], item_copy)
    trimmed = items if limit is None else items[:limit]
    for item in trimmed:
        item.pop("_sort_key", None)
        item.pop("path_state", None)
        item.pop("microstructure_summary", None)
    return {
        "market_day_mode": market_day_mode,
        "exclude_open_cities": exclude_open_cities,
        "excluded_open_city_count": len(open_city_ids or set()) if exclude_open_cities else 0,
        "market_count": len(latest_payloads),
        "ranked_count": len(trimmed),
        "taker_allowed_count": sum(1 for item in items if item["final_decision"] == "TAKER_ALLOWED"),
        **inconsistency_summary,
        "items": trimmed,
        "city_best": city_best,
    }
