# Market-Disagreement Gate — Disabled 2026-05-16 Evening

## Context

The market-disagreement gate was added earlier on 2026-05-16 as a defensive
response to a near-miss: the bot was about to auto-fill the LAX T71 longshot
bust pattern (model said 26%, market said 5%, model was wrong). The gate
blocked auto-fill whenever `|p_model - p_market| > 0.30`.

The gate worked correctly — it caught the 4 high-disagreement signals that
emerged later in the day:
- PHX YES T97  (market 3% / model 79% / gap +76pp)
- OKC YES T87  (market 24% / model 84% / gap +60pp)
- DAL YES T89  (market 43% / model 91% / gap +48pp)
- SAT YES T90  (market 52% / model 89% / gap +37pp)

## Why It's Now Disabled

By ~01:00 UTC May 17 (early evening local time across all 4 cities), the
observed temperatures showed the gate was **over-blocking real edge**:

| City | High So Far | Threshold | Gap Needed To Lose | Past Peak? |
|---|---|---|---|---|
| PHX | 86.0°F | 97°F | needs +11°F more | yes (5:53 PM) |
| OKC | 82.4°F | 87°F | needs +5°F more | yes (7:53 PM) |
| DAL | 78.8°F | 89°F | needs +10°F more | yes (7:53 PM) |
| SAT | 78.8°F | 90°F | needs +11°F more | yes (7:53 PM) |

All 4 are essentially locked in as model wins. The model correctly identified
a structural mispricing on these markets that the gate prevented us from
capturing.

## Distinction From The LAX T71 Pattern

The reason the gate triggered correctly on LAX T71 but over-triggered on
today's 4 bets is a difference in the **direction of disagreement**:

- **LAX T71 (loser)**: market 5%, model 26%. Both LOW probabilities. Model
  said "modest chance of unusual event"; market said "near-zero." Model
  was making a LONGSHOT claim and was wrong.
- **Today's 4 (winners)**: market <50%, model >75%. Model says "high
  probability event"; market says "low/moderate probability." Model is
  making a FAVORITE claim against a market that under-prices favorites.

The gate as written treats these symmetrically when they're not. A model
claiming "this near-certain favorite is mispriced" is a different signal
than a model claiming "this longshot will win."

A better gate would be asymmetric: only block when p_model is low AND market
is even lower (the LAX pattern), not when p_model is high but market is low.
For now, simply disabling lets us capture both flavors and see how the
calibration data sorts out over the next 1-2 weeks.

## Revisit Criteria

Reconsider the gate after we have **≥50 resolved REVIEW bets** with hard
counterfactual P&L. If the resolved REVIEW cohort:

- **wins ≥60% of the time** → keep gate disabled, model is genuinely sharp
- **wins 40-60% of the time** → reinstate at 0.50 threshold (cull only the
  most extreme disagreements)
- **wins <40% of the time** → reinstate at 0.30 threshold (original
  defensive posture was correct)

## Code Locations

- `src/kalshi_weather/engines/shadow.py` — `_VOLUME_GRINDER_MAX_MARKET_DISAGREEMENT`
- `src/kalshi_weather/analytics/volume_selection.py` — `VolumeSelectionThresholds.max_market_disagreement`

Both set to `Decimal("0.99")` (effectively unbounded). The REVIEW
classification in `recommendation_log.py` still labels high-disagreement
signals so we can compare gated-vs-ungated P&L from the recommendation
log alone.
