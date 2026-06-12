#!/usr/bin/env python3
"""Join LLM extractions with observed highs and produce stats + report."""
import csv
import json
import math
import os
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
JSONL = ROOT / "llm_extractions.jsonl"
OBS_DIR = ROOT / "observations"
CSV_OUT = ROOT / "afd_backtest_joined.csv"
REPORT_OUT = ROOT / "afd_backtest_report.md"

CITIES = ["AUS", "LAX", "MIA", "NYC", "PHX"]


def load_observations():
    obs = {}
    for c in CITIES:
        p = OBS_DIR / f"{c}.json"
        with open(p) as f:
            obs[c] = json.load(f)
    return obs


def load_extractions():
    rows = []
    with open(JSONL) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


def join(extractions, obs):
    joined = []
    for r in extractions:
        city, date = r["city"], r["date"]
        observed = obs.get(city, {}).get(date)
        mhigh = r.get("mentioned_today_high_f")
        signed = mhigh - observed if (mhigh is not None and observed is not None) else None
        abs_err = abs(signed) if signed is not None else None
        raw = r.get("raw_llm_response") or ""
        joined.append({
            "city": city,
            "date": date,
            "wfo": r.get("wfo"),
            "product_id": r.get("product_id"),
            "confidence": r.get("confidence"),
            "model_spread_flag": r.get("model_spread_flag"),
            "regime": r.get("regime"),
            "mentioned_today_high_f": mhigh,
            "extraction_failed": r.get("extraction_failed"),
            "observed_high": observed,
            "signed_error": signed,
            "abs_error": abs_err,
            "raw_llm_response_first_120chars": raw[:120],
        })
    return joined


