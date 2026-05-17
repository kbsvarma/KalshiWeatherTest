# Longshot Lane — Disabled 2026-05-16

## What it was

Earlier versions of the daily-selection logic picked the *single highest-EV*
opportunity per city. Because Kalshi fees follow `0.07 × p × (1-p)` (highest
at p=0.50 and ~zero at p=0 / p=1), the highest absolute edges sometimes
appeared on low-probability markets — e.g., LAX YES T71 at $0.05 with model
prob 26.4% on 2026-04-22.

The math at entry looked fine: pay $0.05, win $1.00, model says 26% chance →
+EV. But realized behavior was poor:

- 74% of these bets lose (you wait long stretches for a winner)
- Variance is enormous (5+ losers in a row routine)
- Settlement-shock losses dominate small wins; one bad rainout = many wins erased
- Hard to size; Kelly says ~1% of bankroll but discrete contract minimums distort it
- Slow convergence to expected value (need 50+ trades before P&L makes sense)

## Why it's disabled now

The replacement strategy is **volume-grinder favorites**:

- Filter: bet only markets where `0.70 ≤ p_model ≤ 0.97`
- Bet *every* qualifying market across cities × thresholds (not just one per city)
- Cap daily exposure at $10 total
- Sort by per-bet EV; take highest-EV bets first within cap
- Accept smaller per-bet EV (~0.5-2¢) for much higher hit rate (~75-90%)

Why favorites work better on Kalshi specifically:

| Market price | Kalshi fee | Edge floor for +EV |
|--------------|-----------:|-------------------:|
| $0.95        | 0.3¢       | tiny edge passes   |
| $0.85        | 0.9¢       | small edge passes  |
| $0.50        | 1.75¢      | needs real edge    |
| $0.20        | 1.1¢       | edge must be big   |
| $0.05        | 0.3¢       | edge must be huge  |

The combination of low fees on favorites + the lognormal nature of forecast
errors (NWS is more accurate near climatology than out in the tails) makes
favorite-grinding the higher-Sharpe play at this bet size.

## Where it lives in code

The decision engine still *computes* longshot opportunities (so we can audit
them in `daily_city_summary.latest.json`), but the **volume-grinder selector**
(`src/kalshi_weather/analytics/volume_selection.py`) filters them out via:

```python
if p_model < thresholds.min_model_probability:   # 0.70
    return _rejected(..., reason="below_favorite_floor")
```

To re-enable: lower `VolumeSelectionThresholds.min_model_probability` (or add
a separate `LongshotSelectionThresholds` with a small per-day allocation).

## Revisit criteria

Reconsider re-enabling a small longshot lane only after:

1. Volume-grinder has placed **≥200 settled trades** with realized PnL on track
2. Calibration data shows the forecast model is *trustworthy at extremes*
   (i.e., when model says p=0.20, it really wins ~20% of the time, not 5%)
3. Either:
   - Bankroll is large enough to absorb 10+ consecutive losers, OR
   - A dedicated longshot capital allocation (~10% of daily cap) is approved

If/when re-enabled, cap longshot exposure at ≤ $1/day separately from the
favorite grinder, and require `provider_spread_f` to be ≤ 4°F (stricter
consensus check than the favorite lane requires).
