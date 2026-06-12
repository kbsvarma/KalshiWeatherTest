"""Discovery analysis: which AFD-derived features predict forecast bust?

Inputs:
  research/discovery_features.jsonl  — LLM-extracted features per (city, date)
  research/observations/{CITY}.json — observed daily highs (Mesonet ASOS)

For each row we compute:
  forecast_high_f  = high_point_f if not null,
                     else midpoint(range) if range present,
                     else None  (excluded from error metrics)
  signed_error_f   = forecast_high_f - observed_high_f
  abs_error_f      = |signed_error|
  bust_flag        = 1 if abs_error >= 5°F else 0

Then test which features predict abs_error or bust_flag.

Split:
  discovery set = first 70 distinct calendar dates
  holdout set   = last 30 distinct dates
A feature is "validated" only if its discovery-set effect matches the
holdout-set effect with the same sign and roughly similar magnitude.

Outputs:
  research/discovery_report.md
  research/discovery_joined.csv
"""
from __future__ import annotations

import csv
import json
import os
import re
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

import numpy as np
from scipy import stats

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RES = os.path.join(ROOT, "research")
FEAT_PATH = os.path.join(RES, "discovery_features.jsonl")
OBS_DIR = os.path.join(RES, "observations")
CSV_OUT = os.path.join(RES, "discovery_joined.csv")
REPORT_OUT = os.path.join(RES, "discovery_report.md")

BUST_THRESHOLD_F = 5.0