def write_csv(joined):
    cols = ["city", "date", "wfo", "product_id", "confidence", "model_spread_flag",
            "regime", "mentioned_today_high_f", "extraction_failed", "observed_high",
            "signed_error", "abs_error", "raw_llm_response_first_120chars"]
    with open(CSV_OUT, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in joined:
            w.writerow(r)


# ---------- statistics ----------

def median_iqr(vals):
    vals = sorted(vals)
    n = len(vals)
    if n == 0:
        return None, None, None
    med = statistics.median(vals)
    # quartiles via standard linear interpolation
    def quantile(p):
        if n == 1:
            return vals[0]
        idx = p * (n - 1)
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return vals[lo]
        return vals[lo] + (idx - lo) * (vals[hi] - vals[lo])
    q1 = quantile(0.25)
    q3 = quantile(0.75)
    return med, q1, q3


def rankdata(vals):
    """Average ranks for ties."""
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def mannwhitney_u(a, b):
    """Two-sided Mann-Whitney U with normal approximation. Returns U, p, rank-biserial."""
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return None, None, None
    combined = a + b
    ranks = rankdata(combined)
    R1 = sum(ranks[:n1])
    U1 = R1 - n1 * (n1 + 1) / 2.0
    U2 = n1 * n2 - U1
    U = min(U1, U2)
    # rank-biserial
    rb = 1 - (2 * U) / (n1 * n2)
    # tie correction
    tie_counts = Counter(combined)
    T = sum(t * t * t - t for t in tie_counts.values())
    N = n1 + n2
    mu = n1 * n2 / 2.0
    sigma_sq = n1 * n2 / 12.0 * ((N + 1) - T / (N * (N - 1))) if N > 1 else 0
    if sigma_sq <= 0:
        return U, None, rb
    z = (U1 - mu) / math.sqrt(sigma_sq)
    p = 2 * (1 - phi(abs(z)))
    return U, p, rb


def mannwhitney_u_onesided(a, b, alt="greater"):
    """One-sided MWU testing H1: a stochastically > b (or less). Returns U1, p."""
    n1, n2 = len(a), len(b)
    if n1 == 0 or n2 == 0:
        return None, None
    combined = a + b
    ranks = rankdata(combined)
    R1 = sum(ranks[:n1])
    U1 = R1 - n1 * (n1 + 1) / 2.0
    tie_counts = Counter(combined)
    T = sum(t * t * t - t for t in tie_counts.values())
    N = n1 + n2
    mu = n1 * n2 / 2.0
    sigma_sq = n1 * n2 / 12.0 * ((N + 1) - T / (N * (N - 1))) if N > 1 else 0
    if sigma_sq <= 0:
        return U1, None
    z = (U1 - mu) / math.sqrt(sigma_sq)
    if alt == "greater":
        p = 1 - phi(z)
    else:
        p = phi(z)
    return U1, p


def kruskal_wallis(*groups):
    groups = [g for g in groups if len(g) > 0]
    if len(groups) < 2:
        return None, None
    combined = []
    for g in groups:
        combined.extend(g)
    ranks = rankdata(combined)
    N = len(combined)
    idx = 0
    H_num = 0.0
    for g in groups:
        n = len(g)
        R = sum(ranks[idx:idx + n])
        H_num += (R * R) / n
        idx += n
    H = (12.0 / (N * (N + 1))) * H_num - 3 * (N + 1)
    # tie correction
    tie_counts = Counter(combined)
    T = sum(t * t * t - t for t in tie_counts.values())
    C = 1 - T / (N * N * N - N) if (N * N * N - N) > 0 else 1
    if C > 0:
        H = H / C
    df = len(groups) - 1
    p = 1 - chi2_cdf(H, df)
    return H, p


def phi(z):
    """Standard normal CDF."""
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


def chi2_cdf(x, k):
    """Chi-squared CDF via regularized lower incomplete gamma."""
    if x <= 0:
        return 0.0
    return gammainc(k / 2.0, x / 2.0)


def gammainc(a, x):
    """Regularized lower incomplete gamma P(a, x)."""
    if x < 0 or a <= 0:
        return 0.0
    if x == 0:
        return 0.0
    if x < a + 1:
        # series
        term = 1.0 / a
        total = term
        for n in range(1, 200):
            term *= x / (a + n)
            total += term
            if abs(term) < 1e-12 * abs(total):
                break
        return total * math.exp(-x + a * math.log(x) - math.lgamma(a))
    else:
        # continued fraction
        b = x + 1 - a
        c = 1e30
        d = 1.0 / b
        h = d
        for i in range(1, 200):
            an = -i * (i - a)
            b += 2
            d = an * d + b
            if abs(d) < 1e-30:
                d = 1e-30
            c = b + an / c
            if abs(c) < 1e-30:
                c = 1e-30
            d = 1.0 / d
            delta = d * c
            h *= delta
            if abs(delta - 1.0) < 1e-12:
                break
        Q = math.exp(-x + a * math.log(x) - math.lgamma(a)) * h
        return 1 - Q


# ---------- analysis driver ----------

def analyze(joined):
    out = {}
    # exclusions
    used = [r for r in joined if r["abs_error"] is not None]
    excluded = [r for r in joined if r["abs_error"] is None]
    out["n_total"] = len(joined)
    out["n_used"] = len(used)
    out["n_excluded"] = len(excluded)
    out["excl_reason"] = Counter(
        ("no_mentioned_high" if r["mentioned_today_high_f"] is None else "no_observed")
        + ("|fail" if r.get("extraction_failed") else "")
        for r in excluded
    )

    # per-city composition
    out["per_city_total"] = Counter(r["city"] for r in joined)
    out["per_city_used"] = Counter(r["city"] for r in used)

    # health check
    out["confidence_counts"] = Counter(r["confidence"] for r in joined)
    out["regime_counts"] = Counter(r["regime"] for r in joined)
    out["spread_counts"] = Counter(r["model_spread_flag"] for r in joined)
    out["mhigh_null_rate"] = sum(1 for r in joined if r["mentioned_today_high_f"] is None) / max(len(joined), 1)
    out["extract_fail_rate"] = sum(1 for r in joined if r.get("extraction_failed")) / max(len(joined), 1)

    # bias overall
    signed = [r["signed_error"] for r in used]
    out["bias_overall_mean"] = statistics.mean(signed) if signed else None
    out["bias_overall_median"] = statistics.median(signed) if signed else None
    out["abs_err_median"], out["abs_err_q1"], out["abs_err_q3"] = median_iqr([r["abs_error"] for r in used])

    # by confidence
    by_conf = defaultdict(list)
    by_conf_signed = defaultdict(list)
    for r in used:
        by_conf[r["confidence"]].append(r["abs_error"])
        by_conf_signed[r["confidence"]].append(r["signed_error"])
    out["by_confidence"] = {
        k: {
            "n": len(v),
            "median": median_iqr(v)[0],
            "q1": median_iqr(v)[1],
            "q3": median_iqr(v)[2],
            "mean_signed": statistics.mean(by_conf_signed[k]) if by_conf_signed[k] else None,
        }
        for k, v in by_conf.items()
    }
    eligible_conf = [v for k, v in by_conf.items() if len(v) >= 10]
    if len(eligible_conf) >= 2:
        H, p = kruskal_wallis(*eligible_conf)
        out["confidence_kw_H"] = H
        out["confidence_kw_p"] = p
    else:
        out["confidence_kw_H"] = None
        out["confidence_kw_p"] = None

    # by spread flag
    by_spread = defaultdict(list)
    for r in used:
        by_spread[r["model_spread_flag"]].append(r["abs_error"])
    out["by_spread"] = {
        str(k): {"n": len(v), "median": median_iqr(v)[0], "q1": median_iqr(v)[1], "q3": median_iqr(v)[2]}
        for k, v in by_spread.items()
    }
    a = by_spread.get(True, [])
    b = by_spread.get(False, [])
    if len(a) >= 10 and len(b) >= 10:
        U, p, rb = mannwhitney_u(a, b)
        out["spread_mwu_U"] = U
        out["spread_mwu_p"] = p
        out["spread_rb"] = rb
    else:
        out["spread_mwu_U"] = None
        out["spread_mwu_p"] = None
        out["spread_rb"] = None

    # by regime
    by_regime = defaultdict(list)
    for r in used:
        by_regime[r["regime"]].append(r["abs_error"])
    out["by_regime"] = {
        k: {"n": len(v), "median": median_iqr(v)[0], "q1": median_iqr(v)[1], "q3": median_iqr(v)[2]}
        for k, v in by_regime.items()
    }
    eligible_reg = [v for v in by_regime.values() if len(v) >= 5]
    if len(eligible_reg) >= 2:
        H, p = kruskal_wallis(*eligible_reg)
        out["regime_kw_H"] = H
        out["regime_kw_p"] = p
    else:
        out["regime_kw_H"] = None
        out["regime_kw_p"] = None

    # path.py assumption: frontal_passage/convective > stable/ridge/marine_layer in abs_error
    high_unc = by_regime.get("frontal_passage", []) + by_regime.get("convective", [])
    low_unc = by_regime.get("stable", []) + by_regime.get("ridge", []) + by_regime.get("marine_layer", [])
    if len(high_unc) >= 5 and len(low_unc) >= 5:
        U1, p_one = mannwhitney_u_onesided(high_unc, low_unc, alt="greater")
        out["path_mwu_U"] = U1
        out["path_mwu_p"] = p_one
        out["path_n_high"] = len(high_unc)
        out["path_n_low"] = len(low_unc)
        out["path_med_high"] = median_iqr(high_unc)[0]
        out["path_med_low"] = median_iqr(low_unc)[0]
    else:
        out["path_mwu_U"] = None
        out["path_mwu_p"] = None
        out["path_n_high"] = len(high_unc)
        out["path_n_low"] = len(low_unc)
        out["path_med_high"] = median_iqr(high_unc)[0] if high_unc else None
        out["path_med_low"] = median_iqr(low_unc)[0] if low_unc else None

    return out, used


def verdict_signal_health(stats):
    flags = []
    if len(stats["confidence_counts"]) <= 1:
        flags.append("confidence")
    if len(stats["regime_counts"]) <= 1:
        flags.append("regime")
    if len(stats["spread_counts"]) <= 1:
        flags.append("model_spread_flag")
    return flags


def fmt(x, n=2):
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.{n}f}"
    return str(x)


