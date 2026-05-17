"""T2.3 — Provider error tracking + EWMA-weighted blending.

When NWS CLI settlements arrive, we have ground truth for what each
forecast provider predicted vs what actually happened. This module:

1. Persists per-(provider, city, settlement) errors to the provider_errors
   table.
2. Computes rolling EWMA weights per (provider, city, season) for the
   forecast blender to consume.

EWMA half-life is 20 days — recent performance weighs ~2x as much as
month-old performance, but we don't whip-saw on a single bad day.

INTEGRATION STATE: currently writes data but does NOT yet modify the
forecast blender. We'll wire it in after observing 1-2 weeks of error
data to confirm the EWMA weights look sensible.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


# Half-life for the exponential decay in days. After this many days, an
# error sample is weighted at 0.5 of a fresh sample.
EWMA_HALF_LIFE_DAYS = 20
# Minimum samples per provider per city before we trust the per-city
# weight from provider_errors. Started at 20 (proper EWMA confidence)
# then lowered 2026-05-17 — even 3-5 samples meaningfully distinguishes
# the best provider (NYC NWS @ 1.89°F MAE) from the worst (GraphCast
# 7.64°F). Blended 50/50 with priors so a few bad samples can't dominate.
MIN_SAMPLES_FOR_EWMA = 3
# Blending weight between per-city EWMA weights and existing priors.
# 0.5 = equal influence. Will raise toward 0.8 as samples accumulate.
PER_CITY_BLEND_WEIGHT = Decimal("0.5")


def _season_key(settlement_date: str) -> str:
    """settlement_date is YYYY-MM-DD; return DJF/MAM/JJA/SON."""
    try:
        m = int(settlement_date[5:7])
    except Exception:
        return "UNK"
    if m in (12, 1, 2):
        return "DJF"
    if m in (3, 4, 5):
        return "MAM"
    if m in (6, 7, 8):
        return "JJA"
    return "SON"


def record_provider_errors_from_settlement(
    store,
    *,
    city_id: str,
    local_date: str,
    actual_high_f: float | None,
) -> int:
    """Compute and persist per-provider errors for one (city, day) settlement.

    For every decision saved on the target day for this city, extract each
    provider's predicted daily maximum from forecast_summary.provider_maxima_f
    and store (forecast, actual, abs_error) keyed by provider.

    Returns the count of error rows written.
    """
    if actual_high_f is None:
        return 0
    rows_written = 0
    season = _season_key(local_date)
    # Pull all decisions for this city from the target day. We use the
    # most-recent decision per market to avoid duplicating evals.
    with store._connect() as conn:
        rows = conn.execute(
            "SELECT market_ticker, payload_json, as_of_time FROM decisions "
            "WHERE city_id = ? AND substr(as_of_time, 1, 10) = ? "
            "ORDER BY as_of_time DESC",
            (city_id, local_date),
        ).fetchall()
    seen_markets: set[str] = set()
    inserts: list[tuple] = []
    for market_ticker, payload_raw, as_of_time in rows:
        if market_ticker in seen_markets:
            continue
        seen_markets.add(market_ticker)
        try:
            payload = json.loads(payload_raw)
        except Exception:
            continue
        fs = payload.get("forecast_summary") or {}
        maxima = fs.get("provider_maxima_f") or {}
        if not isinstance(maxima, dict):
            continue
        try:
            lead_hours_raw = (fs.get("forecast_provider_count") or 0)
        except Exception:
            lead_hours_raw = 0
        for provider_id, predicted_str in maxima.items():
            try:
                predicted = float(Decimal(str(predicted_str)))
            except Exception:
                continue
            abs_error = abs(predicted - actual_high_f)
            inserts.append((
                f"{city_id}:{local_date}:{provider_id}:{market_ticker}",
                provider_id,
                city_id,
                market_ticker,
                local_date,
                season,
                predicted,
                actual_high_f,
                abs_error,
                as_of_time,
            ))
    if not inserts:
        return 0
    with store._connect() as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO provider_errors "
            "(error_id, provider_id, city_id, market_ticker, settlement_date, "
            "season, predicted_high_f, actual_high_f, abs_error_f, "
            "decision_as_of_time) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            inserts,
        )
        rows_written = len(inserts)
    return rows_written


def compute_ewma_provider_weights(
    store,
    *,
    city_id: str,
    season: str | None = None,
    half_life_days: int = EWMA_HALF_LIFE_DAYS,
) -> dict[str, float] | None:
    """Return EWMA-weighted provider quality (1/(1+mean_abs_error)) per provider.

    Returns None if no providers have enough samples (MIN_SAMPLES_FOR_EWMA).
    Returns a normalized dict[provider_id, weight] otherwise — weights sum
    to 1.0 and higher-accuracy providers get higher weight.
    """
    where = "city_id = ?"
    params: list = [city_id]
    if season:
        where += " AND season = ?"
        params.append(season)
    with store._connect() as conn:
        rows = conn.execute(
            f"SELECT provider_id, settlement_date, abs_error_f FROM provider_errors "
            f"WHERE {where} ORDER BY settlement_date DESC",
            params,
        ).fetchall()
    if not rows:
        return None
    # Compute time-weighted MAE per provider
    today = datetime.utcnow().date().isoformat()
    today_d = datetime.fromisoformat(today).date()
    decay_per_day = math.log(2) / max(1, half_life_days)
    by_provider: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for provider_id, settlement_date, abs_error in rows:
        try:
            d = datetime.fromisoformat(settlement_date).date()
            days_old = (today_d - d).days
        except Exception:
            continue
        weight = math.exp(-decay_per_day * max(0, days_old))
        by_provider[provider_id].append((float(abs_error), weight))

    # Require min samples per provider
    enough = {p: errs for p, errs in by_provider.items() if len(errs) >= MIN_SAMPLES_FOR_EWMA}
    if not enough:
        return None

    quality_raw: dict[str, float] = {}
    for provider_id, samples in enough.items():
        weighted_err = sum(err * w for err, w in samples)
        weight_total = sum(w for _, w in samples)
        if weight_total <= 0:
            continue
        mae = weighted_err / weight_total
        # Quality = inverse error, capped so a perfect provider doesn't
        # take 100% of the weight.
        quality_raw[provider_id] = 1.0 / (1.0 + mae)

    total_q = sum(quality_raw.values())
    if total_q <= 0:
        return None
    return {p: q / total_q for p, q in quality_raw.items()}


def blend_with_priors(
    *,
    per_city_weights: dict[str, float] | None,
    prior_weights: dict[str, "Decimal"],
    blend: "Decimal" = PER_CITY_BLEND_WEIGHT,
) -> dict[str, "Decimal"]:
    """Blend per-city weights with global/prior weights.

    Returns {provider: weight} sum-to-1.0. If per-city weights are None
    (insufficient samples), returns the priors unchanged. If priors are
    empty, returns the per-city weights as Decimals.
    """
    from decimal import Decimal as _D
    if not per_city_weights:
        return dict(prior_weights)
    if not prior_weights:
        return {p: _D(str(w)) for p, w in per_city_weights.items()}
    # Normalize priors (they may sum to !=1 due to upstream clamping)
    prior_total = sum(prior_weights.values())
    if prior_total <= 0:
        return {p: _D(str(w)) for p, w in per_city_weights.items()}
    normalized_priors = {p: w / prior_total for p, w in prior_weights.items()}
    out: dict[str, _D] = {}
    all_providers = set(per_city_weights) | set(normalized_priors)
    for provider in all_providers:
        pc = _D(str(per_city_weights.get(provider, 0)))
        pr = normalized_priors.get(provider, _D("0"))
        out[provider] = blend * pc + (_D("1") - blend) * pr
    # Re-normalize to 1.0 exactly
    total = sum(out.values())
    if total <= 0:
        return dict(prior_weights)
    return {p: w / total for p, w in out.items()}
