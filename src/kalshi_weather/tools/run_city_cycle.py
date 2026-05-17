from __future__ import annotations

import argparse
import os
import signal
import sys
import time
import traceback
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

from kalshi_weather.clients import KalshiPublicClient, NwsWeatherClient, OpenMeteoClient


# ── Per-cycle hard limits ──────────────────────────────────────────────
# These exist because of the 2026-05-16 evening outage: the bot was happily
# running long fetches when macOS revoked the launchd permission, and we
# learned nothing was watching for "this cycle never finished" failures.
# A hung HTTP call or a single city raising an unhandled exception used to
# kill the entire cycle. With these limits each city is isolated and the
# cycle has a hard ceiling so it can never overlap the next scheduled slot.
MAX_CYCLE_SECONDS = 240  # 4 min hard cap before SIGALRM aborts the cycle
MAX_CITY_SECONDS = 30    # soft per-city budget enforced via wall-clock check


class CycleTimeoutError(RuntimeError):
    """Raised when the cycle exceeds MAX_CYCLE_SECONDS — never overlap slots."""


def _install_cycle_timeout(seconds: int) -> None:
    def _handler(signum, frame):  # noqa: ANN001
        raise CycleTimeoutError(
            f"cycle exceeded {seconds}s budget — aborting to protect next slot"
        )

    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(seconds)


def _preflight(state_store, kalshi_client, *, live_enabled: bool) -> list[str]:
    """Return list of preflight failure reasons. Empty list = healthy."""
    failures: list[str] = []

    # DB readable + writable?
    try:
        with state_store._connect() as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as exc:
        failures.append(f"db_unreadable: {exc}")

    # Kalshi public endpoint reachable?
    try:
        kalshi_client.get_exchange_status()
    except AttributeError:
        # client doesn't expose status — try the cheapest call we do have
        try:
            kalshi_client.get_series("KXHIGHNY")
        except Exception as exc:
            failures.append(f"kalshi_public_unreachable: {exc}")
    except Exception as exc:
        failures.append(f"kalshi_public_unreachable: {exc}")

    # Live credentials present when live mode is on?
    if live_enabled:
        key_id = os.environ.get("KALSHI_API_KEY_ID")
        key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
        if not key_id:
            failures.append("kalshi_api_key_id_missing")
        if not key_path:
            failures.append("kalshi_private_key_path_missing")
        elif not os.path.exists(key_path):
            failures.append(f"kalshi_private_key_file_not_found: {key_path}")

    return failures
