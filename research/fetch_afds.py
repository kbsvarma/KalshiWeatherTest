"""Fetch historical NWS AFDs via IEM archive for 5 trading cities, 40 days back.

IEM endpoints used:
- List products on a date: /api/1/nws/afos/list.json?pil=AFD{WFO}&date=YYYY-MM-DD
- Fetch product text:      /cgi-bin/afos/retrieve.py?pil=AFD{WFO}&e=YYYYMMDDHHMM

Persists one JSON per (city, date) to research/afd_archive/{city}_{date}.json.
Selects the morning issuance (08-15 UTC) when available; otherwise the
earliest issuance of the day.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

CITIES = [
    ("PHX", "PSR"),
    ("AUS", "EWX"),
    ("NYC", "OKX"),
    ("MIA", "MFL"),
    ("LAX", "LOX"),
]

ARCHIVE_DIR = os.path.join(os.path.dirname(__file__), "afd_archive")
os.makedirs(ARCHIVE_DIR, exist_ok=True)

HEADERS = {"User-Agent": "KalshiWeatherTest-research/1.0 (contact research@example.com)"}


def http_get(url: str, timeout: int = 30) -> bytes | None:
    req = urllib.request.Request(url, headers=HEADERS)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, TimeoutError) as e:
            if attempt == 2:
                print(f"  HTTP error {url}: {e}")
                return None
            time.sleep(1.5 * (attempt + 1))
    return None


def list_afds(wfo: str, date_str: str) -> list[dict]:
    url = f"https://mesonet.agron.iastate.edu/api/1/nws/afos/list.json?pil=AFD{wfo}&date={date_str}"
    raw = http_get(url)
    if raw is None:
        return []
    try:
        data = json.loads(raw)
    except Exception:
        return []
    return data.get("data", [])


def fetch_afd_text(product_id: str) -> str | None:
    # IEM's retrieve.py?e= silently ignores the timestamp and returns
    # the most recent product. The correct historical endpoint is
    # /api/1/nwstext/{product_id} which returns plain text and honors
    # the full id (YYYYMMDDHHMM-CCCC-FXUS6N-AFDWFO).
    url = f"https://mesonet.agron.iastate.edu/api/1/nwstext/{product_id}"
    raw = http_get(url)
    if raw is None:
        return None
    text = raw.decode("utf-8", errors="replace")
    if "Area Forecast Discussion" not in text and "AFD" not in text[:200]:
        return None
    return text


def main():
    today = datetime(2026, 5, 19, tzinfo=timezone.utc).date()
    days_back = 100

    total_targets = 0
    saved = 0
    skipped = 0
    already = 0

    for city, wfo in CITIES:
        print(f"\n=== {city} (WFO={wfo}) ===")
        for i in range(days_back):
            d = today - timedelta(days=i + 1)  # yesterday back, not including today
            date_str = d.strftime("%Y-%m-%d")
            total_targets += 1
            out_path = os.path.join(ARCHIVE_DIR, f"{city}_{date_str}.json")
            if os.path.exists(out_path):
                already += 1
                continue

            entries = list_afds(wfo, date_str)
            if not entries:
                skipped += 1
                continue

            # Parse timestamps and pick morning issuance
            picks = []
            for e in entries:
                pid = e.get("product_id", "")
                ts_str = pid.split("-")[0] if pid else ""
                if len(ts_str) != 12:
                    continue
                try:
                    dt = datetime.strptime(ts_str, "%Y%m%d%H%M").replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                picks.append((dt, pid))
            if not picks:
                skipped += 1
                continue
            morning = [p for p in picks if 8 <= p[0].hour <= 15]
            chosen_dt, chosen_pid = sorted(morning or picks, key=lambda p: p[0])[0]

            text = fetch_afd_text(chosen_pid)
            if not text or len(text) < 200:
                skipped += 1
                continue
            with open(out_path, "w") as f:
                json.dump({
                    "city": city,
                    "wfo": wfo,
                    "date": date_str,
                    "product_id": chosen_pid,
                    "issuance_time": chosen_dt.isoformat(),
                    "raw_text": text,
                }, f)
            saved += 1
            time.sleep(0.2)

        print(f"  city total saved so far for {city}")

    print(f"\nTotal targets: {total_targets}")
    print(f"Already had:   {already}")
    print(f"Newly saved:   {saved}")
    print(f"Skipped:       {skipped}")
    n_files = len([f for f in os.listdir(ARCHIVE_DIR) if f.endswith(".json")])
    print(f"Archive total: {n_files} files")


if __name__ == "__main__":
    main()