def write_report(stats, joined):
    et_now = datetime.utcnow()
    # Convert UTC → ET (EDT = UTC-4 in May)
    from datetime import timedelta
    et = et_now - timedelta(hours=4)
    et_str = et.strftime("%Y-%m-%d %H:%M ET")

    degenerate = verdict_signal_health(stats)
    lines = []
    lines.append("# AFD LLM Extraction Backtest Report")
    lines.append(f"_Generated: {et_str}_\n")
    lines.append("## Sample frame")
    lines.append("- 5 cities (AUS, LAX, MIA, NYC, PHX) x 40 days (2026-04-09 through 2026-05-18)")
    lines.append(f"- LLM model: llama3.2:1b on Lightsail (ollama)")
    lines.append(f"- Total rows: {stats['n_total']}")
    lines.append(f"- Used in error stats: {stats['n_used']}")
    lines.append(f"- Excluded: {stats['n_excluded']}  (reasons: {dict(stats['excl_reason'])})\n")

    lines.append("## Methodology")
    lines.append("For each archived AFD, the production extractor (`extract_afd_signals`) was invoked through `llama3.2:1b` to produce {confidence, regime, model_spread_flag, mentioned_today_high_f}. Observed daily highs (from `research/observations/`) were joined by (city,date). `signed_error = mentioned_today_high_f − observed_high`; `abs_error = |signed_error|`. Statistical tests use Mann-Whitney U (two-sided unless noted) and Kruskal-Wallis with tie correction; effect size for MWU reported as rank-biserial. Minimum bucket size n=10 for inference (n=5 for regime where 5 buckets compete).\n")

    lines.append("## Sample composition")
    lines.append("| city | total | used |")
    lines.append("|---|---|---|")
    for c in CITIES:
        lines.append(f"| {c} | {stats['per_city_total'].get(c, 0)} | {stats['per_city_used'].get(c, 0)} |")
    lines.append("")

    lines.append("## Signal-distribution health check")
    if degenerate:
        lines.append(f"**DEGENERATE OUTPUTS DETECTED on:** {', '.join(degenerate)} — the LLM emitted a constant value across 200 inputs. Downstream tests on these signals are meaningless.\n")
    else:
        lines.append("All signal axes show at least 2 distinct values.\n")
    lines.append(f"- confidence counts: {dict(stats['confidence_counts'])}")
    lines.append(f"- regime counts: {dict(stats['regime_counts'])}")
    lines.append(f"- model_spread_flag counts: {dict(stats['spread_counts'])}")
    lines.append(f"- mentioned_today_high_f null rate: {stats['mhigh_null_rate']:.1%}")
    lines.append(f"- extraction_failed rate: {stats['extract_fail_rate']:.1%}\n")

    lines.append("## Overall error and bias")
    lines.append(f"- abs_error median (IQR): {fmt(stats['abs_err_median'])} ({fmt(stats['abs_err_q1'])}, {fmt(stats['abs_err_q3'])})")
    lines.append(f"- signed_error mean: {fmt(stats['bias_overall_mean'])}")
    lines.append(f"- signed_error median: {fmt(stats['bias_overall_median'])}\n")

    lines.append("## Design-assumption verdicts")

    # frontal_passage/convective +0.03
    lines.append("### frontal_passage/convective → +0.03 uncertainty")
    if "frontal_passage" in stats["regime_counts"] or "convective" in stats["regime_counts"]:
        lines.append(f"n high-uncertainty regimes: {stats['path_n_high']}; n low-uncertainty: {stats['path_n_low']}")
        lines.append(f"median abs_error high vs low: {fmt(stats['path_med_high'])} vs {fmt(stats['path_med_low'])}")
        lines.append(f"one-sided MWU p (high > low): {fmt(stats['path_mwu_p'], 4)}")
        if stats["path_mwu_p"] is None:
            v = "INCONCLUSIVE (insufficient samples in one bucket)"
        elif stats["path_mwu_p"] < 0.05 and (stats["path_med_high"] or 0) > (stats["path_med_low"] or 0):
            v = "SUPPORTED"
        else:
            v = "NOT SUPPORTED"
        lines.append(f"**Verdict: {v}**\n")
    else:
        lines.append("**Verdict: INCONCLUSIVE — neither frontal_passage nor convective was ever emitted by the model.**\n")

    # stable/ridge/marine_layer -0.01
    lines.append("### stable/ridge/marine_layer → −0.01 uncertainty")
    low_regimes = {k: v for k, v in stats["regime_counts"].items() if k in ("stable", "ridge", "marine_layer")}
    if not low_regimes:
        lines.append("**Verdict: INCONCLUSIVE — no low-uncertainty regime tags emitted.**\n")
    elif stats["path_mwu_p"] is None:
        lines.append("**Verdict: INCONCLUSIVE — paired test against high-uncertainty regimes was infeasible.**\n")
    else:
        lines.append(f"low-uncertainty regimes present: {low_regimes}")
        lines.append(f"median abs_error low vs high: {fmt(stats['path_med_low'])} vs {fmt(stats['path_med_high'])}")
        # Same one-sided MWU answers both halves of the path assumption
        if stats["path_mwu_p"] < 0.05 and (stats["path_med_low"] or 0) < (stats["path_med_high"] or 0):
            v = "SUPPORTED"
        elif len(low_regimes) == 1 and "marine_layer" in low_regimes:
            v = "NOT SUPPORTED (regime axis collapsed — see health check)"
        else:
            v = "NOT SUPPORTED"
        lines.append(f"**Verdict: {v}**\n")

    # confidence
    lines.append("### confidence=high carries signal")
    lines.append("| confidence | n | median abs_err | (Q1, Q3) | mean signed |")
    lines.append("|---|---|---|---|---|")
    for k, v in stats["by_confidence"].items():
        lines.append(f"| {k} | {v['n']} | {fmt(v['median'])} | ({fmt(v['q1'])}, {fmt(v['q3'])}) | {fmt(v['mean_signed'])} |")
    if stats["confidence_kw_p"] is None:
        lines.append(f"**Verdict: INCONCLUSIVE — only {len([k for k,v in stats['by_confidence'].items() if v['n']>=10])} confidence bucket(s) reached n=10.**\n")
    else:
        lines.append(f"Kruskal-Wallis H={fmt(stats['confidence_kw_H'])}, p={fmt(stats['confidence_kw_p'], 4)}")
        v = "SUPPORTED" if stats["confidence_kw_p"] < 0.05 else "NOT SUPPORTED"
        lines.append(f"**Verdict: {v}**\n")

    # spread
    lines.append("### model_spread_flag=true flags high-error days")
    lines.append("| spread_flag | n | median | (Q1, Q3) |")
    lines.append("|---|---|---|---|")
    for k, v in stats["by_spread"].items():
        lines.append(f"| {k} | {v['n']} | {fmt(v['median'])} | ({fmt(v['q1'])}, {fmt(v['q3'])}) |")
    if stats["spread_mwu_p"] is None:
        lines.append("**Verdict: INCONCLUSIVE — only one value of model_spread_flag observed (or n<10).**\n")
    else:
        lines.append(f"MWU U={fmt(stats['spread_mwu_U'])}, p={fmt(stats['spread_mwu_p'], 4)}, rank-biserial={fmt(stats['spread_rb'], 3)}")
        v = "SUPPORTED" if stats["spread_mwu_p"] < 0.05 else "NOT SUPPORTED"
        lines.append(f"**Verdict: {v}**\n")

    # regime omnibus
    lines.append("### Regime axis omnibus")
    lines.append("| regime | n | median | (Q1, Q3) |")
    lines.append("|---|---|---|---|")
    for k, v in stats["by_regime"].items():
        lines.append(f"| {k} | {v['n']} | {fmt(v['median'])} | ({fmt(v['q1'])}, {fmt(v['q3'])}) |")
    if stats["regime_kw_p"] is None:
        lines.append("Kruskal-Wallis: not enough buckets with n≥5.\n")
    else:
        lines.append(f"Kruskal-Wallis H={fmt(stats['regime_kw_H'])}, p={fmt(stats['regime_kw_p'], 4)}\n")

    lines.append("## Limitations")
    lines.append("- Single tiny model (llama3.2:1b). Larger models likely behave differently. Results are about *this deployed extractor*, not about whether AFD text contains signal in principle.")
    lines.append("- 200 samples across 5 cities x 40 days; one season (April-May 2026).")
    lines.append("- mentioned_today_high_f null/missing rate driven by extractor parsing, not absence in source text.")
    lines.append("- Observed highs are daily maxima from one station per city; not the Kalshi-settled value.")
    lines.append("- No multiple-comparison correction (4 hypotheses).\n")

    lines.append("## Recommendations")
    if "confidence" in degenerate:
        lines.append("- **DROP the confidence knob** in path.py — extractor emits a constant value; no signal possible.")
    if "regime" in degenerate:
        lines.append("- **DROP the regime knob** in path.py — extractor emits a constant value; the +0.03/-0.01 adjustments are noise.")
    if "model_spread_flag" in degenerate:
        lines.append("- **DROP the model_spread_flag gate** — extractor emits a constant value.")
    if not degenerate:
        lines.append("- Re-tune knobs only where verdict was SUPPORTED; drop where NOT SUPPORTED.")
    lines.append("- Re-run with a larger model (e.g. llama3.1:8b or qwen2.5:7b) before keeping any AFD-LLM logic in production.")
    lines.append("- Consider replacing the LLM extractor with a deterministic regex layer for `mentioned_today_high_f`.\n")

    lines.append("## Raw stats appendix")
    lines.append("```json")
    # serialize Counter as dict
    serial = {}
    for k, v in stats.items():
        if isinstance(v, Counter):
            serial[k] = dict(v)
        else:
            serial[k] = v
    lines.append(json.dumps(serial, indent=2, default=str))
    lines.append("```")

    with open(REPORT_OUT, "w") as f:
        f.write("\n".join(lines))


def main():
    obs = load_observations()
    extractions = load_extractions()
    joined = join(extractions, obs)
    write_csv(joined)
    stats, used = analyze(joined)
    write_report(stats, joined)
    print(f"Wrote {CSV_OUT}")
    print(f"Wrote {REPORT_OUT}")
    print(f"n_total={stats['n_total']} n_used={stats['n_used']}")
    print(f"degenerate axes: {verdict_signal_health(stats)}")


if __name__ == "__main__":
    main()
