# LOW Temperature Markets — Deferred to Phase 4

**Status:** Not yet integrated. Settlement-rule parser supports them
(`daily_low_temperature_f`), but forecast + path engines only model daily MAX.

**Why deferred (2026-05-16):**
Adding low-temp markets requires substantial engine changes — not a config-only
expansion as initially thought:

1. **`engines/forecast.py`** — currently does `max(window_values)` to extract
   the day's high from the hourly provider path. For LOW markets we need
   `min(window_values)` and the asymmetric Gaussian PMF must center on the
   minimum, not the maximum.

2. **`engines/path.py`** — tracks `current_high_so_far_f` and computes
   reachability as "can the temp climb to the threshold?" For LOW markets
   we need `current_low_so_far_f` and reachability becomes "can the temp
   drop below the threshold?" The late-day-decay logic also inverts —
   for highs we decay as we approach sunset; for lows we decay as we
   approach sunrise.

3. **Bias corrections** in `forecast.py` — currently target daytime heating
   biases (cloud suppression of highs, marine intrusion suppressing highs,
   etc.). Overnight low biases work differently — radiative cooling under
   clear skies drives lower-than-normal lows; cloud cover RAISES lows; wind
   mixes the atmosphere and RAISES lows.

4. **Climatological priors** — heating window is irrelevant for lows;
   relevant period is overnight (typically 1 AM - 6 AM local).

**Estimated effort:** 4-6 hours of focused engine work + careful test coverage
to avoid regressing the working HIGH-market logic.

**Available canonical Kalshi tickers (verified via API 2026-05-16):**
- KXLOWTATL, KXLOWTAUS, KXLOWTBOS, KXLOWTCHI, KXLOWTDAL, KXLOWTDC,
  KXLOWTDEN, KXLOWTHOU, KXLOWTLAX, KXLOWTLV, KXLOWTMIA, KXLOWTMIN,
  KXLOWTNOLA, KXLOWTNYC, KXLOWTOKC, KXLOWTPHIL, KXLOWTPHX, KXLOWTSATX
  (18 cities total — full parity with HIGH market coverage)

**Estimated daily volume after full integration:**
+~10-15 evaluated markets per cycle, +~3-5 qualifying bets per day.

**Revisit when:** the rich-logging infrastructure has accumulated 1-2 weeks
of HIGH-market settlement data, so we can validate calibration before
doubling the surface area.