from kalshi_weather.domain.enums import RunMode
from kalshi_weather.engines import apply_shadow_decision, run_market_decision_cycle
from kalshi_weather.engines.live_execution import maybe_place_live_order
from kalshi_weather.analytics import build_provider_reliability_report, extract_provider_reliability_weights
from kalshi_weather.analytics.recommendation_log import record_recommendation
from kalshi_weather.ingestion.adapters import (
    KalshiOpenMarketsAdapter,
    KalshiOrderbookAdapter,
    KalshiTradeAdapter,
    NwsGridForecastAdapter,
    NwsHourlyForecastAdapter,
    NwsObservationAdapter,
    OpenMeteoEnsembleAdapter,
)
from kalshi_weather.market.payloads import market_definition_from_payload
from kalshi_weather.analytics.opportunities import parse_market_date
from kalshi_weather.settlement.rule_parser import SettlementRuleParseError
from kalshi_weather.storage import DerivedAnalyticsStore, FileRawStore, FileReferenceRegistry, SQLiteStateStore
from kalshi_weather.utils.serde import to_jsonable


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the weather decision cycle for all or selected cities.")
    parser.add_argument(
        "--city-id",
        action="append",
        dest="city_ids",
        help="Limit the run to one or more city ids (repeatable).",
    )
    args = parser.parse_args()
    selected_city_ids = {city_id.lower() for city_id in (args.city_ids or [])}

    # Install the hard cycle-budget signal so a hung HTTP call cannot
    # bleed into the next 30-min slot.
    _install_cycle_timeout(MAX_CYCLE_SECONDS)
    cycle_start_wall = time.monotonic()

    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    raw_store = FileRawStore("data/raw")
    derived_store = DerivedAnalyticsStore("data/derived")
    state_store = SQLiteStateStore("data/state/runtime.sqlite3")
    kill_switch = state_store.get_kill_switch("GLOBAL")
    active_kill_switch = bool(kill_switch and kill_switch.get("state") == "ACTIVE")
    nws_client = NwsWeatherClient()
    open_meteo_client = OpenMeteoClient(forecast_days=3)
    kalshi_client = KalshiPublicClient()

    # Preflight — fail loudly and early instead of silently producing junk.
    preflight_failures = _preflight(
        state_store,
        kalshi_client,
        live_enabled=(os.environ.get("LIVE_ORDERS_ENABLED", "0").strip() == "1"),
    )
    if preflight_failures:
        print(json.dumps({
            "ran_at": datetime.now(timezone.utc).isoformat(),
            "preflight_failed": True,
            "failures": preflight_failures,
            "decision_count": 0,
            "city_reports": [],
        }, indent=2))
        sys.exit(2)

    city_reports = []
    city_failures: list[dict] = []
    total_decisions = 0
    baseline_open_positions = [
        (position.city_id, position)
        for position in state_store.list_shadow_positions()
        if position.lifecycle_status == "OPEN"
    ]
    baseline_open_position_signals = []
    for open_city_id, position in baseline_open_positions:
        latest_signal = next(
            (
                payload
                for payload in state_store.list_decision_payloads(open_city_id)
                if str(payload.get("market_ticker") or "") == position.market_ticker
            ),
            None,
        )
        if latest_signal is not None:
            baseline_open_position_signals.append(latest_signal)
    for context in registry.iter_city_contexts(seed):
        station = context.station
        city_profile = context.city_profile
        series = context.series_definition
        if selected_city_ids and city_profile.city_id not in selected_city_ids:
            continue
        city_start = time.monotonic()
        try:
            report = _process_city(
                state_store=state_store,
                raw_store=raw_store,
                derived_store=derived_store,
                nws_client=nws_client,
                open_meteo_client=open_meteo_client,
                kalshi_client=kalshi_client,
                station=station,
                city_profile=city_profile,
                series=series,
                baseline_open_positions=baseline_open_positions,
                baseline_open_position_signals=baseline_open_position_signals,
                active_kill_switch=active_kill_switch,
            )
            if report is None:
                continue
            city_reports.append(report)
            total_decisions += report.get("decision_count", 0)
            elapsed = time.monotonic() - city_start
            if elapsed > MAX_CITY_SECONDS:
                # Soft-warn — we didn't abort the city, just flag for the report
                # so we can see which cities are slow.
                city_failures.append({
                    "city_id": city_profile.city_id,
                    "kind": "slow",
                    "elapsed_seconds": round(elapsed, 1),
                })
        except CycleTimeoutError:
            # Re-raise so the outer alarm handler bubbles up to main().
            city_failures.append({
                "city_id": city_profile.city_id,
                "kind": "cycle_timeout_during_city",
                "elapsed_seconds": round(time.monotonic() - city_start, 1),
            })
            raise
        except Exception as exc:  # noqa: BLE001
            # One city's failure must not kill the cycle. Capture, log, continue.
            tb = traceback.format_exc(limit=3)
            print(f"[CYCLE] ✗ city {city_profile.city_id} failed: {exc}\n{tb}")
            city_failures.append({
                "city_id": city_profile.city_id,
                "kind": "exception",
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:300],
                "elapsed_seconds": round(time.monotonic() - city_start, 1),
            })
            continue

    # Cancel the cycle timeout — we made it to the end cleanly.
    signal.alarm(0)
    cycle_elapsed = round(time.monotonic() - cycle_start_wall, 1)

    output = {
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "cycle_elapsed_seconds": cycle_elapsed,
        "decision_count": total_decisions,
        "city_reports": city_reports,
        "city_failures": city_failures,
        "preflight_failed": False,
    }
    print(json.dumps(to_jsonable(output), indent=2, sort_keys=True))


