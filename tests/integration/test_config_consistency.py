"""Config-consistency invariants — catch drift across modules.

The volume-grinder strategy thresholds and operational limits live in several
files. Today (2026-05-18) we shipped three live-money bugs caused by these
values silently disagreeing:

  * ``max_provider_spread_f`` was 10.0 in engines/shadow.py but 8.0 in the
    survey's VolumeSelectionThresholds default — the survey reported
    rejection counts that didn't match what shadow actually blocked.

  * ``daily_capital_cap_usd`` was 10.0 in two Python defaults but $15 in
    the live wrapper's env-var — survey said "10 cap" while live used $15.

  * ``_MAX_DISTINCT_MARKETS_PER_CITY_PER_DAY`` and ``_SAME_CITY_DATE_CAP``
    were duplicated across shadow.py and decision.py. Drift between them
    yesterday left the bot unable to bet for 6 hours overnight.

These tests fail fast when the same logical value disagrees between modules,
forcing a deliberate fix-everywhere change rather than a single-file edit
that gets silently out of sync.

If you intentionally change one of these constants, update ALL the places
listed here in the same commit and update the expected values below.
"""
from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

import pytest

# Project root — tests/integration/test_X.py → 3 .parent calls
REPO_ROOT = Path(__file__).resolve().parents[2]


def test_volume_grinder_spread_cap_is_consistent_across_modules() -> None:
    """``max_provider_spread_f`` must agree across all three definitions.

    Bug surfaced 2026-05-18: shadow.py=10, volume_selection.py=8,
    recommendation_log.py=8. Survey logs reported "max_provider_spread_f=8"
    while shadow's actual gate was 10 — making rejection counts misleading.
    """
    from kalshi_weather.analytics.volume_selection import VolumeSelectionThresholds
    from kalshi_weather.analytics import recommendation_log as rec
    from kalshi_weather.engines import shadow

    shadow_val = shadow._VOLUME_GRINDER_MAX_SPREAD_F
    survey_default = VolumeSelectionThresholds().max_provider_spread_f
    rec_val = rec._MAX_SPREAD_F

    assert shadow_val == survey_default == rec_val, (
        f"max_provider_spread_f drift: "
        f"shadow.py={shadow_val}, "
        f"VolumeSelectionThresholds default={survey_default}, "
        f"recommendation_log={rec_val}"
    )


def test_daily_capital_cap_is_consistent_between_python_modules() -> None:
    """``daily_capital_cap_usd`` default must agree between live + survey.

    Bug surfaced 2026-05-18: live_execution defaulted to $10, survey to
    $10, but the wrapper exported $15 via env. The Python defaults
    diverged from the operational value.
    """
    from kalshi_weather.analytics.volume_selection import VolumeSelectionThresholds
    from kalshi_weather.engines.live_execution import DEFAULT_DAILY_USD_CAP

    survey_default = VolumeSelectionThresholds().daily_capital_cap_usd
    live_default = Decimal(str(DEFAULT_DAILY_USD_CAP))

    assert survey_default == live_default, (
        f"daily_capital_cap_usd drift: "
        f"VolumeSelectionThresholds default={survey_default}, "
        f"live_execution.DEFAULT_DAILY_USD_CAP={live_default}"
    )


def test_per_city_date_cap_is_consistent_between_shadow_and_decision() -> None:
    """``_MAX_DISTINCT_MARKETS_PER_CITY_PER_DAY`` must equal ``_SAME_CITY_DATE_CAP``.

    Bug surfaced 2026-05-18 morning: shadow.py allowed up to 3 distinct
    markets per (city, date) but decision.py's separate constant gated
    every evaluation at the FIRST same-date position. Result: 6 hours
    of zero taker_candidates overnight while positions accumulated.
    """
    from kalshi_weather.engines import shadow

    shadow_cap = shadow._MAX_DISTINCT_MARKETS_PER_CITY_PER_DAY

    # decision.py defines its own constant inside a function body. Parse
    # the source to extract the literal value rather than executing the
    # function (which needs a lot of arguments).
    decision_src = (REPO_ROOT / "src" / "kalshi_weather" / "engines" / "decision.py").read_text()
    m = re.search(r"_SAME_CITY_DATE_CAP\s*=\s*(\d+)", decision_src)
    assert m is not None, "could not locate _SAME_CITY_DATE_CAP in decision.py"
    decision_cap = int(m.group(1))

    assert shadow_cap == decision_cap, (
        f"per-city-date cap drift: "
        f"shadow._MAX_DISTINCT_MARKETS_PER_CITY_PER_DAY={shadow_cap}, "
        f"decision._SAME_CITY_DATE_CAP={decision_cap}"
    )


