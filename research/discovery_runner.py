"""Run discovery_extractor over all (city, date) cells in afd_archive.

Outputs JSONL with one row per cell. Idempotent — skips already-done cells.
Designed to be killed and restarted safely (each row written atomically).
"""
from __future__ import annotations

import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVE_DIR = os.path.join(ROOT, "research", "afd_archive")
OUT_PATH = os.path.join(ROOT, "research", "discovery_features.jsonl")

sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "research"))
from discovery_extractor import extract_discovery_features  # noqa: E402


def load_done() -> set[tuple[str, str]]:
    done: set[tuple[str, str]] = set()
    if not os.path.exists(OUT_PATH):
        return done
    with open(OUT_PATH) as f:
        for line in f:
            try:
                d = json.loads(line)
                done.add((d["city"], d["date"]))
            except Exception:
                pass
    return done


def main() -> None:
    files = sorted(os.listdir(ARCHIVE_DIR))
    files = [f for f in files if f.endswith(".json")]
    done = load_done()
    print(f"start: {len(files)} entries, {len(done)} already done", flush=True)

    t0 = time.time()
    for i, fn in enumerate(files):
        # fn pattern: CITY_YYYY-MM-DD.json
        base = fn[:-5]
        try:
            city, date_s = base.split("_", 1)
        except ValueError:
            continue
        key = (city, date_s)
        if key in done:
            continue
        try:
            with open(os.path.join(ARCHIVE_DIR, fn), encoding="utf-8", errors="replace") as f:
                blob = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            print(f"[SKIP] {fn}: decode error {exc}", flush=True)
            continue
        afd_text = blob.get("raw_text", "")
        wfo = blob.get("wfo", "")
        product_id = blob.get("product_id", "")
        issuance = blob.get("issuance_time", "")

        cell_t0 = time.time()
        r = extract_discovery_features(afd_text)
        cell_dt = time.time() - cell_t0

        row = {
            "city": city,
            "date": date_s,
            "wfo": wfo,
            "product_id": product_id,
            "issuance_time": issuance,
            "extraction_seconds": round(cell_dt, 2),
            # Features
            "high_point_f": r.high_point_f,
            "high_range_low_f": r.high_range_low_f,
            "high_range_high_f": r.high_range_high_f,
            "confidence_word": r.confidence_word,
            "model_disagreement_mentioned": r.model_disagreement_mentioned,
            "model_disagreement_direction": r.model_disagreement_direction,
            "forecaster_lean": r.forecaster_lean,
            "timing_critical": r.timing_critical,
            "observational_anchor": r.observational_anchor,
            "revised_from_previous": r.revised_from_previous,
            "explicit_uncertainty_phrases": list(r.explicit_uncertainty_phrases),
            "bust_risk_words": list(r.bust_risk_words),
            "synoptic_drivers": list(r.synoptic_drivers),
            "key_quote": r.key_quote,
            "extraction_failed": r.extraction_failed,
        }
        with open(OUT_PATH, "a") as f:
            f.write(json.dumps(row) + "\n")
        elapsed = time.time() - t0
        print(f"[{i+1}/{len(files)}] {city} {date_s} -> "
              f"drivers={len(r.synoptic_drivers)} high={r.high_point_f or f'{r.high_range_low_f}-{r.high_range_high_f}'} "
              f"lean={r.forecaster_lean} ({cell_dt:.1f}s, elapsed={elapsed:.0f}s)",
              flush=True)

    print(f"done. total elapsed: {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
