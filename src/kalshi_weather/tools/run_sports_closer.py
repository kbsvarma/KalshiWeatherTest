from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from kalshi_weather.analytics.sports_closer import (
    SportsCloserConfig,
    default_now_utc,
    evaluate_event,
    filter_markets_for_target_date,
    group_markets_by_event,
)
from kalshi_weather.clients import KalshiPrivateClient, KalshiPublicClient
from kalshi_weather.domain.enums import DecisionType, RunMode
from kalshi_weather.domain.models import ExecutionPlan
from kalshi_weather.live import ThinLiveAdapter
from kalshi_weather.utils.serde import to_jsonable


DEFAULT_SERIES = ("KXNHLGAME", "KXMLBGAME", "KXNBAGAME")
USER_TIMEZONE = ZoneInfo("America/New_York")


def _target_date_from_args(raw: str | None) -> date:
    if raw:
        return date.fromisoformat(raw)
    return datetime.now(USER_TIMEZONE).date()


def _estimate_fee_dollars(price: Decimal, fee_multiplier: Decimal = Decimal("1")) -> Decimal:
    return Decimal("0.07") * fee_multiplier * price * (Decimal("1") - price)


def _compact_candidate(candidate: dict[str, object] | None) -> dict[str, object] | None:
    if not isinstance(candidate, dict):
        return None
    return {
        "market_ticker": candidate.get("market_ticker"),
        "side": candidate.get("side"),
        "ready": candidate.get("ready"),
        "ask_price": candidate.get("ask_price"),
        "bid_price": candidate.get("bid_price"),
        "spread_dollars": candidate.get("spread_dollars"),
        "minutes_to_expiration": candidate.get("minutes_to_expiration"),
        "expected_expiration_time": candidate.get("expected_expiration_time"),
        "selection_score": candidate.get("selection_score"),
        "expected_payout_per_share": candidate.get("expected_payout_per_share"),
        "max_loss_per_share": candidate.get("max_loss_per_share"),
        "risk_reward_ratio": candidate.get("risk_reward_ratio"),
        "skip_reason": candidate.get("skip_reason"),
        "recent_closes": list(candidate.get("recent_closes") or ()),
        "notes": list(candidate.get("notes") or ()),
        "title": candidate.get("title"),
    }


def _build_event_report(
    report: dict[str, object],
    markets: list[dict[str, object]],
) -> dict[str, object]:
    selected_candidate = _compact_candidate(report.get("selected_candidate"))
    best_ready_candidate = _compact_candidate(report.get("best_ready_candidate"))
    best_blocked_candidate = _compact_candidate(report.get("best_blocked_candidate"))
    title = str(
        (selected_candidate or {}).get("title")
        or (best_ready_candidate or {}).get("title")
        or (best_blocked_candidate or {}).get("title")
        or (markets[0].get("title") if markets else "")
    )
    return {
        "event_ticker": report["event_ticker"],
        "decision": report["decision"],
        "title": title,
        "series_ticker": str(markets[0].get("ticker") or "").split("-", 1)[0] if markets else "",
        "market_tickers": [str(payload.get("ticker") or "") for payload in markets],
        "candidate_count": len(report["all_candidates"]),
        "skip_reason": report["skip_reason"],
        "decision_notes": list(report.get("decision_notes") or ()),
        "reason_counts": dict(report.get("reason_counts") or {}),
        "selected_candidate": selected_candidate,
        "best_ready_candidate": best_ready_candidate,
        "best_blocked_candidate": best_blocked_candidate,
        "candidates": [_compact_candidate(dict(candidate)) for candidate in report["all_candidates"]],
    }