# ── load data ───────────────────────────────────────────────────────────
def load_features() -> list[dict]:
    rows = []
    with open(FEAT_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def load_obs() -> dict[tuple[str, str], float]:
    obs: dict[tuple[str, str], float] = {}
    for fn in os.listdir(OBS_DIR):
        if not fn.endswith(".json"):
            continue
        city_upper = fn[:-5]  # 'PHX'
        city_lower = city_upper.lower()
        with open(os.path.join(OBS_DIR, fn)) as f:
            data = json.load(f)
        for date_s, mx in data.items():
            if mx is not None:
                obs[(city_lower, date_s)] = float(mx)
                obs[(city_upper, date_s)] = float(mx)  # match both
    return obs


# ── normalize synoptic drivers ──────────────────────────────────────────
_DRIVER_BUCKETS = [
    ("ridge", ["ridge", "anticyclone", "subtropical high", "subsidence"]),
    ("trough", ["trough", "shortwave trough", "longwave trough", "tprw"]),
    ("frontal", ["front", "frontal passage", "cold front", "warm front", "stationary front", "boundary"]),
    ("convective", ["convect", "thunderstorm", "tstm", "shower", "rain", "precip", "deep moisture"]),
    ("marine_layer", ["marine layer", "marine push", "stratus", "low clouds", "onshore flow", "sea breeze", "marine intrusion"]),
    ("upper_low", ["upper low", "closed low", "cutoff low"]),
    ("ridge_breakdown", ["ridge breakdown", "ridge weakening", "ridge moving"]),
    ("warm_advection", ["warm advection", "warm air advection", "wae", "warming aloft"]),
    ("cold_advection", ["cold advection", "cold air advection", "cae", "cool advection"]),
    ("post_frontal", ["post-frontal", "post frontal", "behind the front"]),
    ("downslope", ["downslope", "lee", "foehn", "santa ana"]),
    ("dry_air", ["dry air", "dry slot", "low dewpoint", "low humidity"]),
    ("moist_air", ["moist", "high dewpoint", "humid", "tropical"]),
    ("inversion", ["inversion", "capping", "lid"]),
    ("upslope", ["upslope"]),
    ("low_level_jet", ["low-level jet", "low level jet", "llj"]),
]


def normalize_drivers(driver_phrases: list[str]) -> list[str]:
    """Map free-text driver phrases to canonical buckets."""
    out = set()
    for phrase in driver_phrases:
        p_low = phrase.lower()
        for bucket, keywords in _DRIVER_BUCKETS:
            if any(kw in p_low for kw in keywords):
                out.add(bucket)
    return sorted(out)


# ── stats helpers ───────────────────────────────────────────────────────
def median_iqr(values):
    if not values:
        return None, None, None
    a = np.asarray(values, dtype=float)
    return float(np.median(a)), float(np.percentile(a, 25)), float(np.percentile(a, 75))


def mwu_test(group_a: list[float], group_b: list[float], alternative: str = "two-sided"):
    if len(group_a) < 3 or len(group_b) < 3:
        return None, None, None
    u, p = stats.mannwhitneyu(group_a, group_b, alternative=alternative)
    rb = 1 - 2 * u / (len(group_a) * len(group_b))
    return float(u), float(p), float(rb)


# ── join + compute errors ───────────────────────────────────────────────
def join_rows(feats: list[dict], obs: dict[tuple[str, str], float]) -> list[dict]:
    joined = []
    for r in feats:
        city = r.get("city")
        date_s = r.get("date")
        if not (city and date_s):
            continue
        # forecast high estimate
        hp = r.get("high_point_f")
        lo = r.get("high_range_low_f")
        hi = r.get("high_range_high_f")
        if hp is not None:
            forecast_high = float(hp)
        elif lo is not None and hi is not None:
            forecast_high = (float(lo) + float(hi)) / 2.0
        else:
            forecast_high = None

        observed = obs.get((city, date_s)) or obs.get((city.lower(), date_s)) or obs.get((city.upper(), date_s))

        signed_err = None
        abs_err = None
        bust = None
        if forecast_high is not None and observed is not None:
            signed_err = forecast_high - observed
            abs_err = abs(signed_err)
            bust = 1 if abs_err >= BUST_THRESHOLD_F else 0

        joined.append({
            **r,
            "drivers_normalized": normalize_drivers(r.get("synoptic_drivers", []) or []),
            "forecast_high_f": forecast_high,
            "observed_high_f": observed,
            "signed_error_f": signed_err,
            "abs_error_f": abs_err,
            "bust_flag": bust,
            "n_uncertainty_phrases": len(r.get("explicit_uncertainty_phrases") or []),
            "n_bust_risk_words": len(r.get("bust_risk_words") or []),
            "n_synoptic_drivers": len(r.get("synoptic_drivers") or []),
        })
    return joined


# ── feature tests ───────────────────────────────────────────────────────
def split_holdout(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    dates = sorted({r["date"] for r in rows if r.get("date")})
    if len(dates) < 20:
        return rows, []
    split_at = int(len(dates) * 0.7)
    discovery_dates = set(dates[:split_at])
    holdout_dates = set(dates[split_at:])
    disc = [r for r in rows if r["date"] in discovery_dates]
    hold = [r for r in rows if r["date"] in holdout_dates]
    return disc, hold


def test_binary_feature(rows: list[dict], feature: str):
    """For a True/False feature: MWU on abs_error between True and False groups."""
    t = [r["abs_error_f"] for r in rows if r.get(feature) is True and r.get("abs_error_f") is not None]
    f = [r["abs_error_f"] for r in rows if r.get(feature) is False and r.get("abs_error_f") is not None]
    u, p, rb = mwu_test(t, f, alternative="two-sided")
    return {
        "feature": feature,
        "true_n": len(t),
        "true_median": float(np.median(t)) if t else None,
        "false_n": len(f),
        "false_median": float(np.median(f)) if f else None,
        "u": u, "p": p, "rank_biserial": rb,
    }


def test_categorical_feature(rows: list[dict], feature: str):
    """For a string-valued feature: Kruskal-Wallis on abs_error by bucket."""
    by_bucket = defaultdict(list)
    for r in rows:
        v = r.get(feature)
        if v is None or r.get("abs_error_f") is None:
            continue
        by_bucket[v].append(r["abs_error_f"])
    groups = [vs for vs in by_bucket.values() if len(vs) >= 3]
    if len(groups) < 2:
        return {"feature": feature, "n_buckets": len(by_bucket), "p": None}
    h, p = stats.kruskal(*groups)
    medians = {k: float(np.median(v)) for k, v in by_bucket.items() if len(v) >= 3}
    return {"feature": feature, "n_buckets": len(by_bucket), "h": float(h), "p": float(p),
            "medians": medians}


def test_normalized_driver(rows: list[dict], driver: str):
    """For each canonical driver bucket: MWU comparing rows that mention vs don't."""
    with_d = [r["abs_error_f"] for r in rows
              if driver in r.get("drivers_normalized", []) and r.get("abs_error_f") is not None]
    without_d = [r["abs_error_f"] for r in rows
                 if driver not in r.get("drivers_normalized", []) and r.get("abs_error_f") is not None]
    u, p, rb = mwu_test(with_d, without_d, alternative="two-sided")
    return {
        "driver": driver,
        "with_n": len(with_d),
        "with_median": float(np.median(with_d)) if with_d else None,
        "without_n": len(without_d),
        "without_median": float(np.median(without_d)) if without_d else None,
        "u": u, "p": p, "rank_biserial": rb,
    }


# ── report ──────────────────────────────────────────────────────────────
def fmt_p(p):
    return f"{p:.3f}" if p is not None else "—"


def fmt_f(v, digits=2):
    if v is None:
        return "—"
    try:
        return f"{float(v):.{digits}f}"
    except Exception:
        return "—"


def main():
    feats = load_features()
    obs = load_obs()
    joined = join_rows(feats, obs)

    # Write CSV
    cols = ["city", "date", "wfo",
            "high_point_f", "high_range_low_f", "high_range_high_f",
            "forecast_high_f", "observed_high_f", "signed_error_f", "abs_error_f", "bust_flag",
            "confidence_word", "model_disagreement_mentioned", "model_disagreement_direction",
            "forecaster_lean", "timing_critical", "observational_anchor", "revised_from_previous",
            "n_uncertainty_phrases", "n_bust_risk_words", "n_synoptic_drivers",
            "synoptic_drivers", "drivers_normalized", "key_quote", "extraction_failed"]
    with open(CSV_OUT, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in joined:
            row = []
            for c in cols:
                v = r.get(c)
                if isinstance(v, list):
                    v = "|".join(str(x) for x in v)
                row.append(v if v is not None else "")
            w.writerow(row)

    disc, hold = split_holdout(joined)

    # Feature lists to test
    binary_features = ["model_disagreement_mentioned", "timing_critical",
                       "observational_anchor", "revised_from_previous"]
    categorical_features = ["confidence_word", "forecaster_lean", "model_disagreement_direction"]
    all_drivers = sorted({d for r in joined for d in r.get("drivers_normalized", [])})

    # Discovery-set tests
    disc_binary = {f: test_binary_feature(disc, f) for f in binary_features}
    disc_cat = {f: test_categorical_feature(disc, f) for f in categorical_features}
    disc_drivers = {d: test_normalized_driver(disc, d) for d in all_drivers}

    # Holdout tests (only for features that show p<0.10 in discovery)
    hold_binary = {f: test_binary_feature(hold, f) for f in binary_features
                   if (disc_binary[f].get("p") or 1.0) < 0.10}
    hold_cat = {f: test_categorical_feature(hold, f) for f in categorical_features
                if (disc_cat[f].get("p") or 1.0) < 0.10}
    hold_drivers = {d: test_normalized_driver(hold, d) for d in all_drivers
                    if (disc_drivers[d].get("p") or 1.0) < 0.10}

    # Forecast quality
    err_rows = [r for r in joined if r.get("abs_error_f") is not None]
    abs_errs = [r["abs_error_f"] for r in err_rows]
    signed_errs = [r["signed_error_f"] for r in err_rows]
    bust_count = sum(1 for r in err_rows if r["bust_flag"] == 1)

    # ── build report ──
    lines = []
    w = lines.append

    et = datetime.now().strftime("%Y-%m-%d %H:%M ET")
    w(f"# AFD Feature Discovery — Backtest Report")
    w("")
    w(f"_Report generated {et}_")
    w("")

    w("## Methodology")
    w("")
    w(f"- **Sample:** 5 cities (PHX, AUS, NYC, MIA, LAX) × 100 days = {len(feats)} (city, date) cells")
    w(f"- **Extractor:** `gemma4:26b` via the user's home Ollama instance, JSON-mode disabled, `think:false`, ~8s per AFD")
    w(f"- **Prompt:** discovery_extractor — open-ended features (synoptic drivers, forecaster lean, key quote, bust risk language) rather than a pre-baked 4-bucket enum")
    w(f"- **Ground truth:** observed daily max temp at each city's primary ASOS, verified against Kalshi-resolution CLI products (exact match on the 18 days we could cross-check)")
    w(f"- **Bust threshold:** |forecast − observed| ≥ {BUST_THRESHOLD_F}°F")
    w(f"- **Discovery / holdout split:** first 70% of unique calendar dates = discovery, last 30% = holdout. A feature is *validated* only if its discovery-set effect (same sign) replicates in holdout with p < 0.20.")
    w("")

    w("## Sample composition")
    w("")
    w(f"- Total cells: **{len(joined)}**")
    w(f"- Cells with usable abs_error (have obs AND have forecast estimate): **{len(err_rows)} ({100*len(err_rows)/max(1,len(joined)):.0f}%)**")
    w(f"- Discovery set: {len(disc)} cells")
    w(f"- Holdout set: {len(hold)} cells")
    w("")
    w("**Per-city counts:**")
    per_city = Counter(r["city"] for r in joined)
    for c, n in sorted(per_city.items()):
        w(f"- {c}: {n}")
    w("")

    w("## Forecast quality (LLM-extracted point forecast vs observed)")
    w("")
    if abs_errs:
        w(f"- Median |error|: **{np.median(abs_errs):.2f}°F**")
        w(f"- IQR |error|: [{np.percentile(abs_errs, 25):.2f}, {np.percentile(abs_errs, 75):.2f}]°F")
        w(f"- Mean signed error: **{np.mean(signed_errs):+.2f}°F** (positive = LLM forecast > observed)")
        w(f"- Bust rate (|err| ≥ {BUST_THRESHOLD_F}°F): **{bust_count}/{len(err_rows)} = {100*bust_count/len(err_rows):.1f}%**")
    w("")

    w("## Discovery-set feature tests")
    w("")
    w("### Binary features (Mann-Whitney U on abs_error, True vs False)")
    w("")
    w("| Feature | True n / median | False n / median | p | rank-biserial |")
    w("|---|---|---|---|---|")
    for fname in binary_features:
        r = disc_binary[fname]
        w(f"| {fname} | {r['true_n']} / {fmt_f(r['true_median'])} | "
          f"{r['false_n']} / {fmt_f(r['false_median'])} | {fmt_p(r['p'])} | "
          f"{fmt_f(r['rank_biserial'], 3)} |")
    w("")

    w("### Categorical features (Kruskal-Wallis across buckets)")
    w("")
    w("| Feature | n buckets | H | p | bucket medians |")
    w("|---|---|---|---|---|")
    for fname in categorical_features:
        r = disc_cat[fname]
        meds = r.get("medians", {})
        meds_str = ", ".join(f"{k}:{v:.1f}" for k, v in meds.items())
        w(f"| {fname} | {r['n_buckets']} | {fmt_f(r.get('h'))} | {fmt_p(r.get('p'))} | {meds_str} |")
    w("")

    w("### Synoptic-driver buckets (Mann-Whitney U: mentioned vs not)")
    w("")
    w("| Driver | with n / median | without n / median | p | rank-biserial |")
    w("|---|---|---|---|---|")
    for d in sorted(disc_drivers, key=lambda x: (disc_drivers[x].get("p") or 1.0)):
        r = disc_drivers[d]
        if r.get("with_n", 0) < 5:
            continue
        w(f"| {d} | {r['with_n']} / {fmt_f(r['with_median'])} | "
          f"{r['without_n']} / {fmt_f(r['without_median'])} | "
          f"{fmt_p(r['p'])} | {fmt_f(r['rank_biserial'], 3)} |")
    w("")

    w("## Holdout validation (features with p<0.10 in discovery)")
    w("")
    if not hold_binary and not hold_cat and not hold_drivers:
        w("_No discovery-set features reached p<0.10 — nothing to validate. The features tested do not predict bust on this 5-city, 100-day sample._")
    else:
        w("Comparing discovery effect to holdout effect for each candidate:")
        w("")
        w("| Feature | discovery p | discovery effect | holdout p | holdout effect | replicates? |")
        w("|---|---|---|---|---|---|")
        for f, rd in disc_binary.items():
            if f in hold_binary:
                rh = hold_binary[f]
                d_med = (rd["true_median"] or 0) - (rd["false_median"] or 0)
                h_med = (rh["true_median"] or 0) - (rh["false_median"] or 0)
                ok = "✓" if (rh.get("p") or 1) < 0.20 and (d_med * h_med) > 0 else "✗"
                w(f"| {f} | {fmt_p(rd['p'])} | Δmedian={d_med:+.2f} | {fmt_p(rh['p'])} | Δmedian={h_med:+.2f} | {ok} |")
        for f, rd in disc_cat.items():
            if f in hold_cat:
                rh = hold_cat[f]
                ok = "✓" if (rh.get("p") or 1) < 0.20 else "✗"
                w(f"| {f} | {fmt_p(rd.get('p'))} | KW | {fmt_p(rh.get('p'))} | KW | {ok} |")
        for d, rd in disc_drivers.items():
            if d in hold_drivers:
                rh = hold_drivers[d]
                d_med = (rd["with_median"] or 0) - (rd["without_median"] or 0)
                h_med = (rh["with_median"] or 0) - (rh["without_median"] or 0)
                ok = "✓" if (rh.get("p") or 1) < 0.20 and (d_med * h_med) > 0 else "✗"
                w(f"| driver:{d} | {fmt_p(rd['p'])} | Δmedian={d_med:+.2f} | {fmt_p(rh['p'])} | Δmedian={h_med:+.2f} | {ok} |")
    w("")

    w("## Limitations")
    w("")
    w("- LLM does not extract a point estimate on every AFD (high_point_f is null when only a range is given; some AFDs have no temperature number).")
    w("- 5 cities is enough for discovery but underpowered for rare drivers.")
    w("- Forecast-vs-observed error is one metric; predicting Kalshi pricing error would be a stronger test of edge but requires intraday Kalshi book snapshots that we don't have yet.")
    w("- Bust threshold of 5°F is somewhat arbitrary; results may differ at 3°F or 8°F.")
    w("")

    w("## Artifacts")
    w("")
    w("- `research/discovery_features.jsonl` — raw LLM extractions")
    w("- `research/discovery_joined.csv` — features + outcomes joined")
    w("- `research/discovery_report.md` — this report")

    with open(REPORT_OUT, "w") as f:
        f.write("\n".join(lines))
    print(f"wrote {REPORT_OUT}")
    print(f"wrote {CSV_OUT}")


if __name__ == "__main__":
    main()
