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
from kalshi_weather.clients.nws_afd import NwsAfdClient
from kalshi_weather.clients.spc import fetch_spc_outlook_for_cities
from kalshi_weather.engines.afd_extractor import extract_afd_signals_cached


# ── Per-cycle hard limits ──────────────────────────────────────────────
# These exist because of the 2026-05-16 evening outage: the bot was happily
# running long fetches when macOS revoked the launchd permission, and we
# learned nothing was watching for "this cycle never finished" failures.
# A hung HTTP call or a single city raising an unhandled exception used to
# kill the entire cycle. With these limits each city is isolated and the
# cycle has a hard ceiling so it can never overlap the next scheduled slot.
MAX_CYCLE_SECONDS = 1320  # 22 min — raised 2026-05-25 PM (fifth time)
                          # Afternoon cycles hit 1100-1150s consistently
                          # (candidate count doubles vs overnight). Temporary
                          # bump while parallelization (task #11) lands;
                          # afterwards can drop to ~600s comfortably.
                          # Earlier (now-stale) comment from morning bump:
                          # ----- 1080 (5/25 AM): -----
                          # after 12:30 + 1:00 AM ET slots timed out at
                          # 901s/900s respectively. The 900s ceiling sat
                          # right at the cliff edge of typical 15-min
                          # cycle runtime — natural variance of ±10s was
                          # randomly tipping cycles over. 1080s gives a
                          # ~3 min buffer, the lock TTL (960s in the
                          # wrapper) means the next slot will no-op if
                          # this one is still running past 16 min, and
                          # the wrapper's external SIGKILL watchdog at
                          # HARD_CYCLE_KILL (1000s default — needs bump
                          # too) is the final enforcement.
                          # Earlier history kept for context:
                          #   2026-05-18 PM-mid:   900 (persistent slowdown)
                          # after cycles consistently ran 10-11+ min during
                          # the 1 PM ET range. The 600s ceiling kept firing
                          # SIGALRM on cycles that legitimately needed 10:11.
                          # Slowdown appears persistent (Open-Meteo retries
                          # + 20 cities × 11 series including MAY 19 lookahead
                          # = roughly double the per-city HTTP load vs earlier).
                          # 15 min ceiling matches the 15-min slot cadence —
                          # cycles will use the FULL slot but won't bleed into
                          # the next. Lock TTL bumped to 960s. If the next
                          # slot's launchd fires while this one is still
                          # running, the lock causes the new cycle to no-op
                          # which is the right behavior.
                          # Original history:
                          #   2026-05-16: 240 (pre-KXLOW)
                          #   2026-05-17: 360 (post-KXLOW)
                          #   2026-05-18 AM: 480 (drift)
                          #   2026-05-18 PM-early: 600 (rate-limit retries)
                          #   2026-05-18 PM-mid:   900 (persistent slowdown)
                          # Note: Python's SIGALRM can be swallowed by C-level
                          # blocking HTTP calls, so this ceiling is best-effort
                          # — the watchdog's stuck-cycle SIGKILL is the
                          # belt-and-suspenders enforcement.
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


