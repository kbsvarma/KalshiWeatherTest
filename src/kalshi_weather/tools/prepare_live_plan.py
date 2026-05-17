from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
import json
from uuid import uuid4

from kalshi_weather.analytics import (
    build_live_gate_report,
    build_nowcast_validation_report,
    build_provider_reliability_report,
    select_scannable_market_decisions,
    select_live_gate_profile,
)
from kalshi_weather.clients import KalshiPrivateClient
from kalshi_weather.domain.enums import DecisionType, RunMode
from kalshi_weather.domain.models import EdgeEstimate, StrategyDecisionExplanation
from kalshi_weather.live import LiveAdapterError, ThinLiveAdapter, build_execution_plan
from kalshi_weather.settlement.corpus import summarize_validation_entries
from kalshi_weather.storage import DerivedAnalyticsStore, FileReferenceRegistry, SQLiteStateStore


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _edge_from_stored_decision(
    latest: dict[str, object],
    *,
    executable_price: Decimal,
    fee_cost: Decimal,
    slippage_cost: Decimal,
    adverse_selection_penalty: Decimal,
    total_friction: Decimal,
    portfolio_haircut: Decimal,
) -> EdgeEstimate:
    edge_summary = latest.get("edge_summary", {})
    if not isinstance(edge_summary, dict):
        edge_summary = {}
    return EdgeEstimate(
        market_ticker=str(latest["market_ticker"]),
        side=str(edge_summary.get("selected_side") or ""),
        quantity_fp=Decimal("1"),
        p_model=_decimal(edge_summary.get("selected_p_model")) or Decimal("0"),
        p_market_exec=executable_price,
        raw_edge=_decimal(edge_summary.get("selected_raw_edge")) or Decimal("0"),
        fee_cost=fee_cost,
        slippage_cost=slippage_cost,
        adverse_selection_penalty=adverse_selection_penalty,
        total_friction=total_friction,
        friction_to_edge_ratio=_decimal(edge_summary.get("selected_friction_to_edge_ratio")) or Decimal("0"),
        uncertainty_haircut=_decimal(edge_summary.get("selected_uncertainty_haircut")) or Decimal("0"),
        regime_haircut=Decimal("0"),
        portfolio_haircut=portfolio_haircut,
        edge_conf_adj=Decimal("0"),
        executable_ev_per_contract=_decimal(edge_summary.get("selected_executable_ev")) or Decimal("0"),
        executable_ev_total=_decimal(edge_summary.get("selected_executable_ev")) or Decimal("0"),
    )


def _selected_executable_ev(payload: dict[str, object]) -> Decimal:
    edge_summary = payload.get("edge_summary", {})
    if not isinstance(edge_summary, dict):
        return Decimal("-999")
    value = _decimal(edge_summary.get("selected_executable_ev"))
    return value if value is not None else Decimal("-999")


def _safe_prepare_payload(adapter: ThinLiveAdapter, plan) -> dict[str, object]:
    try:
        return adapter.prepare_order_payload(plan)
    except Exception as exc:
        quantity = getattr(plan, "quantity_fp", None)
        if quantity is None:
            quantity = getattr(plan, "quantity", None)
        return {
            "market_ticker": plan.market_ticker,
            "side": plan.side,
            "quantity": str(quantity) if quantity is not None else "1",
            "error": f"payload_prepare_failed:{exc}",
        }


