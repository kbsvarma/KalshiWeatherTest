# Scientific Improvement Roadmap

Deep-dive inventory from the 2026-05-16 evening review pass. Ranked by
material impact ÷ engineering effort. Tier 1 items #1 was implemented;
#2 and #3 are queued.

## TIER 1 — Material wins (queued / implemented)

### ✅ T1.1 — Multi-bracket per city  *(implemented 2026-05-17 ~02:30 ET)*

**Was:** "one position per city" rule blocked all subsequent bets after the
first market filled in a city. Correct for `-T` threshold markets (where
multiple thresholds are semantically tangled) but wrong for `-B` range
markets which are independent non-overlapping bins.

**Now:** up to **3 distinct markets per city per day** allowed. Daily $15
cap still binds across the whole portfolio. Same-market dedup retained.

**Impact:** went from 4 to 7 live orders in the same cycle. Will scale up
overnight as more cities reach their per-day cap.

### 🔜 T1.2 — NCEI climatological prior  *(deferred, ~3h)*

**Why:** anchors model `p_yes` against the 30-year historical baseline for
each (city, date, threshold) combination. When model says 80% but
climatology says 20%, the bot can apply a Bayesian shrinkage toward
climatology — catches model busts without needing the market as a referee.

**Implementation sketch:**
1. Pull `daily-normals-1991-2020` for all 18 cities from NCEI API
2. Cache in `data/reference/climate_normals/<city>.json`
3. For each market threshold, compute
   `p_climatology = P(climate-distribution-of-high >= threshold)`
4. In path engine, compute shrunk probability:
   `p_final = w·p_model + (1-w)·p_climatology` where `w` is a calibration
   constant starting at 0.85 (favor model) and adjusted by realized error.
5. Log `p_climatology` and shrinkage applied in recommendation_log.

**Why deferred:** clean 3-hour build but adds complexity; tonight's volume
unlock was higher impact. Best done after we have a week of real
settlement data so we can tune `w`.

### 🔜 T1.3 — Persistence baseline  *(deferred, ~1h)*

**Why:** yesterday's actual daily high is a surprisingly strong predictor
of today's high (autocorrelation ~0.6 in temperate latitudes). When the
ensemble forecast deviates >5°F from persistence, that's a high-uncertainty
signal that warrants wider sigma.

**Implementation:**
1. From `market_settlements`, pull each city's high from `today - 1 day`
2. Add to forecast engine as an additional pseudo-provider with weight 0.10
3. Or use as a sanity-check: if `|ensemble_mean - yesterday_high| > 5°F`,
   inflate sigma by +1°F.

## TIER 2 — Worth this week

### T2.1 — Cross-bracket correlation in risk math
Multiple `-B` NO bets in same city are highly positively correlated. The
portfolio_risk_units calculation currently treats them as independent.
Fix: add an intra-market correlation matrix (e.g., 0.85 for same-city same-side).

### T2.2 — Per-city × per-season bias segmentation
Empirical bias correction currently averages all historical pairs.
Marine cities (LAX, SFO, SEA, MIA) have very different summer vs winter
bias structures. Segment by (city, season=DJF/MAM/JJA/SON) once ≥10
samples per bin.

### T2.3 — Brier-score-weighted provider blending
Current provider weights are static priors. Should be exponentially
weighted moving average (EWMA) of each provider's per-bet Brier score
on recent settled bets. λ=0.95 (about 20-day half-life).

### T2.4 — Fractional Kelly sizing
Replace hardcoded 1 contract with `max(1, min(3, round(0.25 × edge/variance)))`.
Variance estimated from the conditioned PMF. Capped at 3 contracts/market.

### T2.5 — Reliability diagrams + isotonic recalibration
After 100+ resolved bets, fit isotonic regression to remap raw `p_model`
to empirically-calibrated probability. Replaces the linear "trust the
model" assumption.

## TIER 3 — Research-grade (future quarters)

### T3.1 — Bayesian model averaging with online updates
Beta(α, β) prior on each provider's accuracy per (city, season). Update
α/β after each settlement. Use posterior mean as provider weight.

### T3.2 — Upper-air anomaly synoptic classifier
Pull 500/700/850mb temperature anomalies from NCEP reanalysis. Classify
each day into 8 regimes (e.g., zonal flow, trough passage, ridge,
cut-off low). Train per-regime bias corrections.

### T3.3 — Variance-aware sigma per (city, season, lead-hour)
Current base_sigma_f is fixed per provider. Should be a 3D tensor learned
from realized forecast errors.

### T3.4 — Sub-hourly forecast disaggregation
Open-Meteo provides hourly forecasts. Currently we extract daily max.
Could use within-day variance to inform the PMF tail shape.

### T3.5 — Heat-wave / cold-front pattern recognition
Multi-day temperature anomaly patterns predict next-day swings. RNN
classifier over 7-day temp history.

### T3.6 — Marine boundary layer parameterization
For SF/SEA/LAX/MIA, fold marine layer thickness (from radiosonde) into
the bias correction. Currently we use a static `marine_intrusion_cap_f`.

### T3.7 — Urban heat island calibration
NY/CHI/DC measurably warmer than surrounding airport stations. Add UHI
offset per city, learned from station-to-airport comparison.

### T3.8 — Concurrent forecast fetching
Currently serial: 18 cities × ~3 API calls each. Switch to asyncio for
~6× speedup of the cycle. Cuts cycle time from ~3 min to ~30s.

### T3.9 — Order-status polling
After IOC submission, verify the order actually filled (not just
"submitted"). Update `live_orders` row with final status.

### T3.10 — Drawdown-based daily kill switch
If realized P&L drops > $5 within 60 minutes, auto-flip to dry-run mode.

### T3.11 — Per-cycle rate limit
Rate-limit to e.g., 5 new bets per cycle even if cap allows more. Smooths
exposure over the day.

### T3.12 — Slippage modeling per orderbook depth
Walk the implied ask ladder to compute true execution price for size > 1.
Currently we assume 1 contract fills at top of book.

---

## Notes on Risk

- Daily cap ($15) still dominates as binding constraint
- Per-city cap (3 markets × ~$0.65 avg = ~$2 max per city)
- Theoretical max if all 18 cities fire 3 bets each: $36 — but daily cap stops at $15
- Worst single-city loss: $2 (all 3 bets fail simultaneously)

The architecture is now correctly calibrated for the user's $2-5/bet,
$15/day volume-grinder philosophy.
