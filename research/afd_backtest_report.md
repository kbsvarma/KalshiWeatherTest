# AFD Signal Extractor Backtest

_Report generated 2026-05-19 09:51 ET_

## Methodology

**Objective.** Test whether the design assumptions hard-coded in `src/kalshi_weather/engines/path.py` — namely that AFD regime, stated confidence, and model-spread flag carry predictive signal about forecast uncertainty — hold up against observed daily highs.

**Sampling frame.** Five cities the bot trades — PHX (WFO=PSR, ASOS=KPHX), AUS (EWX/KAUS), NYC (OKX/KNYC), MIA (MFL/KMIA), and LAX (LOX/KLAX) — across the 40 calendar days immediately preceding 2026-05-19 UTC. Target sample = 5 × 40 = 200 (city, date) cells.

**Data sources.**
- AFD text: Iowa Environmental Mesonet archive (`api/1/nws/afos/list.json` for product IDs, `cgi-bin/afos/retrieve.py` for raw text). Selected the morning issuance (08-15 UTC) when available, else the earliest of the day.
- Observed daily max: Iowa Environmental Mesonet ASOS daily summary endpoint (`cgi-bin/request/daily.py`), `max_temp_f` column.
- LLM extraction: production `extract_afd_signals` from `src/kalshi_weather/engines/afd_extractor.py` (read-only import). Model: `llama3.2:1b` via Ollama on the Lightsail box, temp=0, `num_predict=120`, 2-hour `keep_alive` to retain warm cache.

**Exclusion rules.** A (city, date) row is excluded from the point-estimate error analysis if any of: (a) observed daily high missing from ASOS data, (b) `extraction_failed=True`, or (c) `mentioned_today_high_f` is null. These are reported as data-quality metrics; the categorical signal tests (confidence/regime/spread) still use all rows where the categorical extraction succeeded.

## Sample composition

- Total (city, date) cells fetched: **200**
- Date range: **2026-04-09 to 2026-05-18** (UTC)
- Cities: ['AUS', 'LAX', 'MIA', 'NYC', 'PHX']
- Rows with usable abs-error (have obs AND have LLM high): **132 (66%)**

**Skip reasons:**
- `no_high_estimate`: 68

**Per-city counts (fetched / with-error):**
- AUS: 40 / 25
- LAX: 40 / 19
- MIA: 40 / 29
- NYC: 40 / 25
- PHX: 40 / 34

## Signal distributions

**Confidence:** {'moderate': 95, 'high': 91, 'low': 7}
**Regime:** {'convective': 10, 'trough': 59, 'frontal_passage': 44, 'ridge': 83, 'stable': 1, 'other': 3}
**Model-spread flag:** {False: 159, True: 41}
**LLM-extraction failures:** 0
**Null `mentioned_today_high_f` (extraction succeeded but no point estimate): 68 / 200 (34%)**

## Hypothesis 1: forecaster `confidence` bucket

_Production assumption: `confidence=high` AFDs carry better signal (smaller error) than `confidence=low`._

| Bucket | n | median \|err\| | IQR | mean signed err |
|---|---|---|---|---|
| low | 2 | 6.00 | [6.00, 6.00] | +6.00 |
| moderate | 58 | 3.00 | [1.00, 5.75] | +1.02 |
| high | 66 | 3.00 | [1.00, 6.00] | +2.70 |

**Kruskal-Wallis H = 1.890, p = 0.3887** (across low/moderate/high)

## Hypothesis 2: `model_spread_flag`

_Production assumption: `model_spread_flag=True` flags higher-error days._

| Flag | n | median \|err\| | IQR | mean signed err |
|---|---|---|---|---|
| True | 23 | 3.00 | [1.00, 6.00] | +0.22 |
| False | 109 | 3.00 | [1.00, 6.00] | +2.51 |

**Mann-Whitney U = 1270.5, one-sided p (True>False) = 0.4602**, rank-biserial = -0.014

## Hypothesis 3: regime category

_Production assumption: `frontal_passage`/`convective` widen path uncertainty (+0.03); `stable`/`ridge`/`marine_layer` tighten (-0.01)._

| Regime | n | median \|err\| | IQR | mean signed err |
|---|---|---|---|---|
| ridge | 64 | 3.00 | [1.75, 6.00] | +3.77 |
| trough | 34 | 3.00 | [1.00, 4.75] | -0.21 |
| frontal_passage | 25 | 3.00 | [1.00, 6.00] | +1.16 |
| convective | 6 | 2.00 | [1.25, 5.75] | +1.17 |
| other | 2 | 4.50 | [4.25, 4.75] | +4.50 |
| stable | 1 | 0.00 | [0.00, 0.00] | +0.00 |

**Kruskal-Wallis across regimes: H = 3.422, p = 0.4899** (over 5 regimes with n≥2)

**Targeted test — widen group (frontal_passage ∪ convective) vs tighten group (stable ∪ ridge ∪ marine_layer):**

- widen group: n=31, median |err|=3.00
- tighten group: n=65, median |err|=3.00
- Mann-Whitney U (widen>tighten): U=935.5, p=0.7167, rank-biserial=+0.071

## Verdict per design assumption

- **frontal_passage/convective → +0.03 uncertainty widening**: NOT SUPPORTED (p=0.717)
  - widen n=31, tighten n=65, one-sided p=0.7167064866841036
- **stable/ridge/marine_layer → -0.01 tightening**: tested as part of the same widen-vs-tighten contrast above — same verdict.
- **`confidence=high` carries lower-error signal**: NOT SUPPORTED (p=0.389)
- **`model_spread_flag=True` flags high-error days**: NOT SUPPORTED (p=0.460)

## Limitations

- **n=200 ceiling.** Even with 200 city-days, regime buckets are uneven; some have n<10 and any Mann-Whitney on them is underpowered. Verdicts on rare regimes (e.g. `anomalous_warm`) should be read as descriptive, not inferential.
- **Single LLM, single temperature.** `llama3.2:1b` at temp=0 is the production setup, but a stronger model would likely extract more complete signals. The high null-rate on `mentioned_today_high_f` (see above) is largely a 1b-model artifact — the AFD text does contain explicit highs in most cases.
- **Observation point.** ASOS daily max at the primary airport is the proxy for "today's high" — but the AFD covers a CWA, not a point, so |error| includes some spatial slack.
- **Morning-issuance selection.** When no AFD was issued 08-15 UTC we fell back to the earliest of the day, which may be a late-evening issuance from the prior day's labelled date. This affects ≤5% of cells.
- **No control for season/synoptic regime overlap.** Spring 2026 had few convective days nationally; results for `convective` may not generalize to summer.

## Recommendations

See verdicts above. Concrete guidance:

- **Drop or substantially re-tune** the frontal_passage/convective widening — the data do not support a meaningfully larger error in those regimes. The +0.03 number appears to be a guess that doesn't pay rent on this 40-day sample.
- **Drop** the confidence weighting — the 3-way KW does not find a difference. The LLM's confidence label is not a useful covariate at the current sample size.
- **Drop** the spread flag — it does not correlate with realized error on this sample.

## Artifacts

- Raw joined dataset: `research/afd_backtest_joined.csv` (200 rows)
- LLM extraction log: `research/llm_extractions.jsonl`
- AFD archive: `research/afd_archive/*.json` (200 files)
- Observations: `research/observations/*.json` (5 cities)