def _parse_shell_int(text: str, var_name: str) -> int:
    """Extract a numeric shell variable assignment value.

    Tolerates trailing ``# comment`` on the assignment line.
    """
    # Match ``NAME=123`` or ``: ${NAME:=123}`` or ``NAME="123"`` etc.
    # We anchor the variable name + assignment but allow anything after the
    # number (whitespace, ``#`` comments, etc.).
    patterns = [
        rf'^{var_name}\s*=\s*"?(\d+(?:\.\d+)?)"?(?:\s|$|#)',
        rf':\s*\${{{var_name}:=([\d.]+)}}',
    ]
    for line in text.splitlines():
        for pat in patterns:
            m = re.match(pat, line)
            if m:
                return int(float(m.group(1)))
    raise ValueError(f"could not parse {var_name} from shell text")


def test_lock_ttl_must_exceed_cycle_timeout() -> None:
    """LOCK_TTL_SECONDS > MAX_CYCLE_SECONDS — invariant.

    If the lock expires while a cycle is still running, the next slot's
    cycle will see "stale lock" and start a parallel run. Two cycles
    writing to the same SQLite DB and heartbeat is the kind of state
    corruption we cannot recover from automatically.
    """
    from kalshi_weather.tools.run_city_cycle import MAX_CYCLE_SECONDS

    wrapper = (REPO_ROOT / "scripts" / "run_weather_cycle.sh").read_text()
    lock_ttl = _parse_shell_int(wrapper, "LOCK_TTL_SECONDS")

    assert lock_ttl > MAX_CYCLE_SECONDS, (
        f"LOCK_TTL_SECONDS ({lock_ttl}) must be strictly > "
        f"MAX_CYCLE_SECONDS ({MAX_CYCLE_SECONDS}). Otherwise the lock "
        f"can release mid-cycle and a second cycle starts running in parallel."
    )


def test_cooldown_must_be_under_slot_interval() -> None:
    """COOLDOWN_SECONDS < 30 min — primary-cycle interval invariant.

    The wrapper skips re-running if the last cycle was within
    COOLDOWN_SECONDS. The primary scheduler fires every 30 min (1800s),
    so the cooldown must be strictly under 30 min or we'll skip every
    scheduled primary too.
    """
    wrapper = (REPO_ROOT / "scripts" / "run_weather_cycle.sh").read_text()
    cooldown = _parse_shell_int(wrapper, "COOLDOWN_SECONDS")
    PRIMARY_SLOT_INTERVAL_SECONDS = 30 * 60  # 1800s — :00 and :30
    assert cooldown < PRIMARY_SLOT_INTERVAL_SECONDS, (
        f"COOLDOWN_SECONDS ({cooldown}) must be strictly < "
        f"{PRIMARY_SLOT_INTERVAL_SECONDS} (30 min between :00 and :30 primary fires). "
        f"Otherwise the primary scheduler at :30 will see a too-recent end and skip."
    )


def test_watchdog_in_progress_window_must_exceed_cycle_timeout() -> None:
    """Watchdog's "cycle in progress" cap must exceed MAX_CYCLE_SECONDS.

    If a cycle legitimately runs longer than the watchdog's window, the
    watchdog stops treating it as in-progress and writes a false FAILED.
    """
    from kalshi_weather.tools.run_city_cycle import MAX_CYCLE_SECONDS

    watchdog = (REPO_ROOT / "scripts" / "watchdog.sh").read_text()
    # The cap is hardcoded inline in an arithmetic comparison.
    m = re.search(r"\(now_epoch\s*-\s*hb_epoch\)\s*<\s*(\d+)", watchdog)
    assert m is not None, "could not locate watchdog in-progress cap"
    in_progress_cap = int(m.group(1))

    assert in_progress_cap > MAX_CYCLE_SECONDS, (
        f"Watchdog in-progress window ({in_progress_cap}s) must be > "
        f"MAX_CYCLE_SECONDS ({MAX_CYCLE_SECONDS}). Otherwise the watchdog "
        f"will false-alarm on cycles that legitimately use the full budget."
    )