def _select_latest_taker_decision(
    decisions: list[dict[str, object]],
    *,
    preferred_city_id: str = "nyc",
    cycle_window_seconds: int = 180,
    market_day_mode: str = "all",
    timezone_by_city: dict[str, str] | None = None,
    open_city_ids: set[str] | None = None,
    exclude_open_cities: bool = False,
) -> dict[str, object] | None:
    scannable = select_scannable_market_decisions(
        [dict(payload) for payload in decisions],
        cycle_window_minutes=max(1, cycle_window_seconds // 60),
        market_day_mode=market_day_mode,
        timezone_by_city=timezone_by_city,
        open_city_ids=open_city_ids,
        exclude_open_cities=exclude_open_cities,
    )
    preferred = [payload for payload in scannable if str(payload.get("city_id") or "") == preferred_city_id]
    fallback = scannable
    for pool in (preferred, fallback):
        if not pool:
            continue
        latest_as_of = max(datetime.fromisoformat(str(payload["as_of_time"])) for payload in pool)
        latest_cycle = sorted(
            [
                payload
                for payload in pool
                if 0 <= (latest_as_of - datetime.fromisoformat(str(payload["as_of_time"]))).total_seconds()
                <= cycle_window_seconds
            ],
            key=lambda payload: datetime.fromisoformat(str(payload["as_of_time"])),
            reverse=True,
        )
        takers = [
            payload
            for payload in latest_cycle
            if payload.get("final_decision") == DecisionType.TAKER_ALLOWED.value
        ]
        if takers:
            return max(
                takers,
                key=lambda payload: (
                    _selected_executable_ev(payload),
                    datetime.fromisoformat(str(payload["as_of_time"])),
                ),
            )
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare or execute the weather live order plan.")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Submit the prepared plan live instead of returning a dry-run payload.",
    )
    parser.add_argument(
        "--selection-mode",
        choices=("next_available", "open_day", "all"),
        default="next_available",
        help="Choose whether to scan the next available market day, the current local market day, or all latest-cycle markets.",
    )
    parser.add_argument(
        "--preferred-city-id",
        default="nyc",
        help="Prefer this city when multiple taker-eligible plans exist in the same scan window.",
    )
    parser.add_argument(
        "--include-open-cities",
        action="store_true",
        help="Allow selection from cities that already have an open shadow position. Default is to skip them for new entries.",
    )
    args = parser.parse_args()
    store = SQLiteStateStore("data/state/runtime.sqlite3")
    decisions = store.list_decision_payloads()
    derived_store = DerivedAnalyticsStore("data/derived")
    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    context_by_city = registry.context_by_city_id(seed)
    timezone_by_city = {
        city_id: context.station.timezone
        for city_id, context in context_by_city.items()
    }
    open_city_ids = {
        str(payload.get("city_id") or "")
        for payload in store.list_shadow_position_payloads()
        if str(payload.get("lifecycle_status") or "") == "OPEN"
    }
    if not decisions:
        raise SystemExit("no decisions stored")

    latest = _select_latest_taker_decision(
        decisions,
        preferred_city_id=args.preferred_city_id,
        market_day_mode=args.selection_mode,
        timezone_by_city=timezone_by_city,
        open_city_ids=open_city_ids,
        exclude_open_cities=not args.include_open_cities,
    )
    selection_open_city_fallback_used = False
    if latest is None and not args.include_open_cities:
        # Shadow positions should not hide otherwise-valid live candidates; retry once
        # including open cities before declaring there is no actionable plan.
        latest = _select_latest_taker_decision(
            decisions,
            preferred_city_id=args.preferred_city_id,
            market_day_mode=args.selection_mode,
            timezone_by_city=timezone_by_city,
            open_city_ids=open_city_ids,
            exclude_open_cities=False,
        )
        selection_open_city_fallback_used = latest is not None
    if latest is None:
        raise SystemExit("no taker-eligible stored decision is available from the latest cycle")
    city_id = str(latest["city_id"])
    context = context_by_city.get(city_id)
    if context is None:
        raise SystemExit(f"missing registry context for city_id={city_id}")
    fills = store.list_shadow_fill_payloads(city_id)
    positions = [
        payload
        for payload in store.list_shadow_position_payloads()
        if payload.get("city_id") == city_id
    ]
    validations = store.list_settlement_validation_payloads(city_id)

    edge_summary = latest.get("edge_summary", {})
    micro_summary = latest.get("microstructure_summary", {})
    selected_side = edge_summary["selected_side"]
    executable_price = _decimal(edge_summary.get("selected_p_market_exec"))
    if executable_price is None:
        if selected_side == "yes":
            executable_price = _decimal(micro_summary.get("best_yes_ask"))
        elif selected_side == "no":
            executable_price = _decimal(micro_summary.get("best_no_ask"))
    slippage_cost = _decimal(edge_summary.get("selected_slippage_cost")) or Decimal("0")
    adverse_selection_penalty = (
        _decimal(edge_summary.get("selected_adverse_selection_penalty")) or Decimal("0")
    )
    total_friction = _decimal(edge_summary.get("selected_total_friction")) or Decimal("0")
    portfolio_haircut = _decimal(edge_summary.get("selected_portfolio_haircut")) or Decimal("0")
    fee_cost = _decimal(edge_summary.get("selected_fee_cost"))
    if fee_cost is None:
        fee_cost = max(Decimal("0"), total_friction - slippage_cost - adverse_selection_penalty)
    explanation = StrategyDecisionExplanation(
        schema_version=latest["schema_version"],
        decision_id=latest["decision_id"],
        run_mode=RunMode(latest["run_mode"]),
        as_of_time=datetime.fromisoformat(latest["as_of_time"]),
        market_ticker=latest["market_ticker"],
        city_id=latest["city_id"],
        station_id=latest["station_id"],
        settlement_rule_id=latest["settlement_rule_id"],
        data_freshness=latest["data_freshness"],
        current_state=latest["current_state"],
        forecast_summary=latest["forecast_summary"],
        path_state=latest["path_state"],
        microstructure_summary=latest["microstructure_summary"],
        edge_summary=latest["edge_summary"],
        regime_summary=latest["regime_summary"],
        risk_summary=latest["risk_summary"],
        final_decision=DecisionType(latest["final_decision"]),
        explanation_codes=tuple(latest["explanation_codes"]),
        provenance_refs=tuple(latest["provenance_refs"]),
        module_versions=latest["module_versions"],
    )
    edge = _edge_from_stored_decision(
        latest,
        executable_price=executable_price if executable_price is not None else Decimal("0"),
        fee_cost=fee_cost,
        slippage_cost=slippage_cost,
        adverse_selection_penalty=adverse_selection_penalty,
        total_friction=total_friction,
        portfolio_haircut=portfolio_haircut,
    )
    calibration_report = derived_store.read_provider_reliability(city_id)
    if calibration_report is None:
        calibration_report = build_provider_reliability_report(
            store.get_all_forecasts(context.station.station_id),
            store.get_all_observations(context.station.station_id),
            context.station,
        )
        derived_store.write_provider_reliability(city_id, calibration_report)
    live_gate = build_live_gate_report(
        store.list_decision_payloads(city_id),
        fills,
        position_payloads=positions,
        settlement_summary=summarize_validation_entries(validations),
        nowcast_report=build_nowcast_validation_report(
            store.get_all_observations(context.station.station_id),
            store.get_latest_forecasts(context.station.station_id),
            context.city_profile,
            station_timezone=context.station.timezone,
        ),
        calibration_report=calibration_report,
        profile=select_live_gate_profile(city_id),
    )
    private_client = KalshiPrivateClient.from_env() if args.execute else None
    adapter = ThinLiveAdapter(private_client=private_client)
    plan = build_execution_plan(explanation, edge)
    if args.execute:
        plan = replace(plan, run_mode=RunMode.LIVE_TRADE)
    try:
        response = adapter.submit_plan(plan, live_gate, dry_run=not args.execute)
    except LiveAdapterError as exc:
        response = {
            "status": "BLOCKED",
            "reason": str(exc),
            "endpoint": f"{adapter.api_base}/portfolio/orders",
            "payload": _safe_prepare_payload(adapter, plan),
            "live_gate_report": live_gate,
            "selection_mode": args.selection_mode,
            "include_open_cities": args.include_open_cities,
            "selection_open_city_fallback_used": selection_open_city_fallback_used,
        }
    response["selection_mode"] = args.selection_mode
    response["include_open_cities"] = args.include_open_cities
    response["selection_open_city_fallback_used"] = selection_open_city_fallback_used
    store.save_live_order_record(
        {
            "record_id": uuid4().hex,
            "market_ticker": plan.market_ticker,
            "created_at": explanation.as_of_time.isoformat(),
            "status": response["status"],
            "payload": response["payload"],
            "decision_id": explanation.decision_id,
            "city_id": explanation.city_id,
            "side": edge.side,
            "decision_utc": explanation.as_of_time.isoformat(),
            "confidence_summary": edge_summary.get("confidence_summary") or {},
        }
    )
    print(json.dumps(response, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
