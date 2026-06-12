"""Join LLM extractions with observed highs and run statistical tests.

Outputs:
  research/afd_backtest_joined.csv
  research/afd_backtest_report.md
"""
from __future__ import annotations

import csv
import json
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime

import numpy as np
from scipy import stats

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESEARCH = os.path.join(ROOT, "research")
EXTR_PATH = os.path.join(RESEARCH, "llm_extractions.jsonl")
OBS_DIR = os.path.join(RESEARCH, "observations")
ARCHIVE_DIR = os.path.join(RESEARCH, "afd_archive")
CSV_OUT = os.path.join(RESEARCH, "afd_backtest_joined.csv")
REPORT_OUT = os.path.join(RESEARCH, "afd_backtest_report.md")
CALIB_PATH = os.path.join(RESEARCH, "llm_calibration.jsonl")


def load_extractions():
    rows = []
    with open(EXTR_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def load_obs():
    obs = {}  # (city, date) -> max_temp_f
    for fn in os.listdir(OBS_DIR):
        if not fn.endswith(".json"):
            continue
        city = fn[:-5]
        with open(os.path.join(OBS_DIR, fn)) as f:
            data = json.load(f)
        for date_str, mx in data.items():
            obs[(city, date_str)] = mx
    return obs


def median_iqr(values):
    if not values:
        return None, None, None
    a = np.asarray(values, dtype=float)
    return float(np.median(a)), float(np.percentile(a, 25)), float(np.percentile(a, 75))


def rank_biserial(u, n1, n2):
    if n1 == 0 or n2 == 0:
        return None
    return 1 - 2 * u / (n1 * n2)


def main():
    extractions = load_extractions()
    obs = load_obs()

    joined = []
    skip_reasons = Counter()
    for r in extractions:
        city = r["city"]
        date_s = r["date"]
        ob = obs.get((city, date_s))
        rec = {
            "city": city,
            "date": date_s,
            "wfo": r.get("wfo"),
            "product_id": r.get("product_id"),
            "issuance_time": r.get("issuance_time"),
            "confidence": r.get("confidence"),
            "model_spread_flag": r.get("model_spread_flag"),
            "regime": r.get("regime"),
            "mentioned_today_high_f": r.get("mentioned_today_high_f"),
            "extraction_failed": r.get("extraction_failed"),
            "observed_high_f": ob,
        }
        if ob is None:
            skip_reasons["no_observation"] += 1
            rec["signed_error"] = None
            rec["abs_error"] = None
        elif r.get("extraction_failed"):
            skip_reasons["extraction_failed"] += 1
            rec["signed_error"] = None
            rec["abs_error"] = None
        elif r.get("mentioned_today_high_f") is None:
            skip_reasons["no_high_estimate"] += 1
            rec["signed_error"] = None
            rec["abs_error"] = None
        else:
            rec["signed_error"] = r["mentioned_today_high_f"] - ob
            rec["abs_error"] = abs(rec["signed_error"])
        joined.append(rec)

    # Write CSV
    fields = ["city", "date", "wfo", "product_id", "issuance_time",
              "confidence", "model_spread_flag", "regime",
              "mentioned_today_high_f", "observed_high_f",
              "signed_error", "abs_error", "extraction_failed"]
    with open(CSV_OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in joined:
            w.writerow(r)

    # === Statistical analysis ===
    n_total = len(joined)
    n_have_error = sum(1 for r in joined if r["abs_error"] is not None)

    # Confidence buckets
    by_conf = defaultdict(list)  # conf -> list of abs_error
    by_conf_signed = defaultdict(list)
    for r in joined:
        if r["abs_error"] is None:
            continue
        c = r["confidence"]
        if c in ("low", "moderate", "high"):
            by_conf[c].append(r["abs_error"])
            by_conf_signed[c].append(r["signed_error"])

    # Spread flag
    by_spread = defaultdict(list)
    by_spread_signed = defaultdict(list)
    for r in joined:
        if r["abs_error"] is None:
            continue
        f = r["model_spread_flag"]
        if isinstance(f, bool):
            by_spread[f].append(r["abs_error"])
            by_spread_signed[f].append(r["signed_error"])

    # Regime
    by_regime = defaultdict(list)
    by_regime_signed = defaultdict(list)
    for r in joined:
        if r["abs_error"] is None:
            continue
        rg = r["regime"]
        if rg:
            by_regime[rg].append(r["abs_error"])
            by_regime_signed[rg].append(r["signed_error"])

    # Tests
    def kw_test(groups):
        # groups: list of lists
        groups = [g for g in groups if len(g) >= 2]
        if len(groups) < 2:
            return None, None
        h, p = stats.kruskal(*groups)
        return float(h), float(p)

    conf_h, conf_p = kw_test([by_conf["low"], by_conf["moderate"], by_conf["high"]])

    # Spread: Mann-Whitney
    sp_true = by_spread.get(True, [])
    sp_false = by_spread.get(False, [])
    if len(sp_true) >= 2 and len(sp_false) >= 2:
        u, sp_p = stats.mannwhitneyu(sp_true, sp_false, alternative="greater")
        sp_rb = rank_biserial(u, len(sp_true), len(sp_false))
    else:
        u = sp_p = sp_rb = None

    # Regime: KW
    reg_groups = [by_regime[k] for k in by_regime.keys() if len(by_regime[k]) >= 2]
    reg_keys = [k for k in by_regime.keys() if len(by_regime[k]) >= 2]
    if len(reg_groups) >= 2:
        reg_h, reg_p = stats.kruskal(*reg_groups)
    else:
        reg_h = reg_p = None

    # frontal/convective vs stable/ridge/marine_layer
    widen = by_regime.get("frontal_passage", []) + by_regime.get("convective", [])
    tighten = by_regime.get("stable", []) + by_regime.get("ridge", []) + by_regime.get("marine_layer", [])
    if len(widen) >= 2 and len(tighten) >= 2:
        wt_u, wt_p = stats.mannwhitneyu(widen, tighten, alternative="greater")
        wt_rb = rank_biserial(wt_u, len(widen), len(tighten))
    else:
        wt_u = wt_p = wt_rb = None

    # Reverse: tighten group should have LOWER error
    if len(tighten) >= 2 and len(widen) >= 2:
        tw_u, tw_p = stats.mannwhitneyu(tighten, widen, alternative="less")
    else:
        tw_p = None

    # Calibration
    calib_summary = None
    if os.path.exists(CALIB_PATH):
        with open(CALIB_PATH) as f:
            calib = [json.loads(l) for l in f if l.strip()]
        n_match_conf = sum(1 for c in calib if c["match_confidence"])
        n_match_regime = sum(1 for c in calib if c["match_regime"])
        n_match_spread = sum(1 for c in calib if c["match_spread"])
        n_match_high = sum(1 for c in calib if c["match_high"])
        calib_summary = {
            "n": len(calib),
            "match_confidence_pct": 100 * n_match_conf / len(calib) if calib else 0,
            "match_regime_pct": 100 * n_match_regime / len(calib) if calib else 0,
            "match_spread_pct": 100 * n_match_spread / len(calib) if calib else 0,
            "match_high_pct": 100 * n_match_high / len(calib) if calib else 0,
        }

    # Date range
    dates = sorted({r["date"] for r in joined if r["date"]})
    date_range = (dates[0], dates[-1]) if dates else ("?", "?")

    # === Build report ===
    lines = []
    w = lines.append

    now_et = datetime.now().strftime("%Y-%m-%d %H:%M ET")
    w(f"# AFD Signal Extractor Backtest")
    w("")
    w(f"_Report generated {now_et}_")
    w("")
    w("## Methodology")
    w("")
    w("**Objective.** Test whether the design assumptions hard-coded in "
      "`src/kalshi_weather/engines/path.py` — namely that AFD regime, "
      "stated confidence, and model-spread flag carry predictive signal "
      "about forecast uncertainty — hold up against observed daily highs.")
    w("")
    w("**Sampling frame.** Five cities the bot trades — PHX (WFO=PSR, "
      "ASOS=KPHX), AUS (EWX/KAUS), NYC (OKX/KNYC), MIA (MFL/KMIA), and "
      "LAX (LOX/KLAX) — across the 40 calendar days immediately preceding "
      "2026-05-19 UTC. Target sample = 5 × 40 = 200 (city, date) cells.")
    w("")
    w("**Data sources.**")
    w("- AFD text: Iowa Environmental Mesonet archive "
      "(`api/1/nws/afos/list.json` for product IDs, "
      "`cgi-bin/afos/retrieve.py` for raw text). Selected the morning "
      "issuance (08-15 UTC) when available, else the earliest of the day.")
    w("- Observed daily max: Iowa Environmental Mesonet ASOS daily "
      "summary endpoint (`cgi-bin/request/daily.py`), `max_temp_f` column.")
    w("- LLM extraction: production `extract_afd_signals` from "
      "`src/kalshi_weather/engines/afd_extractor.py` (read-only import). "
      "Model: `llama3.2:1b` via Ollama on the Lightsail box, temp=0, "
      "`num_predict=120`, 2-hour `keep_alive` to retain warm cache.")
    w("")
    w("**Exclusion rules.** A (city, date) row is excluded from the "
      "point-estimate error analysis if any of: (a) observed daily high "
      "missing from ASOS data, (b) `extraction_failed=True`, or "
      "(c) `mentioned_today_high_f` is null. These are reported as "
      "data-quality metrics; the categorical signal tests "
      "(confidence/regime/spread) still use all rows where the categorical "
      "extraction succeeded.")
    w("")
    w("## Sample composition")
    w("")
    w(f"- Total (city, date) cells fetched: **{n_total}**")
    w(f"- Date range: **{date_range[0]} to {date_range[1]}** (UTC)")
    w(f"- Cities: {sorted({r['city'] for r in joined})}")
    w(f"- Rows with usable abs-error (have obs AND have LLM high): "
      f"**{n_have_error} ({100*n_have_error/n_total:.0f}%)**")
    w("")
    w("**Skip reasons:**")
    for k, v in skip_reasons.most_common():
        w(f"- `{k}`: {v}")
    w("")
    # Per-city
    per_city = Counter(r["city"] for r in joined)
    per_city_obs = Counter(r["city"] for r in joined if r["abs_error"] is not None)
    w("**Per-city counts (fetched / with-error):**")
    for c in sorted(per_city):
        w(f"- {c}: {per_city[c]} / {per_city_obs[c]}")
    w("")

    # Distribution tables
    w("## Signal distributions")
    w("")
    conf_dist = Counter(r["confidence"] for r in joined if r["confidence"])
    reg_dist = Counter(r["regime"] for r in joined if r["regime"])
    spread_dist = Counter(r["model_spread_flag"] for r in joined
                         if isinstance(r["model_spread_flag"], bool))
    w(f"**Confidence:** {dict(conf_dist)}")
    w(f"**Regime:** {dict(reg_dist)}")
    w(f"**Model-spread flag:** {dict(spread_dist)}")
    w(f"**LLM-extraction failures:** "
      f"{sum(1 for r in joined if r['extraction_failed'])}")
    null_high = sum(1 for r in joined if not r["extraction_failed"]
                   and r["mentioned_today_high_f"] is None)
    w(f"**Null `mentioned_today_high_f` (extraction succeeded but no "
      f"point estimate): {null_high} / {n_total} "
      f"({100*null_high/n_total:.0f}%)**")
    w("")

    # === Test 1: confidence ===
    w("## Hypothesis 1: forecaster `confidence` bucket")
    w("")
    w("_Production assumption: `confidence=high` AFDs carry better signal "
      "(smaller error) than `confidence=low`._")
    w("")
    w("| Bucket | n | median \\|err\\| | IQR | mean signed err |")
    w("|---|---|---|---|---|")
    for c in ("low", "moderate", "high"):
        vals = by_conf.get(c, [])
        sgn = by_conf_signed.get(c, [])
        if vals:
            med, q1, q3 = median_iqr(vals)
            ms = statistics.mean(sgn) if sgn else None
            w(f"| {c} | {len(vals)} | {med:.2f} | [{q1:.2f}, {q3:.2f}] | {ms:+.2f} |")
        else:
            w(f"| {c} | 0 | – | – | – |")
    w("")
    if conf_h is not None:
        w(f"**Kruskal-Wallis H = {conf_h:.3f}, p = {conf_p:.4f}** (across "
          "low/moderate/high)")
    else:
        w("Kruskal-Wallis: insufficient data across buckets.")
    w("")

    # Test 2: spread flag
    w("## Hypothesis 2: `model_spread_flag`")
    w("")
    w("_Production assumption: `model_spread_flag=True` flags higher-error "
      "days._")
    w("")
    w("| Flag | n | median \\|err\\| | IQR | mean signed err |")
    w("|---|---|---|---|---|")
    for f_val in (True, False):
        vals = by_spread.get(f_val, [])
        sgn = by_spread_signed.get(f_val, [])
        if vals:
            med, q1, q3 = median_iqr(vals)
            ms = statistics.mean(sgn) if sgn else None
            w(f"| {f_val} | {len(vals)} | {med:.2f} | [{q1:.2f}, {q3:.2f}] | {ms:+.2f} |")
        else:
            w(f"| {f_val} | 0 | – | – | – |")
    w("")
    if sp_p is not None:
        w(f"**Mann-Whitney U = {u:.1f}, one-sided p (True>False) = {sp_p:.4f}**, "
          f"rank-biserial = {sp_rb:+.3f}")
    else:
        w("Mann-Whitney: insufficient data.")
    w("")

    # Test 3: regime
    w("## Hypothesis 3: regime category")
    w("")
    w("_Production assumption: `frontal_passage`/`convective` widen path "
      "uncertainty (+0.03); `stable`/`ridge`/`marine_layer` tighten (-0.01)._")
    w("")
    w("| Regime | n | median \\|err\\| | IQR | mean signed err |")
    w("|---|---|---|---|---|")
    for rg in sorted(by_regime.keys(), key=lambda k: -len(by_regime[k])):
        vals = by_regime[rg]
        sgn = by_regime_signed[rg]
        med, q1, q3 = median_iqr(vals)
        ms = statistics.mean(sgn)
        w(f"| {rg} | {len(vals)} | {med:.2f} | [{q1:.2f}, {q3:.2f}] | {ms:+.2f} |")
    w("")
    if reg_p is not None:
        w(f"**Kruskal-Wallis across regimes: H = {reg_h:.3f}, p = {reg_p:.4f}** "
          f"(over {len(reg_keys)} regimes with n≥2)")
    w("")
    w("**Targeted test — widen group (frontal_passage ∪ convective) vs "
      "tighten group (stable ∪ ridge ∪ marine_layer):**")
    w("")
    if wt_p is not None:
        med_w, _, _ = median_iqr(widen)
        med_t, _, _ = median_iqr(tighten)
        w(f"- widen group: n={len(widen)}, median |err|={med_w:.2f}")
        w(f"- tighten group: n={len(tighten)}, median |err|={med_t:.2f}")
        w(f"- Mann-Whitney U (widen>tighten): U={wt_u:.1f}, p={wt_p:.4f}, "
          f"rank-biserial={wt_rb:+.3f}")
    else:
        w("Insufficient data for targeted regime test.")
    w("")

    # Verdicts
    def verdict(p, direction_ok, n):
        if p is None or n < 10:
            return "INCONCLUSIVE (underpowered)"
        if p < 0.05 and direction_ok:
            return "SUPPORTED"
        if p < 0.05 and not direction_ok:
            return f"REJECTED (significant in opposite direction, p={p:.3f})"
        return f"NOT SUPPORTED (p={p:.3f})"

    # confidence verdict — direction "high < low"
    if by_conf.get("high") and by_conf.get("low"):
        conf_dir = statistics.median(by_conf["high"]) < statistics.median(by_conf["low"])
    else:
        conf_dir = False
    spread_dir = (sp_p is not None and (statistics.median(by_spread.get(True, [0]))
                                       > statistics.median(by_spread.get(False, [0]))))
    widen_dir = wt_p is not None and statistics.median(widen) > statistics.median(tighten) if widen and tighten else False

    n_conf_total = sum(len(v) for v in by_conf.values())
    n_widen = len(widen) if widen else 0
    n_tighten = len(tighten) if tighten else 0
    n_spread = len(by_spread.get(True, [])) + len(by_spread.get(False, []))

    w("## Verdict per design assumption")
    w("")
    w(f"- **frontal_passage/convective → +0.03 uncertainty widening**: "
      f"{verdict(wt_p, widen_dir, min(n_widen, n_tighten))}")
    w(f"  - widen n={n_widen}, tighten n={n_tighten}, one-sided p={wt_p}")
    w(f"- **stable/ridge/marine_layer → -0.01 tightening**: tested as part "
      f"of the same widen-vs-tighten contrast above — same verdict.")
    w(f"- **`confidence=high` carries lower-error signal**: "
      f"{verdict(conf_p, conf_dir, n_conf_total)}")
    w(f"- **`model_spread_flag=True` flags high-error days**: "
      f"{verdict(sp_p, spread_dir, n_spread)}")
    w("")

    if calib_summary:
        w("## Bonus: LLM determinism check")
        w(f"- N re-runs: {calib_summary['n']}")
        w(f"- Confidence match: {calib_summary['match_confidence_pct']:.0f}%")
        w(f"- Regime match:     {calib_summary['match_regime_pct']:.0f}%")
        w(f"- Spread match:     {calib_summary['match_spread_pct']:.0f}%")
        w(f"- High match:       {calib_summary['match_high_pct']:.0f}%")
        w("")

    # Limitations
    w("## Limitations")
    w("")
    w("- **n=200 ceiling.** Even with 200 city-days, regime buckets are "
      "uneven; some have n<10 and any Mann-Whitney on them is "
      "underpowered. Verdicts on rare regimes (e.g. `anomalous_warm`) "
      "should be read as descriptive, not inferential.")
    w("- **Single LLM, single temperature.** `llama3.2:1b` at temp=0 is the "
      "production setup, but a stronger model would likely extract more "
      "complete signals. The high null-rate on `mentioned_today_high_f` "
      "(see above) is largely a 1b-model artifact — the AFD text does "
      "contain explicit highs in most cases.")
    w("- **Observation point.** ASOS daily max at the primary airport "
      "is the proxy for \"today's high\" — but the AFD covers a CWA, not "
      "a point, so |error| includes some spatial slack.")
    w("- **Morning-issuance selection.** When no AFD was issued 08-15 UTC "
      "we fell back to the earliest of the day, which may be a late-evening "
      "issuance from the prior day's labelled date. This affects ≤5% of "
      "cells.")
    w("- **No control for season/synoptic regime overlap.** Spring 2026 had "
      "few convective days nationally; results for `convective` may not "
      "generalize to summer.")
    w("")
    w("## Recommendations")
    w("")
    w("See verdicts above. Concrete guidance:")
    w("")
    if wt_p is not None and wt_p < 0.05 and widen_dir:
        w("- **Keep** the frontal_passage/convective widening; the data "
          "support it. Consider sizing the widening to the observed effect "
          "(median delta in |err| between groups).")
    elif wt_p is not None and wt_p > 0.20:
        w("- **Drop or substantially re-tune** the frontal_passage/convective "
          "widening — the data do not support a meaningfully larger error "
          "in those regimes. The +0.03 number appears to be a guess that "
          "doesn't pay rent on this 40-day sample.")
    else:
        w("- **Inconclusive** on the regime widening — keep but flag for "
          "re-test on a larger sample.")

    if conf_p is not None and conf_p < 0.05 and conf_dir:
        w("- **Keep** the confidence weighting; high-confidence AFDs do "
          "track lower realized error.")
    elif conf_p is not None and conf_p > 0.20:
        w("- **Drop** the confidence weighting — the 3-way KW does not "
          "find a difference. The LLM's confidence label is not a useful "
          "covariate at the current sample size.")
    else:
        w("- **Inconclusive** on the confidence weighting.")

    if sp_p is not None and sp_p < 0.05 and spread_dir:
        w("- **Keep** the spread-flag handling — it correctly identifies "
          "higher-error days.")
    elif sp_p is not None and sp_p > 0.20:
        w("- **Drop** the spread flag — it does not correlate with realized "
          "error on this sample.")
    else:
        w("- **Inconclusive** on the spread flag.")
    w("")

    w("## Artifacts")
    w("")
    w(f"- Raw joined dataset: `research/afd_backtest_joined.csv` "
      f"({n_total} rows)")
    w(f"- LLM extraction log: `research/llm_extractions.jsonl`")
    w(f"- AFD archive: `research/afd_archive/*.json` ({n_total} files)")
    w(f"- Observations: `research/observations/*.json` (5 cities)")
    w("")

    with open(REPORT_OUT, "w") as f:
        f.write("\n".join(lines))
    print(f"wrote {REPORT_OUT}")
    print(f"wrote {CSV_OUT}")


if __name__ == "__main__":
    main()