def _maybe_close_if_underwater(
    *,
    state_store,
    market_ticker: str,
    orderbook,
    city_id: str,
    p_model_at_close=None,
) -> None:
    """If we own this market and selling now would be a loss, close.

    Idempotent — safe to call from inside the per-market loop. Looks up
    the most recent PLACED live order for the ticker, computes pnl vs
    the top opposite-side bid, and if pnl < 0 places a sell IOC. We do
    NOT close profitable positions on EXIT signals (let them ride).

    p_model_at_close: optional Decimal — the model's probability for the
    held side at the moment of close. Forwarded to maybe_close_position
    so the CLOSED row's payload carries ``exit_metadata.p_model_at_close``,
    which the re-entry policy needs later in the day to compare against
    a new signal.
    """
    import json as _json
    from decimal import Decimal as _D
    # Find the most recent PLACED live order for this ticker
    try:
        with state_store._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM live_orders "
                "WHERE market_ticker = ? AND status LIKE 'PLACED_%' "
                "AND status NOT LIKE 'CLOSED_%' "
                "ORDER BY created_at DESC LIMIT 1",
                (market_ticker,),
            ).fetchone()
    except Exception:
        return
    if not row:
        return  # not held, or already closed
    payload = _json.loads(row[0])
    if payload.get("close_intent"):
        return  # this row IS a close — skip
    side_held = payload.get("side")
    if side_held not in ("yes", "no"):
        return
    entry_price_cents = int(payload.get("yes_price") or payload.get("no_price") or 0)
    if entry_price_cents <= 0:
        return
    # Already closed earlier today?
    try:
        with state_store._connect() as conn:
            closed_row = conn.execute(
                "SELECT count(*) FROM live_orders "
                "WHERE market_ticker = ? AND status LIKE 'CLOSED_%'",
                (market_ticker,),
            ).fetchone()
        if closed_row and int(closed_row[0]) > 0:
            return
    except Exception:
        pass

    # Best bid on the side we hold (= the price we could SELL at)
    try:
        bids = orderbook.yes_bids_ladder if side_held == "yes" else orderbook.no_bids_ladder
    except Exception:
        return
    if not bids:
        return
    try:
        best_bid_dollars = max(_D(str(price)) for price, _qty in bids)
    except Exception:
        return
    best_bid_cents = int(round(float(best_bid_dollars) * 100))
    if best_bid_cents <= 0 or best_bid_cents > 99:
        return

    # Only close if underwater. Profit positions ride to settlement (or
    # future profit-taking rule). pnl per contract in cents:
    pnl_cents = best_bid_cents - entry_price_cents
    if pnl_cents >= 0:
        return  # in profit — hold

    # ── EV-based close gate (added 2026-05-24) ───────────────────────────
    # Underwater alone is not a reason to sell. If the model still says
    # the held side is more likely to win than the market is pricing in,
    # holding to settlement has higher expected value than selling now.
    # Without this gate, the bot was selling at 2¢-37¢ contracts that the
    # model still gave 30-93% win probability — locking in losses that the
    # path uncertainty had already priced into the orderbook overreaction.
    # 7-day audit (May 17-24): 6 of 13 closes with p_model_at_close data
    # were demonstrably -EV vs holding; total avoidable loss ~$1.20.
    #
    # Math: EV_hold per contract = held_prob * 100c. EV_sell = best_bid_c.
    # Skip the close iff EV_hold > EV_sell + buffer. 3¢ buffer accounts for
    # round-trip fees and tail risk that the model is overconfident.
    if p_model_at_close is not None:
        try:
            p_yes = _D(str(p_model_at_close))
            held_prob = p_yes if side_held == "yes" else (_D("1") - p_yes)
            ev_hold_c = held_prob * _D("100")
            if ev_hold_c > _D(best_bid_cents) + _D("3"):
                print(f"[EXIT-EXEC] HOLD {market_ticker} {side_held} "
                      f"@ {best_bid_cents}c (entry {entry_price_cents}c, "
                      f"pnl {pnl_cents}c) — model says hold: "
                      f"held_prob={float(held_prob):.3f} "
                      f"ev_hold={float(ev_hold_c):.1f}c > sell {best_bid_cents}c+3c buffer")
                return
        except Exception as exc:
            # If EV math blows up, fall through to the legacy underwater
            # close — safer to close than to leave a bug-induced hold.
            print(f"[EXIT-EXEC] EV-gate calc failed for {market_ticker}: {exc}")

    # Underwater + EXIT signal + model agrees → close to limit further downside
    from kalshi_weather.engines.live_execution import maybe_close_position
    res = maybe_close_position(
        store=state_store,
        market_ticker=market_ticker,
        side_held=side_held,
        entry_price_cents=entry_price_cents,
        current_sell_bid_cents=best_bid_cents,
        p_model_at_close=p_model_at_close,
        close_reason="underwater_exit_signal",
    )
    if res.placed:
        # Include held_prob / ev_hold in the print so weekly close audits
        # can confirm the EV gate ran and agreed with the close.
        ev_tag = ""
        if p_model_at_close is not None:
            try:
                p_yes = _D(str(p_model_at_close))
                held_prob = p_yes if side_held == "yes" else (_D("1") - p_yes)
                ev_tag = (f" held_prob={float(held_prob):.3f} "
                          f"ev_hold={float(held_prob * _D('100')):.1f}c")
            except Exception:
                ev_tag = ""
        print(f"[EXIT-EXEC] ✓ CLOSED {market_ticker} {side_held} "
              f"@ {best_bid_cents}c (entry {entry_price_cents}c, "
              f"pnl {pnl_cents}c){ev_tag}")
        # Mark shadow_position as closed so it doesn't keep firing EXIT
        try:
            state_store.delete_shadow_position(city_id, market_ticker)
        except Exception:
            pass
    elif res.dry_run:
        print(f"[EXIT-EXEC] DRY-RUN close preview {market_ticker} "
              f"{side_held} @ {best_bid_cents}c (entry {entry_price_cents}c)")
    elif res.blocker_reason and res.blocker_reason != "live_orders_disabled_via_env":
        print(f"[EXIT-EXEC] BLOCKED {market_ticker}: {res.blocker_reason}")


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
from kalshi_weather.engines.live_execution import maybe_close_position, maybe_place_live_order
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

    # SPC convective outlook — fetched ONCE per cycle (single API call,
    # then point-in-polygon for each city). Best-effort; on failure all
    # cities get fetched_ok=False and rank=0.
    spc_cities = {
        st.station_id: (float(st.latitude), float(st.longitude))
        for st in seed.stations
    }
    try:
        spc_by_station = fetch_spc_outlook_for_cities(spc_cities)
    except Exception as exc:  # observational guard
        print(f"[SPC] WARN: fetch failed: {exc}")
        spc_by_station = {}

    city_reports = []
    city_failures: list[dict] = []
    total_decisions = 0
    # 2026-05-25: shared per-cycle HTTP cache. Cuts ~60 duplicate Open-Meteo
    # + DB calls per cycle (city-level signals are series-independent but
    # `iter_city_contexts` yields one context per series, so each city was
    # fetched twice). Empirical wins: -80s GEFS, -40s soil, -10s NWS-rev.
    cycle_cache: dict = {}
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
    # 2026-05-25: parallel per-city processing via ThreadPoolExecutor.
    # Work is HTTP-bound (NWS + Open-Meteo + Kalshi) so GIL is not a
    # constraint. Thread-safety verified:
    #   - SQLiteStateStore: WAL mode, per-call connection, 30s busy_timeout
    #   - HTTP clients: requests/urllib are thread-safe
    #   - cycle_cache: dict ops are GIL-atomic; worst case two threads
    #     race to fetch the same uncached endpoint → redundant work, not
    #     wrong results
    #   - Open-Meteo circuit breaker is Lock-protected at module level
    # Stdout is NOT captured per-city — contextlib.redirect_stdout rebinds
    # sys.stdout process-wide and would corrupt across threads. Log lines
    # may interleave between cities; print() itself is atomic per call so
    # individual lines remain intact (just out-of-city-order). The lines
    # are tagged ([SPC] city_id: ..., [SOIL] city_id: ...) so grep-by-city
    # still works.
    # SIGALRM-based CycleTimeoutError only delivers to the main thread; on
    # raise we shutdown the pool with cancel_futures=True so workers stop.
    import concurrent.futures as _cf

    contexts_to_run = []
    for context in registry.iter_city_contexts(seed):
        if selected_city_ids and context.city_profile.city_id not in selected_city_ids:
            continue
        contexts_to_run.append(context)

    def _run_one_city(context):
        """Process one city in a worker thread.

        Returns: (context, report_or_None, failure_dict_or_None)
        """
        start = time.monotonic()
        try:
            report = _process_city(
                state_store=state_store,
                raw_store=raw_store,
                derived_store=derived_store,
                nws_client=nws_client,
                open_meteo_client=open_meteo_client,
                kalshi_client=kalshi_client,
                station=context.station,
                city_profile=context.city_profile,
                series=context.series_definition,
                baseline_open_positions=baseline_open_positions,
                baseline_open_position_signals=baseline_open_position_signals,
                active_kill_switch=active_kill_switch,
                spc_for_station=spc_by_station.get(context.station.station_id),
                cycle_cache=cycle_cache,
            )
            elapsed = time.monotonic() - start
            failure = None
            if report is not None and elapsed > MAX_CITY_SECONDS:
                failure = {
                    "city_id": context.city_profile.city_id,
                    "kind": "slow",
                    "elapsed_seconds": round(elapsed, 1),
                }
            return (context, report, failure)
        except Exception as exc:  # noqa: BLE001
            tb = traceback.format_exc(limit=3)
            print(f"[CYCLE] ✗ city {context.city_profile.city_id} failed: {exc}\n{tb}")
            return (context, None, {
                "city_id": context.city_profile.city_id,
                "kind": "exception",
                "error_type": type(exc).__name__,
                "error_message": str(exc)[:300],
                "elapsed_seconds": round(time.monotonic() - start, 1),
            })

    # Worker count: 4 → 8 on 2026-05-28. Cycles were running ~21 min on
    # 4 workers because 40 city-contexts / 4 = 10 sequential batches.
    # Work is I/O-bound (HTTP wait), so doubling threads ≈ halves wall time.
    # Open-Meteo circuit breaker protects against the rate-limit pool (~10k
    # req/day shared). Tunable via env KALSHI_WEATHER_CITY_WORKERS.
    _MAX_WORKERS = int(os.environ.get("KALSHI_WEATHER_CITY_WORKERS", "8"))
    with _cf.ThreadPoolExecutor(max_workers=_MAX_WORKERS, thread_name_prefix="city") as pool:
        futures = [pool.submit(_run_one_city, ctx) for ctx in contexts_to_run]
        try:
            for fut in _cf.as_completed(futures):
                _ctx, report, failure = fut.result()
                if report is not None:
                    city_reports.append(report)
                    total_decisions += report.get("decision_count", 0)
                if failure is not None:
                    city_failures.append(failure)
        except CycleTimeoutError:
            # SIGALRM delivered to main thread. Stop the pool fast and re-raise.
            pool.shutdown(wait=False, cancel_futures=True)
            raise

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
    spc_for_station=None,
    cycle_cache: dict | None = None,
) -> dict | None:
    """Run the decision cycle for one city. Returns a city_report dict or None.

    Raises only CycleTimeoutError. All other failures bubble up to caller.
    """
    state_store.ensure_default_qualification(city_profile.city_id)
    qualification = state_store.get_qualification_state(city_profile.city_id)
    if qualification is None:
        return None

    # 2026-05-25: per-cycle HTTP cache. `iter_city_contexts` yields TWO
    # contexts per city (HIGH series + LOW series), so without caching
    # the city-level fetches (SOIL, GEFS, NWS-REVISIONS) fire twice per
    # city → ~80s of duplicate HTTP per cycle. Cache by station_id since
    # all these signals are series-independent. Cache lives only for
    # this cycle's main() invocation — next cycle starts fresh.
    if cycle_cache is None:
        cycle_cache = {}
    _cache_soil = cycle_cache.setdefault("soil", {})
    _cache_gefs = cycle_cache.setdefault("gefs", {})
    _cache_nws_rev = cycle_cache.setdefault("nws_rev", {})

    # ── External signals (now LIVE — feed path engine via current_state) ──
    # 2026-05-22: AFD/LLM extraction gated behind ENABLE_AFD_EXTRACTION.
    # 200-sample backtest showed no AFD signal predicted forecast error at
    # p<0.05; cost was 4-8 min/cycle on 2-vCPU box. Path engine handles
    # afd_* = None as a no-op (no uncertainty modulation).
    afd_confidence: str | None = None
    afd_model_spread_flag: bool | None = None
    afd_regime: str | None = None
    afd_mentioned_high_f: int | None = None
    if os.environ.get("ENABLE_AFD_EXTRACTION", "0") == "1":
        try:
            afd_product = NwsAfdClient.fetch_latest(station.wfo_office)
            if afd_product and afd_product.raw_text:
                extraction = extract_afd_signals_cached(
                    wfo=station.wfo_office,
                    product_id=afd_product.product_id,
                    afd_text=afd_product.raw_text,
                )
                afd_confidence = extraction.confidence
                afd_model_spread_flag = extraction.model_spread_flag
                afd_regime = extraction.regime
                afd_mentioned_high_f = extraction.mentioned_today_high_f
                print(f"[AFD] {city_profile.city_id}: "
                      f"wfo={station.wfo_office} conf={extraction.confidence} "
                      f"spread={extraction.model_spread_flag} regime={extraction.regime} "
                      f"high={extraction.mentioned_today_high_f} "
                      f"failed={extraction.extraction_failed}")
        except Exception as exc:  # never fatal
            print(f"[AFD] {city_profile.city_id}: lookup failed: {exc}")

    # SPC outlook for this city (precomputed once per cycle, passed in)
    if spc_for_station is not None:
        print(f"[SPC] {city_profile.city_id}: category={spc_for_station.category} "
              f"rank={spc_for_station.rank} ok={spc_for_station.fetched_ok}")

    # 2026-05-23: NWS forecast revision pace over last 24h. High revisions =
    # NWS has been actively rewriting the forecast for today; small
    # uncertainty bump in path engine.
    nws_revisions_24h: int | None = _cache_nws_rev.get(station.station_id, "MISS")
    if nws_revisions_24h == "MISS":
        nws_revisions_24h = None
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            with state_store._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(DISTINCT provider_run_time) FROM forecasts "
                    "WHERE station_id=? AND provider_id IN ('NWS','NWS_GRID') "
                    "AND provider_run_time >= ?",
                    (station.station_id, cutoff),
                ).fetchone()
                if row and row[0] is not None:
                    nws_revisions_24h = int(row[0])
                    print(f"[NWS-REVISIONS] {city_profile.city_id}: {nws_revisions_24h} distinct runs in last 24h")
        except Exception as exc:
            print(f"[NWS-REVISIONS] {city_profile.city_id}: lookup failed: {exc}")
        _cache_nws_rev[station.station_id] = nws_revisions_24h

    # GOES proxy — surface the real-time sky_cover_code from NWS METAR
    # observations (already ingested). This is NOT true GOES satellite
    # imagery; it's the airport-station cloud observation, which is
    # what feeds METAR and what GOES is regridded to anyway. Free, no new
    # API. If the user wants true GOES (netCDF + AWS S3 + regridding to
    # station lat/lon), that's a follow-up.
    try:
        recent_obs = state_store.get_recent_observations(station.station_id, limit=1)
        if recent_obs:
            sky_code = recent_obs[0].sky_cover_code
            obs_pct_map = {"CLR": 0, "FEW": 25, "SCT": 50, "BKN": 75, "OVC": 100}
            obs_pct = obs_pct_map.get(sky_code, None)
            print(f"[GOES-PROXY] {city_profile.city_id}: sky={sky_code} "
                  f"(~{obs_pct}% cover)" if obs_pct is not None
                  else f"[GOES-PROXY] {city_profile.city_id}: sky={sky_code} (unknown code)")
    except Exception:
        pass

    # Soil moisture (single Open-Meteo call, near-surface) — cached per cycle
    soil = _cache_soil.get(station.station_id, "MISS")
    if soil == "MISS":
        soil = None
        try:
            soil = open_meteo_client.fetch_soil_moisture(
                latitude=float(station.latitude),
                longitude=float(station.longitude),
            )
            if soil:
                print(f"[SOIL] {city_profile.city_id}: "
                      f"{soil['variable']} now={soil['current_value']:.3f} "
                      f"24h_mean={soil['mean_24h']:.3f}")
        except Exception as exc:
            print(f"[SOIL] {city_profile.city_id}: lookup failed: {exc}")
        _cache_soil[station.station_id] = soil

    # 2026-05-23: GEFS ensemble probabilistic forecast — 30 perturbation
    # members + control from NOAA GEFS, fetched via Open-Meteo's free
    # ensemble-api. Inter-member spread = true probabilistic uncertainty,
    # independent of our model-fusion math. Path engine uses std as a small
    # uncertainty addon (capped at 0.03).
    gefs_std_f: float | None = None
    gefs_mean_f: float | None = None
    gefs_member_count: int | None = None
    cached_gefs = _cache_gefs.get(station.station_id, "MISS")
    if cached_gefs == "MISS":
        gefs = None
        try:
            gefs = open_meteo_client.fetch_gefs_ensemble_high(
                latitude=float(station.latitude),
                longitude=float(station.longitude),
                timezone_name=station.timezone,
            )
            if gefs:
                print(f"[GEFS-ENSEMBLE] {city_profile.city_id}: "
                      f"n={int(gefs['count'])} mean={float(gefs['mean']):.1f}°F "
                      f"std={float(gefs['std']):.2f}°F target={gefs['target_date']}")
        except Exception as exc:
            print(f"[GEFS-ENSEMBLE] {city_profile.city_id}: lookup failed: {exc}")
        _cache_gefs[station.station_id] = gefs
    else:
        gefs = cached_gefs
    if gefs:
        gefs_std_f = float(gefs["std"])
        gefs_mean_f = float(gefs["mean"])
        gefs_member_count = int(gefs["count"])

    # 2026-05-23: Intraday Bayesian update from ASOS observations.
    # Compare today's already-observed hours vs the latest NWS hourly forecast
    # for those same hours. Mean residual (obs - forecast) is a posterior
    # signal for the remaining-day high. Path engine uses it as a bounded
    # threshold shift (capped ±2°F, requires >=3 sample hours).
    intraday_bias_f: float | None = None
    intraday_sample_hours: int | None = None
    try:
        local_today = datetime.now(ZoneInfo(station.timezone)).date()
        # Today's hourly obs (one per hour, latest if multiple)
        all_obs = state_store.get_all_observations(station.station_id)
        obs_by_hour: dict = {}
        for o in all_obs:
            if o.temperature_f is None:
                continue
            local_dt = o.event_time.astimezone(ZoneInfo(station.timezone))
            if local_dt.date() != local_today:
                continue
            hour_key = local_dt.replace(minute=0, second=0, microsecond=0)
            # Keep the obs closest to top of hour
            prev = obs_by_hour.get(hour_key)
            if prev is None or abs(local_dt.minute - 0) < abs(prev[0].minute - 0):
                obs_by_hour[hour_key] = (local_dt, float(o.temperature_f))
        # Latest NWS hourly forecast (NWS_GRID has hourly resolution)
        nws_forecasts = [
            f for f in state_store.get_latest_forecasts(station.station_id)
            if f.provider_id in ("NWS_GRID", "NWS")
        ]
        forecast_by_hour: dict = {}
        for snap in nws_forecasts:
            for vt, tf in zip(snap.valid_for_times, snap.hourly_temp_path_f, strict=False):
                if tf is None:
                    continue
                local_vt = vt.astimezone(ZoneInfo(station.timezone))
                if local_vt.date() != local_today:
                    continue
                hour_key = local_vt.replace(minute=0, second=0, microsecond=0)
                forecast_by_hour.setdefault(hour_key, float(tf))
        residuals = []
        for hour_key, (_, obs_t) in obs_by_hour.items():
            fc_t = forecast_by_hour.get(hour_key)
            if fc_t is not None:
                residuals.append(obs_t - fc_t)
        if len(residuals) >= 3:
            intraday_bias_f = sum(residuals) / len(residuals)
            intraday_sample_hours = len(residuals)
            print(f"[INTRADAY-BIAS] {city_profile.city_id}: "
                  f"bias={intraday_bias_f:+.2f}°F n={intraday_sample_hours}h")
    except Exception as exc:
        print(f"[INTRADAY-BIAS] {city_profile.city_id}: lookup failed: {exc}")
    # ───────────────────────────────────────────────────────────────────────

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

    # ── Pre-filter markets we know we'll auto-reject ──────────────────
    # 2026-05-28: profile showed ~200 Kalshi orderbook fetches per cycle =
    # ~800s wasted on markets that downstream gates immediately reject.
    # Filter BEFORE the prefetch:
    #   - "-B" tickers: B-no is structurally blocked (28 of 55 typical
    #     candidates); B-yes is marginal and rarely qualifies. Skipping
    #     all B markets saves ~50% of fetches.
    #   - strike_type='less': less-yes is structurally blocked (the
    #     -$5.61 lane); less-no is a marginal 5-bet lane. Acceptable
    #     tradeoff for the speed win.
    # Keep: T markets with strike_type='greater' (the proven +86% ROI
    # lane) — these are the only markets where the bot has historical
    # edge anyway.
    def _ticker_is_b(tkr: str) -> bool:
        last = tkr.rsplit('-', 1)[-1]
        return last.startswith('B')

    payload_by_ticker = {p.get('ticker'): p for p in market_payloads}
    keep_pairs: list[tuple] = []
    skipped_b = skipped_less = 0
    for ms, pl in zip(market_snapshots, market_payloads, strict=False):
        if _ticker_is_b(ms.market_ticker):
            skipped_b += 1
            continue
        if pl.get('strike_type') == 'less':
            skipped_less += 1
            continue
        keep_pairs.append((ms, pl))
    if skipped_b or skipped_less:
        print(f"[CYCLE-FILTER] {city_profile.city_id}: skipping {skipped_b} B-markets + {skipped_less} less-markets "
              f"(prefetch only {len(keep_pairs)} of {len(market_snapshots)} markets)")
    filtered_snapshots = [ms for ms, _ in keep_pairs]
    filtered_payloads  = [pl for _,  pl in keep_pairs]

    # ── Prefetch orderbook + trades for surviving markets concurrently ──
    # Before this optimization, each market did 2 sequential HTTP calls
    # inside the decision loop. Now: 6 worker threads fetch in parallel;
    # the decision loop below uses the prefetched data. Kalshi public
    # client is stateless so concurrent calls are safe.
    from concurrent.futures import ThreadPoolExecutor

    def _prefetch_market(ms) -> tuple[str, dict | None]:
        try:
            ob_adapter = KalshiOrderbookAdapter(kalshi_client, ms.market_ticker)
            ob_raw = ob_adapter.fetch_raw()
            tr_adapter = KalshiTradeAdapter(kalshi_client, ms.market_ticker, limit=200)
            tr_raw = tr_adapter.fetch_raw()
            return ms.market_ticker, {
                "ob_raw": ob_raw, "ob_adapter": ob_adapter,
                "tr_raw": tr_raw, "tr_adapter": tr_adapter,
            }
        except Exception as exc:
            return ms.market_ticker, {"error": str(exc)}

    prefetched: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for ticker, data in pool.map(_prefetch_market, filtered_snapshots):
            prefetched[ticker] = data
    # Use filtered lists for the decision loop too
    market_snapshots = filtered_snapshots
    market_payloads = filtered_payloads

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
            # Use prefetched orderbook + trades
            md = prefetched.get(market_snapshot.market_ticker, {})
            if "error" in md or "ob_raw" not in md:
                print(f"[WARN] prefetch missing for {market_snapshot.market_ticker}")
                continue
            orderbook_raw = md["ob_raw"]
            raw_store.write(orderbook_raw)
            orderbook = md["ob_adapter"].normalize(orderbook_raw)[0].record
            state_store.save_orderbook_snapshot(orderbook)
            trade_raw = md["tr_raw"]
            raw_store.write(trade_raw)
            trades = [env.record for env in md["tr_adapter"].normalize(trade_raw)]
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
                spc_outlook_rank=(spc_for_station.rank if spc_for_station else 0),
                afd_confidence=afd_confidence,
                afd_model_spread_flag=afd_model_spread_flag,
                afd_regime=afd_regime,
                afd_mentioned_today_high_f=afd_mentioned_high_f,
                nws_forecast_revisions_24h=nws_revisions_24h,
                gefs_ensemble_std_f=gefs_std_f,
                gefs_ensemble_mean_f=gefs_mean_f,
                gefs_ensemble_member_count=gefs_member_count,
                intraday_obs_forecast_bias_f=intraday_bias_f,
                intraday_obs_sample_hours=intraday_sample_hours,
            )
        except SettlementRuleParseError:
            continue
        except Exception as exc:  # noqa: BLE001
            # Per-market failure must not kill the city. Log + continue.
            print(f"[WARN] market {market_snapshot.market_ticker} failed: {exc}")
            continue
        state_store.save_decision(result.explanation)

        # ── Position exit check ──────────────────────────────────────
        # If the engine emits EXIT for a market we own AND we're currently
        # underwater (sell now would be a loss), close to avoid a bigger
        # loss at settlement. If we're in profit on an EXIT signal we hold
        # (let the position ride — current policy, can revisit).
        if result.explanation.final_decision.value == "EXIT":
            try:
                # The selected_edge carries the model's current probability
                # for the side we're closing. Forward it so the CLOSED row
                # records p_model_at_close — needed by the re-entry policy
                # later in the day to decide whether a re-buy is justified.
                _p_model_at_close = None
                if result.selected_edge is not None:
                    try:
                        from decimal import Decimal as _D
                        _p_model_at_close = _D(str(result.selected_edge.p_model))
                    except Exception:
                        _p_model_at_close = None
                _maybe_close_if_underwater(
                    state_store=state_store,
                    market_ticker=market_snapshot.market_ticker,
                    orderbook=orderbook,
                    city_id=city_profile.city_id,
                    p_model_at_close=_p_model_at_close,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[EXIT-EXEC] WARN: exit check failed for "
                      f"{market_snapshot.market_ticker}: {exc}")

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
                              f"{result.selected_edge.side} {live_result.quantity}@{live_result.price_cents}c "
                              f"[p_model={float(result.selected_edge.p_model):.3f}, "
                              f"exec_ev=${float(result.selected_edge.executable_ev_per_contract):.3f}]")
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
