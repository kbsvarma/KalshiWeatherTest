"""T2.5 — Isotonic recalibration of p_yes.

After settlements land, we have (predicted_p_yes_at_decision,
actual_outcome) pairs. We fit a monotone non-decreasing function that maps
raw model probability to empirical hit rate.

The bot says 80% but actually wins 70% of those bets — isotonic catches
that and shrinks the predictions. The bot says 60% but actually wins 75% —
isotonic boosts.

INTEGRATION STATE:
- Data accumulator: pulls settled (predicted, outcome) pairs from the DB.
- Trainer: sklearn IsotonicRegression, fit once per day (or on demand).
- Cache: data/derived/isotonic_model.json with knot points.
- Applier: maps raw p → calibrated p. Identity until enough data.

Activation gate: minimum 50 settled (predicted, outcome) pairs total before
the calibration is used. Below that, the recalibration is too noisy.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterable


# 2026-05-19: raised 50 → 2000 after the 120-sample fit produced a
# 9-bin step function that snapped every live p_yes to ~0.21-0.40 for
# two days, blocking the favorite-floor gate on all B-markets and
# stopping ~80% of trading. Symptom file: data/derived/isotonic_model.json.bad.
# Until we have ≥2000 (predicted, outcome) pairs the calibration is
# forced to identity. Need a less brittle calibrator (fixed-resolution
# logistic etc.) before re-enabling under any lower threshold.
MIN_SAMPLES_FOR_CALIBRATION = 2000
DEFAULT_CACHE_PATH = Path("data/derived/isotonic_model.json")
# Bot underwent substantial changes on 2026-05-17 (T1.2 climatology prior,
# T1.3 persistence baseline, portfolio_risk_limit fix, spread cap raise,
# shadow_positions PK fix). Calibration on data from before this date
# would mis-fit today's model. Anchor: only use settled bets where the
# decision was made on or after this anchor.
CALIBRATION_ANCHOR_DATE = "2026-05-18"  # tomorrow — guarantees fresh data only


@dataclass(frozen=True, slots=True)
class CalibrationPair:
    predicted_p_yes: float
    actual_yes: int  # 1 if market settled YES, else 0


def collect_calibration_pairs(store) -> list[CalibrationPair]:
    """Walk decisions ∪ settlements to produce (predicted_p, actual_outcome).

    For each decision with path_state.p_yes AND a matching settlement
    for (city_id, ticker's settlement date), emit one pair. Outcome is
    derived from market_ticker pattern vs actual high.
    """
    import re
    from kalshi_weather.analytics.opportunities import parse_market_date
    pairs: list[CalibrationPair] = []
    t_re = re.compile(r"-T(\d+)$")
    b_re = re.compile(r"-B(\d+\.\d+)$")
    with store._connect() as conn:
        decisions = conn.execute(
            "SELECT city_id, market_ticker, payload_json, as_of_time "
            "FROM decisions WHERE substr(as_of_time, 1, 10) >= ? "
            "ORDER BY as_of_time DESC LIMIT 20000",
            (CALIBRATION_ANCHOR_DATE,),
        ).fetchall()
        settlements = conn.execute(
            "SELECT city_id, local_date, daily_high_f FROM market_settlements "
            "WHERE daily_high_f IS NOT NULL"
        ).fetchall()
    settlement_index: dict[tuple[str, str], float] = {
        (city_id, local_date): float(daily_high)
        for city_id, local_date, daily_high in settlements
    }
    # Take only the MOST RECENT decision per (city, ticker) to avoid
    # double-counting intra-day re-evaluations.
    seen: set[tuple[str, str]] = set()
    for city_id, ticker, payload_raw, _as_of_time in decisions:
        key = (city_id, ticker)
        if key in seen:
            continue
        seen.add(key)
        market_date = parse_market_date(ticker)
        if market_date is None:
            continue
        daily_high = settlement_index.get((city_id, market_date.isoformat()))
        if daily_high is None:
            continue
        try:
            payload = json.loads(payload_raw)
            p_yes_str = (payload.get("path_state") or {}).get("p_yes")
            if p_yes_str is None:
                continue
            p_yes_val = float(Decimal(str(p_yes_str)))
        except Exception:
            continue
        actual_yes: int | None = None
        m = t_re.search(ticker)
        if m:
            threshold = float(m.group(1))
            actual_yes = 1 if daily_high > threshold else 0
        else:
            m = b_re.search(ticker)
            if m:
                floor = float(m.group(1))
                # Kalshi -B markets are 2°F-wide bins (e.g., B82.5 → 82.5-84.5)
                cap = floor + 2.0
                actual_yes = 1 if floor <= daily_high <= cap else 0
        if actual_yes is None:
            continue
        pairs.append(CalibrationPair(p_yes_val, actual_yes))
    return pairs


def fit_and_cache(
    store, *, cache_path: Path = DEFAULT_CACHE_PATH
) -> dict | None:
    """Fit sklearn IsotonicRegression on settled pairs and cache to disk.

    Returns the cached dict (or None if not enough data).
    """
    pairs = collect_calibration_pairs(store)
    if len(pairs) < MIN_SAMPLES_FOR_CALIBRATION:
        return None
    try:
        from sklearn.isotonic import IsotonicRegression
    except ImportError:
        return None
    xs = [p.predicted_p_yes for p in pairs]
    ys = [float(p.actual_yes) for p in pairs]
    iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(xs, ys)

    # Serialize knot points
    cache = {
        "sample_count": len(pairs),
        "x_thresholds": [float(x) for x in iso.X_thresholds_],
        "y_thresholds": [float(y) for y in iso.y_thresholds_],
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2))
    return cache


def _piecewise_linear(x: float, xs: list[float], ys: list[float]) -> float:
    if not xs:
        return x
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    # Find segment
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            x0, x1 = xs[i], xs[i + 1]
            y0, y1 = ys[i], ys[i + 1]
            if x1 == x0:
                return y0
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return x


def apply_calibration(
    p_yes: Decimal,
    *,
    cache_path: Path = DEFAULT_CACHE_PATH,
) -> tuple[Decimal, bool]:
    """Return (calibrated_p_yes, was_applied). Identity if cache missing or
    sample count below threshold."""
    if not cache_path.exists():
        return (p_yes, False)
    try:
        cache = json.loads(cache_path.read_text())
    except Exception:
        return (p_yes, False)
    if cache.get("sample_count", 0) < MIN_SAMPLES_FOR_CALIBRATION:
        return (p_yes, False)
    xs = cache.get("x_thresholds") or []
    ys = cache.get("y_thresholds") or []
    if not xs or not ys:
        return (p_yes, False)
    calibrated = _piecewise_linear(float(p_yes), xs, ys)
    calibrated = max(0.0, min(1.0, calibrated))
    return (Decimal(str(round(calibrated, 6))), True)
