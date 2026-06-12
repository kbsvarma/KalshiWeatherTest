"""Determinism / calibration check: pick 10 random AFDs that already have a
first-pass extraction, re-extract them, and record agreement.

To be run ON LIGHTSAIL after primary run completes. Writes to
research/llm_calibration.jsonl.
"""
import json, os, random, sys, time, urllib.request

PRIMARY = "/opt/kalshi-weather/research/llm_extractions.jsonl"
OUT = "/opt/kalshi-weather/research/llm_calibration.jsonl"
os.environ.setdefault("OLLAMA_MODEL", "llama3.2:1b")
os.environ.setdefault("OLLAMA_TIMEOUT_SECONDS", "120")
sys.path.insert(0, "/opt/kalshi-weather/src")
from kalshi_weather.engines.afd_extractor import extract_afd_signals  # noqa

def fetch_text(product_id):
    parts = product_id.split("-")
    ts, pil = parts[0], parts[3]
    url = f"https://mesonet.agron.iastate.edu/cgi-bin/afos/retrieve.py?pil={pil}&e={ts}"
    req = urllib.request.Request(url, headers={"User-Agent": "research/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")

with open(PRIMARY) as f:
    rows = [json.loads(l) for l in f if l.strip()]
rows = [r for r in rows if not r.get("extraction_failed")]
random.seed(42)
sample = random.sample(rows, min(10, len(rows)))

with open(OUT, "w") as out:
    for r in sample:
        text = fetch_text(r["product_id"])
        r2 = extract_afd_signals(text)
        rec = {
            "city": r["city"], "date": r["date"],
            "first_confidence": r["confidence"], "second_confidence": r2.confidence,
            "first_regime": r["regime"], "second_regime": r2.regime,
            "first_spread": r["model_spread_flag"], "second_spread": r2.model_spread_flag,
            "first_high": r["mentioned_today_high_f"], "second_high": r2.mentioned_today_high_f,
            "match_confidence": r["confidence"] == r2.confidence,
            "match_regime": r["regime"] == r2.regime,
            "match_spread": r["model_spread_flag"] == r2.model_spread_flag,
            "match_high": r["mentioned_today_high_f"] == r2.mentioned_today_high_f,
        }
        out.write(json.dumps(rec) + "\n")
        print(f"{r['city']} {r['date']}: conf {r['confidence']}->{r2.confidence}, regime {r['regime']}->{r2.regime}", flush=True)
        time.sleep(0.5)
print("calibration done")