def _report_paths(generated_at: str) -> dict[str, str]:
    reports_dir = Path("data/reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    timestamp = generated_at.replace(":", "").replace("-", "").replace("+", "_").replace("T", "_").replace("Z", "Z")
    latest_path = reports_dir / "sports_closer_summary.latest.json"
    timestamped_path = reports_dir / f"sports_closer_summary.{timestamp}.json"
    journal_path = reports_dir / "sports_closer_game_journal.jsonl"
    return {
        "latest": str(latest_path),
        "timestamped": str(timestamped_path),
        "game_journal": str(journal_path),
    }


def _write_report(payload: dict[str, object]) -> dict[str, str]:
    generated_at = str(payload.get("generated_at") or "")
    report_paths = _report_paths(generated_at)
    payload["report_paths"] = report_paths
    content = json.dumps(to_jsonable(payload), indent=2, sort_keys=True)
    latest_path = Path(report_paths["latest"])
    timestamped_path = Path(report_paths["timestamped"])
    latest_path.write_text(content + "\n", encoding="utf-8")
    timestamped_path.write_text(content + "\n", encoding="utf-8")
    return report_paths


def _append_game_journal(payload: dict[str, object]) -> str:
    report_paths = payload.get("report_paths") or {}
    journal_path = Path(str(report_paths.get("game_journal") or "data/reports/sports_closer_game_journal.jsonl"))
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    generated_at = str(payload.get("generated_at") or "")
    target_date = str(payload.get("target_date") or "")
    strategy = str(payload.get("strategy") or "")
    lines: list[str] = []
    for event in list(payload.get("events") or ()):
        if not isinstance(event, dict):
            continue
        record = {
            "generated_at": generated_at,
            "target_date": target_date,
            "strategy": strategy,
            "event_ticker": event.get("event_ticker"),
            "title": event.get("title"),
            "series_ticker": event.get("series_ticker"),
            "decision": event.get("decision"),
            "skip_reason": event.get("skip_reason"),
            "decision_notes": event.get("decision_notes"),
            "selected_candidate": event.get("selected_candidate"),
            "best_ready_candidate": event.get("best_ready_candidate"),
            "best_blocked_candidate": event.get("best_blocked_candidate"),
            "reason_counts": event.get("reason_counts"),
            "candidates": event.get("candidates"),
        }
        lines.append(json.dumps(to_jsonable(record), sort_keys=True))
    if lines:
        with journal_path.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    return str(journal_path)


def _batch_recent_candles(
    client: KalshiPublicClient,
    markets: list[dict[str, object]],
    *,
    as_of_time: datetime,
    lookback_minutes: int,
    batch_size: int = 25,
) -> dict[str, list[dict[str, object]]]:
    if not markets:
        return {}
    start_ts = int((as_of_time - timedelta(minutes=lookback_minutes)).timestamp())
    end_ts = int(as_of_time.timestamp())
    out: dict[str, list[dict[str, object]]] = {}
    for start in range(0, len(markets), batch_size):
        batch = markets[start : start + batch_size]
        payload = client.batch_get_market_candlesticks(
            [str(market["ticker"]) for market in batch],
            start_ts=start_ts,
            end_ts=end_ts,
            period_interval=1,
        )
        for entry in payload.get("markets", []):
            market_ticker = str(entry.get("market_ticker") or "")
            if market_ticker:
                out[market_ticker] = list(entry.get("candlesticks") or ())
    return out


def _build_execution_plan(candidate: dict[str, object], *, as_of_time: datetime) -> ExecutionPlan:
    price = Decimal(str(candidate["ask_price"]))
    fee = _estimate_fee_dollars(price)
    return ExecutionPlan(
        plan_id=uuid4().hex,
        action_type=DecisionType.TAKER_ALLOWED,
        market_ticker=str(candidate["market_ticker"]),
        side=str(candidate["side"]),
        quantity_fp=Decimal("1"),
        order_type="limit",
        limit_price_dollars=price,
        time_in_force="immediate_or_cancel",
        max_cost_dollars=price + fee,
        cancel_on_pause=True,
        maker_flag=False,
        rationale_codes=("sports_closer", "late_confirmation", "single_share"),
        expires_at=as_of_time + timedelta(minutes=1),
        run_mode=RunMode.LIVE_TRADE,
        decision_utc=as_of_time,
        max_age_seconds=60,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the simple Kalshi sports closer bot.")
    parser.add_argument("--execute", action="store_true", help="Place the top ready bets live.")
    parser.add_argument("--target-date", help="Target market date in YYYY-MM-DD. Defaults to local today.")
    parser.add_argument(
        "--series",
        default=",".join(DEFAULT_SERIES),
        help="Comma-separated Kalshi sports series to scan.",
    )
    parser.add_argument("--max-bets", type=int, default=3, help="Maximum number of live bets to place when executing.")
    args = parser.parse_args()

    as_of_time = default_now_utc()
    target_date = _target_date_from_args(args.target_date)
    config = SportsCloserConfig()
    public_client = KalshiPublicClient()
    series_list = tuple(item.strip() for item in args.series.split(",") if item.strip())

    open_markets: list[dict[str, object]] = []
    series_counts: dict[str, int] = {}
    refresh_failures: dict[str, str] = {}
    for series_ticker in series_list:
        try:
            markets = [dict(payload) for payload in public_client.iter_open_markets(series_ticker, max_records=200)]
        except Exception as exc:
            refresh_failures[series_ticker] = str(exc)
            continue
        day_markets = [dict(payload) for payload in filter_markets_for_target_date(markets, target_date=target_date)]
        series_counts[series_ticker] = len(day_markets)
        open_markets.extend(day_markets)

    candles_by_market = _batch_recent_candles(
        public_client,
        open_markets,
        as_of_time=as_of_time,
        lookback_minutes=max(10, config.confirmation_lookback_minutes + 5),
    )
    events = group_markets_by_event(open_markets)
    event_reports: list[dict[str, object]] = []
    for event_ticker, markets in sorted(events.items()):
        report = evaluate_event(
            event_ticker,
            markets,
            candles_by_market=candles_by_market,
            now=as_of_time,
            config=config,
        )
        event_reports.append(_build_event_report(report, markets))

    ready = [
        entry
        for entry in event_reports
        if entry.get("decision") == "BET"
        and isinstance(entry.get("selected_candidate"), dict)
    ]
    ready_sorted = sorted(
        ready,
        key=lambda entry: (
            Decimal(str(entry["selected_candidate"].get("selection_score") or "0")),
            Decimal(str(entry["selected_candidate"].get("ask_price") or "0")),
        ),
        reverse=True,
    )

    execution_results: list[dict[str, object]] = []
    if args.execute and ready_sorted:
        adapter = ThinLiveAdapter(private_client=KalshiPrivateClient.from_env())
        for entry in ready_sorted[: max(0, args.max_bets)]:
            candidate = dict(entry["selected_candidate"])
            plan = _build_execution_plan(candidate, as_of_time=as_of_time)
            try:
                result = adapter.submit_plan(
                    plan,
                    {"all_passed": True},
                    active_kill_switch=False,
                    dry_run=False,
                )
            except Exception as exc:
                result = {
                    "status": "ERROR",
                    "error": str(exc),
                }
            execution_results.append(
                {
                    "event_ticker": entry["event_ticker"],
                    "market_ticker": candidate["market_ticker"],
                    "side": candidate["side"],
                    "price": candidate["ask_price"],
                    "response": result,
                }
            )

    payload = {
        "generated_at": as_of_time.isoformat(),
        "strategy": "sports_closer",
        "target_date": target_date.isoformat(),
        "generated_at_et": as_of_time.astimezone(USER_TIMEZONE).isoformat(),
        "config": config,
        "series": series_list,
        "series_market_counts": series_counts,
        "refresh_failures": refresh_failures,
        "event_count": len(events),
        "ready_bet_count": len(ready_sorted),
        "ready_bets": ready_sorted,
        "events": event_reports,
        "execution_results": execution_results,
    }
    report_paths = _write_report(payload)
    payload["report_paths"] = report_paths
    payload["journal_path"] = _append_game_journal(payload)
    _write_report(payload)
    print(json.dumps(to_jsonable(payload), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
