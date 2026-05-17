"""Climatology prior — T1.2 of the scientific roadmap.

Reads the per-city monthly normals built by
`tools.build_climate_normals` and computes a Bayesian-shrinkage probability:

    p_final = w · p_model + (1 - w) · p_climatology

where p_climatology is derived from a 30-year Gaussian assumption per
calendar month. Gracefully no-ops (returns None) when the cache is missing
or the requested month has too few samples — never fabricates.
"""
from __future__ import annotations

import json
import math
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

CACHE_DIR = Path("data/reference/climate_normals")
# Bayesian-shrinkage weight on the MODEL. 0.85 = strong trust in model,
# climatology is the regularizer. Roadmap-specified starting point; can be
# tuned downward (e.g. 0.75) once we have a week of realized settlements.
DEFAULT_MODEL_WEIGHT = Decimal("0.85")
# Minimum monthly sample count before we'll use the cached normals.
MIN_SAMPLES_PER_MONTH = 300


@lru_cache(maxsize=64)
def _load_city_normals(city_id: str) -> dict | None:
    path = CACHE_DIR / f"{city_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _gaussian_cdf(x: float, mean: float, std: float) -> float:
    if std <= 0:
        return 1.0 if x >= mean else 0.0
    return 0.5 * (1.0 + math.erf((x - mean) / (std * math.sqrt(2))))


def compute_p_climatology(
    *,
    city_id: str,
    settlement_month: int,
    operator: str,
    threshold_f: Decimal,
    threshold_high_f: Decimal | None = None,
    inclusive_flag: bool = False,
    settlement_variable: str = "daily_high_temperature_f",
) -> Decimal | None:
    """Return P(yes-settles) under 30-year climatology, or None if no data.

    Uses a Gaussian assumption per calendar month — accurate for MIDDLE of
    the distribution; tails are looser. We DON'T use this as a hard input
    to gates, only as a shrinkage anchor (15% weight by default).
    """
    cache = _load_city_normals(city_id)
    if cache is None:
        return None

    if settlement_variable == "daily_low_temperature_f":
        by_month = cache.get("tmin_f_by_month")
    else:
        by_month = cache.get("tmax_f_by_month")
    if not by_month:
        return None
    stats = by_month.get(str(settlement_month))
    if stats is None or stats.get("mean") is None or stats.get("std") is None:
        return None
    if stats.get("sample_count", 0) < MIN_SAMPLES_PER_MONTH:
        return None

    mean = float(stats["mean"])
    std = float(stats["std"])
    threshold = float(threshold_f)

    if operator in {">", ">="}:
        # P(temp > threshold)
        # For ">=", inclusive — treat as P(temp >= threshold)
        # Continuous Gaussian; inclusion of the exact value is measure-zero
        cdf_at = _gaussian_cdf(threshold, mean, std)
        p = 1.0 - cdf_at
    elif operator in {"<", "<="}:
        cdf_at = _gaussian_cdf(threshold, mean, std)
        p = cdf_at
    elif operator == "between" and threshold_high_f is not None:
        cdf_lo = _gaussian_cdf(threshold, mean, std)
        cdf_hi = _gaussian_cdf(float(threshold_high_f), mean, std)
        p = max(0.0, cdf_hi - cdf_lo)
    else:
        return None

    p = max(0.0, min(1.0, p))
    return Decimal(str(round(p, 6)))


def apply_shrinkage(
    *,
    p_model: Decimal,
    p_climatology: Decimal | None,
    weight: Decimal = DEFAULT_MODEL_WEIGHT,
) -> tuple[Decimal, Decimal]:
    """Return (p_final, effective_weight_used).

    If `p_climatology` is None, returns (p_model, 1.0) — no shrinkage.
    """
    if p_climatology is None:
        return (p_model, Decimal("1.0"))
    w = max(Decimal("0"), min(Decimal("1"), weight))
    p_final = w * p_model + (Decimal("1") - w) * p_climatology
    # Clamp to [0,1] for numerical safety
    p_final = max(Decimal("0"), min(Decimal("1"), p_final))
    return (p_final, w)
