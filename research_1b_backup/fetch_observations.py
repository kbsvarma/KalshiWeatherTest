"""Fetch observed daily max temps (°F) for the 5 cities' ASOS stations.

Uses Iowa State Mesonet daily.py endpoint. One JSON file per city to
research/observations/{city}.json with {date: max_tmpf} mapping.
"""
from __future__ import annotations

import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone

# (city, network, station)
STATIONS = [
    ("PHX", "AZ_ASOS", "PHX"),
    ("AUS", "TX_ASOS", "AUS"),
    ("NYC", "NY_ASOS", "NYC"),
    ("MIA", "FL_ASOS", "MIA"),
    ("LAX", "CA_ASOS", "LAX"),
]

OBS_DIR = os.path.join(os.path.dirname(__file__), "observations")
os.makedirs(OBS_DIR, exist_ok=True)


def main():
    today = datetime(2026, 5, 19, tzinfo=timezone.utc).date()
    start = today - timedelta(days=45)  # bit of buffer
    end = today

    for city, network, station in STATIONS:
        url = (
            f"https://mesonet.agron.iastate.edu/cgi-bin/request/daily.py?"
            f"network={network}&station={station}"
            f"&year1={start.year}&month1={start.month}&day1={start.day}"
            f"&year2={end.year}&month2={end.month}&day2={end.day}"
            f"&format=json"
        )
        print(f"{city} {network}/{station} ...")
        req = urllib.request.Request(url, headers={"User-Agent": "KalshiWeatherTest-research/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        if isinstance(data, list):
            rows = data
        else:
            rows = data.get("data", [])
        out = {}
        for r in rows:
            d = r.get("day")
            mx = r.get("max_temp_f", r.get("max_tmpf"))
            if d and mx is not None:
                # Normalize 'YYYY-MM-DDT00:00:00.000' -> 'YYYY-MM-DD'
                out[d[:10]] = mx
        with open(os.path.join(OBS_DIR, f"{city}.json"), "w") as f:
            json.dump(out, f)
        print(f"  saved {len(out)} days, sample={list(out.items())[:2]}")


if __name__ == "__main__":
    main()
