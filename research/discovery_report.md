# AFD Feature Discovery — Backtest Report

_Report generated 2026-05-19 11:11 ET_

## Methodology

- **Sample:** 5 cities (PHX, AUS, NYC, MIA, LAX) × 100 days = 200 (city, date) cells
- **Extractor:** `gemma4:26b` via the user's home Ollama instance, JSON-mode disabled, `think:false`, ~8s per AFD
- **Prompt:** discovery_extractor — open-ended features (synoptic drivers, forecaster lean, key quote, bust risk language) rather than a pre-baked 4-bucket enum
- **Ground truth:** observed daily max temp at each city's primary ASOS, verified against Kalshi-resolution CLI products (exact match on the 18 days we could cross-check)
- **Bust threshold:** |forecast − observed| ≥ 5.0°F
- **Discovery / holdout split:** first 70% of unique calendar dates = discovery, last 30% = holdout. A feature is *validated* only if its discovery-set effect (same sign) replicates in holdout with p < 0.20.

## Sample composition

- Total cells: **200**
- Cells with usable abs_error (have obs AND have forecast estimate): **117 (58%)**
- Discovery set: 140 cells
- Holdout set: 60 cells

**Per-city counts:**
- AUS: 100
- LAX: 100

## Forecast quality (LLM-extracted point forecast vs observed)

- Median |error|: **2.50°F**
- IQR |error|: [1.00, 6.50]°F
- Mean signed error: **+2.95°F** (positive = LLM forecast > observed)
- Bust rate (|err| ≥ 5.0°F): **39/117 = 33.3%**

## Discovery-set feature tests

### Binary features (Mann-Whitney U on abs_error, True vs False)

| Feature | True n / median | False n / median | p | rank-biserial |
|---|---|---|---|---|
| model_disagreement_mentioned | 5 / 4.50 | 76 / 2.50 | 0.126 | -0.411 |
| timing_critical | 9 / 6.50 | 72 / 2.50 | 0.145 | -0.299 |
| observational_anchor | 10 / 9.50 | 71 / 2.50 | 0.038 | -0.406 |
| revised_from_previous | 6 / 4.25 | 75 / 2.50 | 0.195 | -0.320 |

### Categorical features (Kruskal-Wallis across buckets)

| Feature | n buckets | H | p | bucket medians |
|---|---|---|---|---|
| confidence_word | 1 | — | — |  |
| forecaster_lean | 4 | — | — |  |
| model_disagreement_direction | 3 | — | — |  |

### Synoptic-driver buckets (Mann-Whitney U: mentioned vs not)

| Driver | with n / median | without n / median | p | rank-biserial |
|---|---|---|---|---|
| marine_layer | 15 / 3.50 | 66 / 2.50 | 0.410 | -0.137 |
| upper_low | 5 / 2.50 | 76 / 2.50 | 0.664 | 0.118 |
| ridge | 14 / 2.75 | 67 / 2.50 | 0.792 | 0.046 |
| frontal | 12 / 2.75 | 69 / 2.50 | 0.894 | -0.025 |
| downslope | 9 / 3.00 | 72 / 2.50 | 0.964 | -0.011 |
| trough | 6 / 2.50 | 75 / 2.50 | 0.993 | 0.004 |

## Holdout validation (features with p<0.10 in discovery)

Comparing discovery effect to holdout effect for each candidate:

| Feature | discovery p | discovery effect | holdout p | holdout effect | replicates? |
|---|---|---|---|---|---|
| observational_anchor | 0.038 | Δmedian=+7.00 | 0.864 | Δmedian=+1.00 | ✗ |

## Limitations

- LLM does not extract a point estimate on every AFD (high_point_f is null when only a range is given; some AFDs have no temperature number).
- 5 cities is enough for discovery but underpowered for rare drivers.
- Forecast-vs-observed error is one metric; predicting Kalshi pricing error would be a stronger test of edge but requires intraday Kalshi book snapshots that we don't have yet.
- Bust threshold of 5°F is somewhat arbitrary; results may differ at 3°F or 8°F.

## Artifacts

- `research/discovery_features.jsonl` — raw LLM extractions
- `research/discovery_joined.csv` — features + outcomes joined
- `research/discovery_report.md` — this report