"""Daily summary report — volume-grinder strategy (2026-05-16 onwards).

Reads decision payloads from SQLite, runs the volume-grinder selector
(every +EV favorite with p_model ≥ 0.70 across all cities × thresholds,
capped at $10 daily exposure), then renders both a JSON payload and a
human-readable text report to ``data/reports/daily_city_summary.latest.{json,txt}``.

The previous "best-bet-per-city" + longshot-friendly format has been
retired. See ``notes/longshot_lane.md`` for the rationale.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import re
from typing import Any

from kalshi_weather.analytics import build_opportunity_board, build_volume_grinder_selection
from kalshi_weather.analytics.recommendation_log import resolve_pending_recommendations
from kalshi_weather.clients.kalshi_public import KalshiPublicClient
from kalshi_weather.storage import FileReferenceRegistry, SQLiteStateStore
from kalshi_weather.utils.serde import to_jsonable


# ─────────────────────────── Helpers ───────────────────────────

def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _pct(value: object, decimals: int = 1) -> str:
    d = _decimal(value)
    if d is None:
        return "n/a"
    return f"{float(d) * 100:.{decimals}f}%"


def _cents(value: object) -> str:
    d = _decimal(value)
    if d is None:
        return "n/a"
    return f"{float(d) * 100:.1f}¢"


def _dollars(value: object) -> str:
    d = _decimal(value)
    if d is None:
        return "n/a"
    return f"${float(d):.2f}"


def _fallback_condition_text(ticker: str) -> str:
    threshold_match = re.search(r"-T(-?\d+)$", ticker)
    if threshold_match:
        return f"reach or exceed {threshold_match.group(1)}°F"
    return "market condition met"


def _confidence_grade(p_model: Decimal | None) -> str:
    if p_model is None:
        return "N/A"
    p = float(p_model)
    if p >= 0.90:
        return "VERY HIGH ★★★★"
    if p >= 0.80:
        return "HIGH ★★★"
    if p >= 0.75:
        return "GOOD ★★"
    return "OK ★"


def _load_market_titles(tickers: set[str]) -> dict[str, dict[str, str]]:
    details: dict[str, dict[str, str]] = {}
    if not tickers:
        return details
    client = KalshiPublicClient()
    for ticker in sorted(tickers):
        try:
            payload = client.get_market(ticker)
            mp = payload.get("market") if isinstance(payload.get("market"), dict) else payload
            if not isinstance(mp, dict):
                continue
            details[ticker] = {
                "title": str(mp.get("title") or ""),
                "yes_sub_title": str(mp.get("yes_sub_title") or ""),
            }
        except Exception:
            continue
    return details


# ─────────────────────────── Rendering ───────────────────────────

_LINE = "─" * 78
_THICK = "═" * 78


def _render_bet_line(rank: int, candidate: dict[str, Any], market_details: dict[str, dict[str, str]]) -> list[str]:
    city = str(candidate.get("city_id") or "").upper()
    ticker = str(candidate.get("market_ticker") or "")
    side = str(candidate.get("side") or "").upper()
    price = _decimal(candidate.get("market_price"))
    p_model = _decimal(candidate.get("p_model"))
    exec_ev = _decimal(candidate.get("exec_ev"))
    spread_f = _decimal(candidate.get("provider_spread_f"))
    trad = _decimal(candidate.get("tradability"))
    conf = _decimal(candidate.get("overall_confidence"))

    grade = _confidence_grade(p_model)
    condition = market_details.get(ticker, {}).get("yes_sub_title") or _fallback_condition_text(ticker)

    return [
        f"  [{rank:>2}] {city:5} {side:>3} {ticker:30}  {_dollars(price):>6}  EV {_cents(exec_ev):>6}  p_model={_pct(p_model, 1):>6}  {grade}",
        f"       Condition : {condition}",
        f"       Diagnostics: model_spread={float(spread_f) if spread_f else 0:.1f}°F  liquidity={float(trad)*100 if trad else 0:.0f}%  confidence={_pct(conf, 0)}",
    ]


def _render_rejection_summary(rejected: list[dict[str, Any]]) -> list[str]:
    if not rejected:
        return ["  (none)"]
    from collections import Counter
    reasons = Counter(str(r.get("rejection_reason") or "unknown") for r in rejected)
    lines = []
    for reason, count in reasons.most_common():
        plain = {
            "below_favorite_floor": "p_model < 0.70 (longshot — disabled)",
            "near_certain_capped": "p_model > 0.97 (fees eat tiny edge)",
            "ev_below_floor": "EV < 0.5¢ (not enough edge)",
            "model_consensus_too_weak": "model spread > 8°F (too much disagreement)",
            "tradability_below_floor": "orderbook too thin/wide",
            "confidence_below_floor": "overall confidence too low",
        }.get(reason, reason)
        lines.append(f"  {count:>3}× {plain}")
    return lines


def _render_open_positions(open_positions: list[dict[str, Any]]) -> list[str]:
    if not open_positions:
        return ["  (none)"]
    lines = []
    for pos in open_positions:
        ticker = str(pos.get("market_ticker") or "")
        side = str(pos.get("side") or "").upper()
        qty = pos.get("open_quantity_fp") or "1"
        cost = _decimal(pos.get("avg_cost_dollars"))
        city = str(pos.get("city_id") or "").upper()
        lines.append(f"  {city:5} {side:>3} {ticker:32}  ×{qty}  @ {_dollars(cost)}")
    return lines


def _render_volume_report(payload: dict[str, Any]) -> str:
    generated_at_raw = str(payload.get("generated_at") or "")
    try:
        generated = datetime.fromisoformat(generated_at_raw).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        generated = generated_at_raw

    selection = payload.get("volume_grinder") or {}
    selected: list[dict[str, Any]] = list(selection.get("selected") or [])
    deferred: list[dict[str, Any]] = list(selection.get("deferred") or [])
    rejected: list[dict[str, Any]] = list(selection.get("rejected") or [])
    evaluated = int(selection.get("evaluated_count") or 0)
    cap_used = _decimal(selection.get("daily_capital_used_usd")) or Decimal("0")
    cap_max = _decimal(selection.get("daily_capital_cap_usd")) or Decimal("10")
    open_positions = list(payload.get("open_positions") or [])
    fills_today: list[dict[str, Any]] = list(payload.get("fills_today") or [])

    # Combine "bets just placed today" (fills) with "still queued" (selected
    # from the volume selector) for the headline "BETS TO PLACE" list.
    tickers = {str(c.get("market_ticker") or "") for c in selected if c.get("market_ticker")}
    tickers.update(str(f.get("market_ticker") or "") for f in fills_today if f.get("market_ticker"))
    market_details = _load_market_titles(tickers)

    lines: list[str] = []
    lines.append(_THICK)
    lines.append("  KALSHI WEATHER DAILY SUMMARY — VOLUME GRINDER")
    lines.append(f"  Generated  : {generated}")
    lines.append(f"  Strategy   : Bet every +EV favorite (0.70 ≤ p_model ≤ 0.97)")
    lines.append(f"  Capital    : ${float(cap_used):.2f} / ${float(cap_max):.2f} budgeted today")
    review_recs = payload.get("review_recommendations") or []
    lines.append(f"  Markets    : {evaluated} evaluated   {len(selected)} to bet   {len(deferred)} deferred   {len(rejected)} rejected")
    lines.append(f"  Bets today : {len(fills_today)} auto-filled   {len(review_recs)} for manual review   {len(open_positions)} total open")
    lines.append(_THICK)

    # ── BETS PLACED TODAY (just executed) ───────────────────────────────────
    if fills_today:
        lines.append("")
        lines.append(f"  ✅ BETS PLACED TODAY  ({len(fills_today)} contracts shadow-filled this cycle)")
        lines.append(_LINE)
        lines.append(f"  {'TIME':<8} {'CITY':<5} {'SIDE':<4} {'TICKER':<32}  {'PRICE':>6}  {'QTY':>5}  {'EV/CT':>7}  {'p_MODEL':>8}  CONFIDENCE")
        lines.append(f"  {'-'*8} {'-'*5} {'-'*4} {'-'*32}  {'-'*6}  {'-'*5}  {'-'*7}  {'-'*8}  {'-'*16}")
        total_cost = 0.0
        total_ev = 0.0
        for fill in fills_today:
            city = str(fill.get("city_id") or "").upper()
            ticker = str(fill.get("market_ticker") or "")
            side = str(fill.get("side") or "").upper()
            price = _decimal(fill.get("price")) or Decimal("0")
            qty = _decimal(fill.get("quantity")) or Decimal("1")
            ev = _decimal(fill.get("predicted_ev")) or Decimal("0")
            p_m = _decimal(fill.get("p_model"))
            ft = str(fill.get("fill_time") or "")
            try:
                ftime = datetime.fromisoformat(ft).strftime("%H:%M:%S")
            except Exception:
                ftime = ft[:8]
            grade = _confidence_grade(p_m)
            condition = market_details.get(ticker, {}).get("yes_sub_title") or _fallback_condition_text(ticker)
            qty_str = f"{float(qty):.2f}" if qty else "1.00"
            lines.append(
                f"  {ftime:<8} {city:<5} {side:<4} {ticker:<32}  {_dollars(price):>6}  "
                f"{qty_str:>5}  {_cents(ev):>7}  {_pct(p_m, 1):>8}  {grade}"
            )
            lines.append(f"           Condition: {condition}")
            total_cost += float(price) * float(qty)
            total_ev += float(ev) * float(qty)
        lines.append(_LINE)
        lines.append(f"  TOTAL CAPITAL DEPLOYED: ${total_cost:.2f}    PREDICTED EV: ${total_ev:.3f}")

    # ── ADDITIONAL BETS QUEUED (selector picks that haven't been filled yet) ──
    lines.append("")
    if selected:
        lines.append(f"  ▶ ADDITIONAL BETS TO PLACE  ({len(selected)} contracts)")
        lines.append(_LINE)
        lines.append(f"  {'RANK':<5} {'CITY':<6} {'SIDE':<4} {'TICKER':<30}  {'PRICE':>6}  {'EV':>7}  {'p_MODEL':>8}  {'CONFIDENCE'}")
        lines.append(f"  {'-'*5} {'-'*6} {'-'*4} {'-'*30}  {'-'*6}  {'-'*7}  {'-'*8}  {'-'*16}")
        for rank, candidate in enumerate(selected, start=1):
            lines.extend(_render_bet_line(rank, candidate, market_details))
            lines.append("")
        lines.append(_LINE)
        total_cost = sum(float(_decimal(c.get("market_price")) or 0) for c in selected)
        total_ev = sum(float(_decimal(c.get("exec_ev")) or 0) for c in selected)
        lines.append(f"  TOTAL COST  : ${total_cost:.2f}")
        lines.append(f"  TOTAL EV    : ${total_ev:.3f}   ({total_ev/total_cost*100:.1f}% return on capital if model is well-calibrated)" if total_cost > 0 else f"  TOTAL EV    : ${total_ev:.3f}")
    elif not fills_today:
        lines.append("  ▶ NO BETS TODAY")
        lines.append(_LINE)
        lines.append("  No markets passed the favorite filter (p_model ≥ 0.70 + EV ≥ 0.5¢).")
        lines.append("  This is normal on days when the market is efficiently priced or models disagree heavily.")
        lines.append("  Re-run in 30-60 min as forecasts update.")

    # ── DEFERRED (qualifying but over cap) ──────────────────────────────────
    if deferred:
        lines.append("")
        lines.append(_THICK)
        lines.append(f"  ▷ DEFERRED — qualifying but capped at ${float(cap_max):.2f}/day  ({len(deferred)} bets)")
        lines.append(_THICK)
        for rank, candidate in enumerate(deferred[:10], start=1):
            city = str(candidate.get("city_id") or "").upper()
            ticker = str(candidate.get("market_ticker") or "")
            side = str(candidate.get("side") or "").upper()
            price = _dollars(_decimal(candidate.get("market_price")))
            ev = _cents(_decimal(candidate.get("exec_ev")))
            p_m = _pct(_decimal(candidate.get("p_model")), 1)
            lines.append(f"  [{rank:>2}] {city:5} {side:>3} {ticker:30}  {price:>6}  EV {ev:>6}  p_model={p_m}")
        if len(deferred) > 10:
            lines.append(f"  ... ({len(deferred) - 10} more)")

    # ── REVIEW MANUALLY (high market disagreement) ──────────────────────────
    review_recs = list(payload.get("review_recommendations") or [])
    if review_recs:
        lines.append("")
        lines.append(_THICK)
        lines.append(f"  ⚠ REVIEW MANUALLY  ({len(review_recs)} bets the bot will NOT auto-place)")
        lines.append(_THICK)
        lines.append("  These passed the favorite + consensus gates BUT model and Kalshi")
        lines.append("  disagree by >30 percentage points. The bot doesn't auto-fill these")
        lines.append("  because that gap usually means our (uncalibrated) model is wrong.")
        lines.append("  YOU decide whether to place them — data is saved either way.")
        lines.append("")
        lines.append(f"  {'CITY':<5} {'SIDE':<4} {'TICKER':<32}  {'MARKET':>6}  {'MODEL':>6}  {'GAP':>5}  EV/CT")
        lines.append(f"  {'-'*5} {'-'*4} {'-'*32}  {'-'*6}  {'-'*6}  {'-'*5}  {'-'*6}")
        for rec in review_recs:
            city = str(rec.get("city_id") or "").upper()
            ticker = str(rec.get("market_ticker") or "")
            side = str(rec.get("recommended_side") or "").upper()
            p_model = _decimal(rec.get("model_p_yes"))
            p_market = _decimal(rec.get("market_price"))
            ev = _decimal(rec.get("executable_ev"))
            gap = float(p_model - p_market) if (p_model is not None and p_market is not None) else 0
            cond = market_details.get(ticker, {}).get("yes_sub_title") or _fallback_condition_text(ticker)
            lines.append(
                f"  {city:<5} {side:<4} {ticker:<32}  "
                f"{_pct(p_market, 0):>6}  {_pct(p_model, 0):>6}  {gap*100:+5.0f}pp  "
                f"{_cents(ev):>6}"
            )
            lines.append(f"        Condition: {cond}")

    # ── OPEN POSITIONS ──────────────────────────────────────────────────────
    lines.append("")
    lines.append(_THICK)
    lines.append(f"  CURRENTLY OPEN POSITIONS  ({len(open_positions)})")
    lines.append(_THICK)
    lines.extend(_render_open_positions(open_positions))

    # ── REJECTIONS SUMMARY ──────────────────────────────────────────────────
    lines.append("")
    lines.append(_THICK)
    lines.append(f"  REJECTED MARKETS  ({len(rejected)} markets — why they didn't bet)")
    lines.append(_THICK)
    lines.extend(_render_rejection_summary(rejected))

    # ── COUNTERFACTUAL P&L (resolved recommendations) ───────────────────────
    cf = payload.get("counterfactual_summary") or {}
    if cf.get("total_resolved", 0) > 0:
        lines.append("")
        lines.append(_THICK)
        lines.append(f"  COUNTERFACTUAL P&L  (from {cf['total_resolved']} resolved past recommendations)")
        lines.append(_THICK)
        overall = cf.get("overall") or {}
        win_rate_pct = float(overall.get("win_rate") or 0) * 100
        pnl = float(overall.get("pnl_usd") or 0)
        sign = "+" if pnl >= 0 else ""
        lines.append(
            f"  OVERALL:  {overall.get('wins')}W / {overall.get('losses')}L  "
            f"({win_rate_pct:.1f}% win rate)   "
            f"Cumulative P&L if we'd bet all: {sign}${pnl:.2f}"
        )
        by_kind = cf.get("by_kind") or {}
        for kind in ("BET", "WATCH_RECOMMEND", "SKIP"):
            b = by_kind.get(kind)
            if not b:
                continue
            wr = float(b.get("win_rate") or 0) * 100
            bp = float(b.get("pnl_usd") or 0)
            bsign = "+" if bp >= 0 else ""
            label_hint = {
                "BET": "(all gates passed — bot would auto-fill live)",
                "WATCH_RECOMMEND": "(positive raw edge but a gate failed — would-have recommended manually)",
                "SKIP": "(no actionable edge per current filters)",
            }[kind]
            lines.append(
                f"    {kind:<17}  {b['count']:>3} bets  {wr:5.1f}% win  "
                f"  {bsign}${bp:.2f}  {label_hint}"
            )
        lines.append("")
        lines.append("  Reading:")
        if (by_kind.get("BET") or {}).get("pnl_usd", 0) > 0 and \
           (by_kind.get("SKIP") or {}).get("pnl_usd", 0) <= 0:
            lines.append("    ✓ Gates are working: BETs are profitable, SKIPs would have lost.")
        elif (by_kind.get("SKIP") or {}).get("pnl_usd", 0) > 0:
            lines.append("    ⚠ SKIP counterfactual is positive — we may be over-filtering.")
        else:
            lines.append("    Insufficient resolved data yet — keep accumulating.")

    # ── LIVE GATE STATUS ────────────────────────────────────────────────────
    live_gate = payload.get("live_gate_report")
    if isinstance(live_gate, dict):
        lines.append("")
        lines.append(_THICK)
        all_passed = bool(live_gate.get("all_passed"))
        passed = int(live_gate.get("gates_passed") or 0)
        total = int(live_gate.get("gates_total") or 0)
        icon = "✅ LIVE GATE PASSED" if all_passed else f"🔒 LIVE GATE LOCKED  ({passed}/{total})"
        lines.append(f"  {icon}")
        lines.append(_THICK)
        for k, v in (live_gate.get("gates") or {}).items():
            mark = "✓" if v else "✗"
            lines.append(f"    {mark} {k}")

    # ── DATA-SOURCE TRANSPARENCY ────────────────────────────────────────────
    lines.append("")
    lines.append(_THICK)
    lines.append("  FORECAST SOURCES (11-model ensemble)")
    lines.append(_THICK)
    lines.append("    NWS Hourly, NWS Grid")
    lines.append("    NOAA GFS, NOAA HRRR, NOAA NBM")
    lines.append("    ECMWF IFS, ECMWF AIFS, Google GraphCast")
    lines.append("    DWD ICON, JMA, ECCC GEM")
    lines.append("")
    lines.append("  Strategy details : volume-grinder favorites")
    lines.append("  Longshot lane    : DISABLED (see notes/longshot_lane.md)")
    lines.append(_THICK)

    # ── NEXT STEPS ──────────────────────────────────────────────────────────
    lines.append("")
    lines.append(_THICK)
    lines.append("  NEXT STEPS")
    lines.append(_THICK)
    auto_filled_today = list(payload.get("fills_today") or [])
    review_recs_for_steps = list(payload.get("review_recommendations") or [])
    if not auto_filled_today and not selected and not review_recs_for_steps:
        lines.append("  1. No bets ready. Re-run 'run_city_cycle' in 30-60 min and try again.")
        lines.append("  2. If consistently zero bets, check the rejection summary above —")
        lines.append("     persistent 'model_consensus_too_weak' means weather is genuinely uncertain.")
    else:
        step = 1
        if auto_filled_today:
            lines.append(f"  {step}. AUTO-FILLED ({len(auto_filled_today)}) — bot already shadow-filled "
                         f"these; would be live orders tomorrow:")
            for fill in auto_filled_today:
                city = str(fill.get("city_id") or "").upper()
                ticker = str(fill.get("market_ticker") or "")
                side = str(fill.get("side") or "").upper()
                price = _dollars(_decimal(fill.get("price")))
                lines.append(f"       {city}  BUY {side} 1 @ {price}  →  {ticker}")
            step += 1
        if review_recs_for_steps:
            lines.append(f"  {step}. REVIEW MANUALLY ({len(review_recs_for_steps)}) — YOUR call whether "
                         f"to place (bot will NOT auto-place these):")
            for rec in review_recs_for_steps:
                city = str(rec.get("city_id") or "").upper()
                ticker = str(rec.get("market_ticker") or "")
                side = str(rec.get("recommended_side") or "").upper()
                price = _dollars(_decimal(rec.get("market_price")))
                gap_pct = (float(rec.get("model_p_yes") or 0) - float(rec.get("market_price") or 0)) * 100
                lines.append(f"       {city}  BUY {side} 1 @ {price}  →  {ticker}  (model-market gap {gap_pct:+.0f}pp)")
            step += 1
        if selected:
            lines.append(f"  {step}. ADDITIONAL CANDIDATES ({len(selected)}) — selector picks; "
                         f"bot will auto-fill these on next cycle:")
            for candidate in selected:
                city = str(candidate.get("city_id") or "").upper()
                ticker = str(candidate.get("market_ticker") or "")
                side = str(candidate.get("side") or "").upper()
                price = _dollars(_decimal(candidate.get("market_price")))
                lines.append(f"       {city}  BUY {side} 1 @ {price}  →  {ticker}")
            step += 1
        lines.append(f"  {step}. Refresh in 30-60 min:  python -m kalshi_weather.tools.run_city_cycle")
    lines.append("")
    lines.append(f"  Report saved: data/reports/daily_city_summary.latest.txt")
    lines.append(_THICK)
    lines.append("")
    return "\n".join(lines)


# ─────────────────────────── Output ───────────────────────────

def _write_summary_files(payload: dict[str, Any]) -> dict[str, str]:
    reports_dir = Path("data/reports")
    reports_dir.mkdir(parents=True, exist_ok=True)
    generated_at = str(payload.get("generated_at") or "")
    timestamp = (
        generated_at.replace(":", "").replace("-", "").replace("+", "_")
        .replace("T", "_").replace("Z", "Z")
    )
    latest_json = reports_dir / "daily_city_summary.latest.json"
    stamped_json = reports_dir / f"daily_city_summary.{timestamp}.json"
    latest_txt = reports_dir / "daily_city_summary.latest.txt"
    stamped_txt = reports_dir / f"daily_city_summary.{timestamp}.txt"

    files = {
        "latest_json": str(latest_json),
        "timestamped_json": str(stamped_json),
        "latest_text": str(latest_txt),
        "timestamped_text": str(stamped_txt),
    }
    payload_with_files = dict(payload)
    payload_with_files["report_files"] = files
    json_text = json.dumps(to_jsonable(payload_with_files), indent=2, sort_keys=True)
    human = _render_volume_report(payload_with_files)
    latest_json.write_text(json_text + "\n", encoding="utf-8")
    stamped_json.write_text(json_text + "\n", encoding="utf-8")
    latest_txt.write_text(human, encoding="utf-8")
    stamped_txt.write_text(human, encoding="utf-8")
    return files


def _build_counterfactual_summary(store: SQLiteStateStore) -> dict[str, Any]:
    """Aggregate resolved recommendations into per-kind counterfactual stats.

    Tells us, for each recommendation type (BET / WATCH_RECOMMEND / SKIP):
      - how many resolved
      - how many won counterfactually
      - cumulative P&L if we had bet every one
    """
    resolved = [
        r for r in store.list_recommendations()
        if r.get("counterfactual_resolved")
    ]
    out: dict[str, Any] = {
        "total_resolved": len(resolved),
        "by_kind": {},
        "overall": {"wins": 0, "losses": 0, "pnl_usd": 0.0},
    }
    by_kind: dict[str, dict[str, Any]] = {}
    for r in resolved:
        kind = str(r.get("recommendation_kind") or "unknown")
        bucket = by_kind.setdefault(kind, {
            "count": 0, "wins": 0, "losses": 0, "pnl_usd": 0.0,
        })
        bucket["count"] += 1
        won = bool(r.get("counterfactual_won"))
        pnl = float(r.get("counterfactual_pnl_usd") or 0.0)
        if won:
            bucket["wins"] += 1
            out["overall"]["wins"] += 1
        else:
            bucket["losses"] += 1
            out["overall"]["losses"] += 1
        bucket["pnl_usd"] += pnl
        out["overall"]["pnl_usd"] += pnl
    # Compute win rates
    for bucket in by_kind.values():
        total = bucket["count"]
        bucket["win_rate"] = (bucket["wins"] / total) if total else 0.0
    out["by_kind"] = by_kind
    total = len(resolved)
    out["overall"]["win_rate"] = (out["overall"]["wins"] / total) if total else 0.0
    return out


def main() -> None:
    store = SQLiteStateStore("data/state/runtime.sqlite3")
    registry = FileReferenceRegistry()
    seed = registry.load_or_default()
    decisions = store.list_decision_payloads()
    contexts_by_city = registry.context_by_city_id(seed)
    timezone_by_city = {
        city_id: context.station.timezone
        for city_id, context in contexts_by_city.items()
    }

    open_positions = [
        p for p in store.list_shadow_position_payloads()
        if str(p.get("lifecycle_status") or "") == "OPEN"
    ]
    qualification_states = {}
    open_city_ids = {str(p.get("city_id") or "") for p in open_positions}

    # Collect shadow fills from the most recent 12 hours — these are "today's bets".
    fills_today: list[dict[str, Any]] = []
    try:
        import sqlite3
        conn = sqlite3.connect("data/state/runtime.sqlite3")
        c = conn.cursor()
        c.execute(
            "SELECT city_id, market_ticker, fill_time, payload_json "
            "FROM shadow_fills "
            "WHERE datetime(fill_time) >= datetime('now', '-12 hours') "
            "ORDER BY fill_time DESC"
        )
        # We also need the matching p_model for each filled market. Pull the
        # latest decision payload per (city, market_ticker).
        latest_decision_by_market: dict[tuple[str, str], dict[str, Any]] = {}
        for d in decisions:
            key = (str(d.get("city_id") or ""), str(d.get("market_ticker") or ""))
            prev = latest_decision_by_market.get(key)
            if prev is None or str(d.get("as_of_time", "")) > str(prev.get("as_of_time", "")):
                latest_decision_by_market[key] = d

        for row in c.fetchall():
            city_id, ticker, fill_time, raw = row
            try:
                fp = json.loads(raw or "{}")
            except Exception:
                fp = {}
            side = str(fp.get("side") or "")
            # Shadow-fill stores the modeled fill price (what we paid for the contract).
            price = (
                fp.get("modeled_fill_price_dollars")
                or fp.get("price")
                or fp.get("avg_cost_dollars")
            )
            # Match to latest decision for p_model.
            # IMPORTANT: a decision evaluates BOTH yes/no sides and picks the better one.
            # The fill's `side` always matches the decision's `selected_side`, so
            # `selected_p_model` is the model probability for the side actually filled.
            dec = latest_decision_by_market.get((city_id, ticker), {})
            edge = dec.get("edge_summary") if isinstance(dec, dict) else None
            p_model = (edge or {}).get("selected_p_model") if isinstance(edge, dict) else None
            # Safety check: if the side stored on the latest decision doesn't match
            # the fill's side, the fill's p_model is the complement.
            decided_side = (edge or {}).get("selected_side") if isinstance(edge, dict) else None
            if p_model is not None and decided_side and decided_side != side:
                try:
                    p_model = str(Decimal("1") - Decimal(str(p_model)))
                except Exception:
                    pass
            fills_today.append({
                "city_id": city_id,
                "market_ticker": ticker,
                "fill_time": fill_time,
                "side": side,
                "price": price,
                "p_model": p_model,
                "predicted_ev": fp.get("predicted_executable_ev_per_contract"),
                "quantity": fp.get("quantity_fp"),
            })
        conn.close()
    except Exception:
        fills_today = []
    for context in registry.iter_city_contexts(seed):
        city_id = context.city_profile.city_id
        q = store.get_qualification_state(city_id)
        qualification_states[city_id] = q.state.value if q else None

    # Core: volume-grinder selection across all cities × thresholds.
    # Skip markets we already hold open positions on (avoids duplicate recommendations).
    open_keys = {
        (str(p.get("city_id") or ""), str(p.get("market_ticker") or ""))
        for p in open_positions
        if p.get("city_id") and p.get("market_ticker")
    }
    volume_selection = build_volume_grinder_selection(
        decision_payloads=decisions,
        timezone_by_city=timezone_by_city,
        already_open_keys=open_keys,
    )

    # Keep the opportunity board for diagnostic purposes (also feeds JSON dump).
    same_day_board = build_opportunity_board(
        decisions,
        qualification_states=qualification_states,
        limit=30,
        market_day_mode="open_day",
        timezone_by_city=timezone_by_city,
        open_city_ids=open_city_ids,
        exclude_open_cities=False,
    )
    next_avail_board = build_opportunity_board(
        decisions,
        qualification_states=qualification_states,
        limit=30,
        market_day_mode="next_available",
        timezone_by_city=timezone_by_city,
        open_city_ids=open_city_ids,
        exclude_open_cities=False,
    )

    # Live gate
    live_gate_report = None
    try:
        from kalshi_weather.analytics.live_gating import (
            build_live_gate_report,
            select_live_gate_profile,
        )
        fills = (
            store.list_shadow_fill_payloads()
            if hasattr(store, "list_shadow_fill_payloads")
            else []
        )
        positions = (
            store.list_shadow_position_payloads()
            if hasattr(store, "list_shadow_position_payloads")
            else []
        )
        first_city = next(iter(qualification_states), None)
        profile = select_live_gate_profile(first_city)
        live_gate_report = build_live_gate_report(
            decision_payloads=decisions,
            fill_payloads=fills,
            position_payloads=positions,
            profile=profile,
        )
    except Exception as exc:
        live_gate_report = {"error": str(exc)}

    # ── Counterfactual P&L analysis ────────────────────────────────────────
    # 1. Resolve any unresolved recommendations whose settlements have arrived.
    try:
        resolver_counts = resolve_pending_recommendations(store)
    except Exception as exc:
        resolver_counts = {"error": str(exc)}

    # 2. Compute counterfactual stats from the resolved recommendations.
    counterfactual_summary = _build_counterfactual_summary(store)

    # 3. Pull today's REVIEW recommendations (high model-vs-market disagreement
    #    — bot won't auto-place but surface to the user).
    today_review_recs: list[dict[str, Any]] = []
    try:
        all_recs = store.list_recommendations()
        today_iso = datetime.now(timezone.utc).date().isoformat()
        seen_keys: set[tuple[str, str]] = set()
        for rec in all_recs:
            if rec.get("recommendation_kind") != "REVIEW":
                continue
            # Only show recommendations from today's cycle(s)
            as_of = str(rec.get("as_of_time") or "")
            if not as_of.startswith(today_iso):
                continue
            key = (str(rec.get("city_id") or ""), str(rec.get("market_ticker") or ""))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            today_review_recs.append(rec)
        # Sort by absolute disagreement descending (most suspicious first)
        today_review_recs.sort(
            key=lambda r: abs(float(r.get("model_p_yes") or 0) - float(r.get("market_price") or 0)),
            reverse=True,
        )
    except Exception as exc:
        today_review_recs = []

    payload: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "strategy": "volume_grinder_v1",
        "volume_grinder": volume_selection,
        "fills_today": fills_today,
        "review_recommendations": today_review_recs,
        "open_positions": open_positions,
        "qualification_states": qualification_states,
        "live_gate_report": live_gate_report,
        "same_day_board": same_day_board,
        "next_available_board": next_avail_board,
        "counterfactual_summary": counterfactual_summary,
        "counterfactual_resolver_counts": resolver_counts,
    }
    payload["report_files"] = _write_summary_files(payload)
    print(json.dumps(to_jsonable(payload), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