def _process_city(  # noqa: PLR0913 — orchestration helper; many deps by design
    *,
    state_store,
    raw_store,
    derived_store,
    nws_client,
    open_meteo_client,
    kalshi_client,
    station,
    city_profile,
    series,
    baseline_open_positions,
    baseline_open_position_signals,
    active_kill_switch: bool,
) -> dict | None:
    """Run the decision cycle for one city. Returns a city_report dict or None.

    Raises only CycleTimeoutError. All other failures bubble up to caller.
    """
    state_store.ensure_default_qualification(city_profile.city_id)
    qualification = state_store.get_qualification_state(city_profile.city_id)
    if qualification is None:
        return None

    # T1.3 persistence baseline — looked up PER MARKET because settlement
    # date varies (today's market vs tomorrow's scouting market need
    # different "yesterday" anchors). Defined as a closure so the per-market
    # loop can call it cheaply with the market_date in scope.
    from decimal import Decimal as _D

    def _yesterday_high_for_market(market_settle_date) -> _D | None:
        """Return yesterday-relative-to-market-settlement high in °F, or None.

        For today's market: yesterday = today-1, settlement is on file.
        For tomorrow's market: yesterday = today, hasn't settled yet → None.
        Past markets: shouldn't happen, but return None defensively.
        """
        if market_settle_date is None:
            return None
        local_today = datetime.now(ZoneInfo(station.timezone)).date()
        if market_settle_date <= local_today - timedelta(days=1):
            return None
        # Look up (market_settle_date - 1)
        anchor_date = (market_settle_date - timedelta(days=1)).isoformat()
        try:
            settlement = state_store.get_market_settlement(
                city_profile.city_id, anchor_date
            )
            if settlement:
                raw_high = settlement.get("daily_high_f")
                if raw_high is not None:
                    return _D(str(raw_high))
        except Exception as exc:  # observational only — never fatal
            print(f"[WARN] persistence lookup failed for "
                  f"{city_profile.city_id} {anchor_date}: {exc}")
        return None

    obs_adapter = NwsObservationAdapter(nws_client, station, limit=4)
    obs_raw = obs_adapter.fetch_raw()
    raw_store.write(obs_raw)
    observations = [env.record for env in obs_adapter.normalize(obs_raw)]
    state_store.save_observations(observations)

    forecast_adapter = NwsHourlyForecastAdapter(nws_client, station)
    forecast_raw = forecast_adapter.fetch_raw()
    raw_store.write(forecast_raw)
    forecasts = [env.record for env in forecast_adapter.normalize(forecast_raw)]
    grid_forecast_adapter = NwsGridForecastAdapter(nws_client, station)
    grid_forecast_raw = grid_forecast_adapter.fetch_raw()
    raw_store.write(grid_forecast_raw)
    forecasts.extend(env.record for env in grid_forecast_adapter.normalize(grid_forecast_raw))
    # Open-Meteo multi-model ensemble — 9 independent models in one call.
    # Failures here are non-fatal — NWS-only operation is the fallback.
    try:
        om_adapter = OpenMeteoEnsembleAdapter(open_meteo_client, station, forecast_days=3)
        om_raw = om_adapter.fetch_raw()
        raw_store.write(om_raw)
        forecasts.extend(env.record for env in om_adapter.normalize(om_raw))
    except Exception as exc:  # pragma: no cover — operational guard
        print(f"[WARN] Open-Meteo fetch failed for {city_profile.city_id}: {exc}")
    state_store.save_forecasts(forecasts)
    calibration_report = build_provider_reliability_report(
        forecasts=state_store.get_all_forecasts(station.station_id),
        observations=state_store.get_all_observations(station.station_id),
        station=station,
    )
    derived_store.write_provider_reliability(city_profile.city_id, calibration_report)
    provider_reliability = extract_provider_reliability_weights(calibration_report)

    market_adapter = KalshiOpenMarketsAdapter(kalshi_client, series.series_ticker)
    market_raw = market_adapter.fetch_raw()
    raw_store.write(market_raw)
    market_snapshots = [env.record for env in market_adapter.normalize(market_raw)]
    state_store.save_market_snapshots(market_snapshots)
    market_payloads = json.loads(str(market_raw.payload)).get("markets", [])
    series_payload = kalshi_client.get_series(series.series_ticker)
    fee_multiplier = (series_payload.get("series", {}) or {}).get("fee_multiplier") or 1

    decision_results: list[dict] = []
    for market_snapshot, payload in zip(market_snapshots, market_payloads, strict=False):
        try:
            market_definition = market_definition_from_payload(payload)
            _ = market_definition.rules_primary
        except Exception:
            continue
        try:
            local_today = datetime.now(ZoneInfo(station.timezone)).date()
            market_date = parse_market_date(market_snapshot.market_ticker)
            scouting_future_market = market_date is not None and market_date > local_today
            decision_open_positions = [] if scouting_future_market else baseline_open_positions
            decision_open_position_signals = [] if scouting_future_market else baseline_open_position_signals
            orderbook_adapter = KalshiOrderbookAdapter(kalshi_client, market_snapshot.market_ticker)
            orderbook_raw = orderbook_adapter.fetch_raw()
            raw_store.write(orderbook_raw)
            orderbook = orderbook_adapter.normalize(orderbook_raw)[0].record
            state_store.save_orderbook_snapshot(orderbook)
            trade_adapter = KalshiTradeAdapter(kalshi_client, market_snapshot.market_ticker, limit=200)
            trade_raw = trade_adapter.fetch_raw()
            raw_store.write(trade_raw)
            trades = [env.record for env in trade_adapter.normalize(trade_raw)]
            state_store.save_trade_snapshots(trades)
            recent_orderbooks = state_store.get_recent_orderbook_snapshots(
                market_snapshot.market_ticker,
                limit=10,
            )
            result = run_market_decision_cycle(
                market=market_snapshot,
                market_definition=market_definition,
                orderbook=orderbook,
                recent_orderbooks=recent_orderbooks,
                recent_trades=state_store.get_recent_trade_snapshots(market_snapshot.market_ticker, limit=50),
                observations=state_store.get_recent_observations(station.station_id, limit=4),
                forecasts=state_store.get_latest_forecasts(station.station_id),
                city_profile=city_profile,
                station=station,
                qualification_state=qualification.state,
                provider_reliability=provider_reliability,
                provider_calibration_report=calibration_report,
                run_mode=RunMode.SHADOW,
                fee_multiplier=int(fee_multiplier),
                open_positions=decision_open_positions,
                open_position_signals=decision_open_position_signals,
                active_kill_switch=active_kill_switch,
                yesterday_high_f=_yesterday_high_for_market(market_date),
            )
        except SettlementRuleParseError:
            continue
        except Exception as exc:  # noqa: BLE001
            # Per-market failure must not kill the city. Log + continue.
            print(f"[WARN] market {market_snapshot.market_ticker} failed: {exc}")
            continue
        state_store.save_decision(result.explanation)
        recommendation_id: str | None = None
        try:
            from kalshi_weather.settlement.rule_parser import parse_settlement_rule
            settlement_rule = parse_settlement_rule(market_definition, station)
            recommendation_id = record_recommendation(state_store, result.explanation, settlement_rule)
        except Exception as exc:  # pragma: no cover — observational only
            print(f"[WARN] recommendation_log failed for {market_snapshot.market_ticker}: {exc}")
        shadow = None
        if result.selected_edge is not None:
            try:
                shadow = apply_shadow_decision(
                    store=state_store,
                    city_id=city_profile.city_id,
                    explanation=result.explanation,
                    edge=result.selected_edge,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] shadow apply failed for {market_snapshot.market_ticker}: {exc}")
                shadow = None
            if shadow is not None and shadow.fill is not None:
                try:
                    live_result = maybe_place_live_order(
                        store=state_store,
                        explanation=result.explanation,
                        edge=result.selected_edge,
                        shadow_fill=shadow.fill,
                    )
                except Exception as exc:  # noqa: BLE001
                    print(f"[WARN] live order failed for {market_snapshot.market_ticker}: {exc}")
                    live_result = None
                if live_result is not None:
                    if live_result.placed:
                        print(f"[LIVE-EXEC] ✓ PLACED {result.explanation.market_ticker} "
                              f"{result.selected_edge.side} {live_result.quantity}@{live_result.price_cents}c")
                    elif live_result.dry_run:
                        print(f"[LIVE-EXEC] DRY-RUN preview {result.explanation.market_ticker} "
                              f"{result.selected_edge.side} {live_result.quantity}@{live_result.price_cents}c")
                    elif live_result.blocker_reason and live_result.blocker_reason != "live_orders_disabled_via_env":
                        print(f"[LIVE-EXEC] BLOCKED {result.explanation.market_ticker}: "
                              f"{live_result.blocker_reason}")
                        # Roll back the shadow row we wrote a moment ago — we
                        # don't actually hold this position on Kalshi. Without
                        # rollback, dedup blocks the next cycle from re-trying
                        # this same edge and the shadow_positions table claims
                        # exposure we don't have. (2026-05-17 bug.)
                        try:
                            state_store.delete_shadow_fill(shadow.fill.shadow_fill_id)
                            state_store.delete_shadow_position(
                                city_profile.city_id, result.explanation.market_ticker
                            )
                            print(f"[LIVE-EXEC] rolled back shadow_fill for "
                                  f"{result.explanation.market_ticker}")
                            shadow = None
                        except Exception as rb_exc:
                            print(f"[LIVE-EXEC] WARN: shadow rollback failed: {rb_exc}")
            if recommendation_id is not None:
                try:
                    if shadow is not None and shadow.fill is not None:
                        state_store.mark_recommendation_filled(recommendation_id)
                    else:
                        blocker = "fill_layer_blocked"
                        if result.selected_edge is not None:
                            edge = result.selected_edge
                            from decimal import Decimal as _D
                            if edge.p_model < _D("0.70") or edge.p_model > _D("0.97"):
                                blocker = "outside_favorite_range"
                            elif edge.executable_ev_per_contract < _D("0.005"):
                                blocker = "ev_below_floor"
                            else:
                                market_disagreement = abs(edge.p_model - edge.p_market_exec)
                                if market_disagreement > _D("0.30"):
                                    blocker = "high_market_disagreement"
                                else:
                                    fs = result.explanation.forecast_summary or {}
                                    spread = fs.get("provider_spread_f")
                                    if spread is not None and _D(str(spread)) > _D("8.0"):
                                        blocker = "model_consensus_too_weak"
                        state_store.mark_recommendation_blocked(recommendation_id, blocker)
                except Exception:
                    pass
        decision_results.append(
            {
                "market_ticker": result.explanation.market_ticker,
                "decision": result.explanation.final_decision.value,
                "selected_side": result.selected_edge.side if result.selected_edge else None,
                "selected_ev": str(result.selected_edge.executable_ev_per_contract)
                if result.selected_edge
                else None,
                "shadow_fill_id": shadow.fill.shadow_fill_id if shadow and shadow.fill else None,
            }
        )

    return {
        "city_id": city_profile.city_id,
        "series_ticker": series.series_ticker,
        "qualification_state": qualification.state.value,
        "provider_reliability": calibration_report.get("provider_weights", {}),
        "forecast_calibration_sample_sufficient": calibration_report.get("sample_sufficient", False),
        "decision_count": len(decision_results),
        "decisions": decision_results,
    }


if __name__ == "__main__":
    main()
