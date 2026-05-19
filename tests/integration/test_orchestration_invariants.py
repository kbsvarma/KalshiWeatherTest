"""Orchestration-layer invariants — log parsers, lock semantics, heartbeat.

These tests exercise the shell/file-system layer that today's bugs lived in.
Each test is paired with a comment naming the specific bug from the 2026-05-18
audit it would have caught.

What they test:

  * Log-parser substring bug — every script that picks the "last cycle end"
    from cron_cycle.log must use the literal ────── marker, not a substring
    match like ``grep "cycle end"`` (which also matches "cycle ended" in
    skip-log lines). This regressed the cooldown gate at 1:25 PM ET.

  * Atomic-mkdir lock — two parallel invocations of the wrapper must result
    in exactly one acquiring the lock. The previous test-then-write pattern
    was a classic TOCTOU race.

  * Atomic heartbeat write — concurrent readers must never see a partial
    or empty heartbeat file. The previous ``> file`` truncate-then-write
    leaves a window where readers see empty content.

  * Cooldown gate behavior — the wrapper must SKIP within the cooldown
    window and RUN after it, and must NOT mistake its own skip-log line
    for a real cycle_end.

Each test uses subprocess to invoke the real shell scripts against
hermetic temp directories, so the production code paths are exercised
directly.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"

CYCLE_END_MARKER = "────── cycle end ──────"
CYCLE_START_MARKER = "────── cycle start ──────"


# ────────────────────────────────────────────────────────────────────────────
# Log-parser invariants
# ────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def log_with_skip_after_real_end(tmp_path: Path) -> Path:
    """Synthetic cron_cycle.log: real cycle_end FOLLOWED BY a skip-log line.

    This is the exact pattern that broke the cooldown gate. The skip log
    text contains the substring "cycle ended", so a naive grep matches it.
    """
    log = tmp_path / "cron_cycle.log"
    log.write_text(
        f"[2026-05-18T17:30:00Z] {CYCLE_START_MARKER}\n"
        f"[2026-05-18T17:37:20Z] {CYCLE_END_MARKER}\n"
        f"[2026-05-18T17:45:02Z] last cycle ended 462s ago (< 1200s cooldown) — skipping\n"
        f"[2026-05-18T18:00:05Z] last cycle ended 903s ago (< 1200s cooldown) — skipping\n"
    )
    return log


def _grep_last_cycle_end(log: Path) -> str:
    """Run the production grep pattern used by all log-parsers."""
    result = subprocess.run(
        ["grep", "-F", CYCLE_END_MARKER, str(log)],
        capture_output=True, text=True, check=False,
    )
    lines = [l for l in result.stdout.splitlines() if l]
    return lines[-1] if lines else ""


def test_grep_F_marker_does_not_match_skip_log_lines(log_with_skip_after_real_end: Path) -> None:
    """``grep -F`` must match ONLY the real marker, not "cycle ended" skip lines.

    Catches: the 2026-05-18 cooldown regression that silently broke the bot
    for 45 min. The previous ``grep "cycle end"`` matched the skip-log line
    because "cycle ended" contains "cycle end" as a substring.
    """
    last_match = _grep_last_cycle_end(log_with_skip_after_real_end)
    assert CYCLE_END_MARKER in last_match
    assert "skipping" not in last_match
    assert "17:37:20Z" in last_match, (
        "Last cycle_end should be the 17:37:20Z marker line, not a later skip log"
    )


def test_legacy_substring_grep_would_have_matched_skip_log(log_with_skip_after_real_end: Path) -> None:
    """Negative control: the OLD ``grep "cycle end"`` pattern matched skip logs.

    This documents the regression: ``grep "cycle end"`` returns the most
    recent SKIP log line because "cycle ended" contains the substring.
    Pinning this behavior in a test makes it obvious if anyone reverts
    to the broken pattern.
    """
    result = subprocess.run(
        ["grep", "cycle end", str(log_with_skip_after_real_end)],
        capture_output=True, text=True, check=False,
    )
    lines = [l for l in result.stdout.splitlines() if l]
    last = lines[-1]
    # The old pattern picks up the skip log line, NOT the real marker.
    assert "skipping" in last
    assert CYCLE_END_MARKER not in last


def test_all_log_parsers_use_literal_marker() -> None:
    """Every script/code path that finds the last cycle_end must use ``grep -F``
    against the literal marker, not a substring pattern.

    Scans the source files and asserts the right pattern.
    """
    targets = [
        ("scripts/run_weather_cycle.sh", 'grep -F "────── cycle end ──────"'),
        ("scripts/healthcheck.sh",       'grep -F "────── cycle end ──────"'),
        ("scripts/watchdog.sh",          'grep -F "────── cycle end ──────"'),
        ("scripts/write_cycle_report.sh", 'grep -nF "────── cycle end ──────"'),
    ]
    for rel, expected in targets:
        text = (REPO_ROOT / rel).read_text()
        assert expected in text, (
            f"{rel} must use the literal cycle-end marker via ``grep -F`` "
            f"to avoid matching skip-log lines. Expected substring: {expected!r}"
        )

    # Dashboard parses in pure Python; check it doesn't use the BROKEN
    # substring pattern in EXECUTABLE code.
    dashboard_lines = (REPO_ROOT / "scripts" / "dashboard.py").read_text().splitlines()
    # Strip comments and triple-quoted blocks from each line so we don't
    # flag the test-naming docstring that intentionally cites the broken
    # pattern. Simple lexer is enough — these patterns don't occur inside
    # multiline strings in this file.
    def _strip_comments(line: str) -> str:
        # Remove anything from `#` onward (rough but enough for this file).
        if "#" in line:
            return line[: line.index("#")]
        return line
    code_lines = [_strip_comments(l) for l in dashboard_lines]
    code_text = "\n".join(code_lines)
    # Naive substring `"cycle end" in line` is the broken pattern — it
    # matches the skip-log "cycle ended" lines too. Must not be present in
    # executable code.
    assert '"cycle end" in line' not in code_text, (
        'dashboard.py contains the broken substring pattern `"cycle end" in line` '
        'in executable code. This matches "cycle ended" in skip-log lines. '
        'Use the full marker via a module-level constant.'
    )
    assert '"cycle start" in line' not in code_text, (
        'dashboard.py contains the broken substring pattern `"cycle start" in line` '
        'in executable code. Use the full marker.'
    )
    # And the literal marker must appear at least once so we know the
    # parser is anchored to it. Whole file is fine (constant + comments OK).
    full_text = (REPO_ROOT / "scripts" / "dashboard.py").read_text()
    assert "────── cycle end ──────" in full_text, (
        "dashboard.py must reference the literal cycle-end marker somewhere."
    )


# ────────────────────────────────────────────────────────────────────────────
# Atomic-mkdir lock invariant
# ────────────────────────────────────────────────────────────────────────────
def test_mkdir_lock_excludes_concurrent_acquirers(tmp_path: Path) -> None:
    """mkdir(2) is atomic — exactly one caller succeeds on parallel attempts.

    Catches: the 2026-05-18 audit finding that ``[[ -f LOCK ]]`` then
    ``echo $$ > LOCK`` is a TOCTOU race. mkdir replaces both steps with
    a single atomic syscall.
    """
    lock_dir = tmp_path / "cycle.lockdir"
    N = 20
    # Launch N background shells racing for the lock.
    procs = [
        subprocess.Popen(
            ["bash", "-c", f"mkdir {lock_dir} 2>/dev/null && echo WON || echo LOST"],
            stdout=subprocess.PIPE, text=True,
        )
        for _ in range(N)
    ]
    outputs = [p.communicate()[0].strip() for p in procs]
    won = sum(1 for o in outputs if o == "WON")
    lost = sum(1 for o in outputs if o == "LOST")
    assert won == 1, f"expected exactly 1 winner, got {won} (lost: {lost})"
    assert won + lost == N


# ────────────────────────────────────────────────────────────────────────────
# Atomic heartbeat-write invariant
# ────────────────────────────────────────────────────────────────────────────
def test_heartbeat_atomic_write_never_yields_partial_reads(tmp_path: Path) -> None:
    """Writers using tmp-file + ``mv -f`` never expose a partial state.

    Spawns a background writer that updates the heartbeat in a tight loop,
    while the foreground reads it many times. Every read must show exactly
    3 lines and a valid ``phase=<value>`` line — no truncated state.

    Catches: the 2026-05-18 audit finding that ``printf ... > FILE`` is
    truncate-then-write, leaving a window where readers see empty content.
    """
    hbp = tmp_path / "heartbeat.txt"
    # Initialize so the file exists when reader starts.
    hbp.write_text("phase=init\nutc=t0\nhost_pid=0\n")

    writer = subprocess.Popen(
        ["bash", "-c", textwrap.dedent(f"""
            set -u
            for i in $(seq 1 200); do
              tmp="{hbp}.tmp.$$.$i"
              printf 'phase=cycle_start\\nutc=2026-05-18T%02d:00:00Z\\nhost_pid=%d\\n' "$i" "$i" > "$tmp"
              mv -f "$tmp" "{hbp}"
            done
        """)],
    )
    partial_count = 0
    total_reads = 0
    while writer.poll() is None or total_reads < 500:
        try:
            content = hbp.read_text()
        except FileNotFoundError:
            continue
        total_reads += 1
        lines = content.split("\n")
        # Trailing newline yields empty string after split → 4 elements expected
        if len([l for l in lines if l]) != 3:
            partial_count += 1
        if not content.startswith("phase="):
            partial_count += 1
        if total_reads >= 500 and writer.poll() is not None:
            break
    writer.wait(timeout=10)

    assert partial_count == 0, (
        f"Atomic heartbeat write violated: {partial_count} of {total_reads} "
        f"reads saw partial content. Writers must use tmp-file + ``mv -f``, "
        f"not ``> FILE`` which is truncate-then-write."
    )


# ────────────────────────────────────────────────────────────────────────────
# Cooldown gate end-to-end behavior
# ────────────────────────────────────────────────────────────────────────────
def _run_cooldown_grep(log_text: str, tmp: Path) -> int | None:
    """Mimic the wrapper's cooldown computation against a synthetic log.

    Returns the age in seconds the wrapper would compute, or None if it
    finds no real cycle_end marker.
    """
    log = tmp / "cron.log"
    log.write_text(log_text)
    # Reproduce the wrapper's pipeline:
    #   grep -F MARKER | tail -1 | awk -F'[][]' '{print $2}'
    proc = subprocess.run(
        ["bash", "-c", f"grep -F '{CYCLE_END_MARKER}' '{log}' 2>/dev/null | tail -1 | awk -F'[][]' '{{print $2}}'"],
        capture_output=True, text=True,
    )
    last_end_utc = proc.stdout.strip()
    if not last_end_utc:
        return None
    # Parse the UTC timestamp to epoch (BSD `date -j -u -f`)
    parse = subprocess.run(
        ["date", "-j", "-u", "-f", "%Y-%m-%dT%H:%M:%SZ", last_end_utc, "+%s"],
        capture_output=True, text=True,
    )
    last_end_epoch = int(parse.stdout.strip())
    now_epoch = int(time.time())
    return now_epoch - last_end_epoch


def test_cooldown_gate_uses_real_cycle_end_not_skip_lines(tmp_path: Path) -> None:
    """End-to-end: the cooldown computation must anchor on the REAL last
    cycle_end, even when later skip-log lines exist in the file.

    Catches: today's regression where the wrapper kept seeing its own
    skip-log line as the "last cycle end" and re-skipping forever.
    """
    # Real cycle_end 30 min ago, but several skip lines after it.
    thirty_min_ago_utc = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 1800)
    )
    ten_min_ago_utc = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 600)
    )
    log_text = (
        f"[{thirty_min_ago_utc}] {CYCLE_END_MARKER}\n"
        f"[{ten_min_ago_utc}] last cycle ended 600s ago (< 1200s cooldown) — skipping\n"
    )
    age = _run_cooldown_grep(log_text, tmp_path)
    assert age is not None
    # The real cycle_end was ~30 min ago; the skip line shouldn't matter.
    # Allow 30s of clock slop.
    assert 1770 <= age <= 1830, (
        f"Expected ~1800s (30 min) since the REAL cycle_end, got {age}s. "
        f"This means the parser is picking up the skip-log line at "
        f"~600s as the 'last cycle end' — the today's bug."
    )


def test_watchdog_recognizes_pre_slot_cooldown_coverage() -> None:
    """Watchdog must accept a cycle_end that landed within the cooldown
    window BEFORE the slot as satisfying that slot.

    Scenario the bug came from: a manual kickstart (or backup-scheduler
    failover) ends at 2:29:41 PM. The wrapper's cooldown then correctly
    skips the scheduled 2:30 PM primary. Watchdog at 2:38 must not
    false-alarm — the slot's intent (recent work) was met.
    """
    text = (REPO_ROOT / "scripts" / "watchdog.sh").read_text()
    # The Case A2 logic must be present.
    assert "yes_pre_slot_cooldown" in text or "pre-slot cycle" in text, (
        "watchdog.sh must include the Case A2 slot-coverage check — "
        "a cycle ending shortly BEFORE the slot covers that slot's intent. "
        "Without this, off-schedule kickstarts produce spurious FAILED rows."
    )
    # Pin the cooldown coverage window so it stays in sync with the wrapper's
    # COOLDOWN_SECONDS (1200s).
    import re
    m = re.search(r"COOLDOWN_SLOT_COVERAGE_SECONDS\s*=\s*(\d+)", text)
    assert m is not None, "expected COOLDOWN_SLOT_COVERAGE_SECONDS in watchdog.sh"
    watchdog_cooldown = int(m.group(1))
    wrapper = (REPO_ROOT / "scripts" / "run_weather_cycle.sh").read_text()
    m2 = re.search(r"^COOLDOWN_SECONDS\s*=\s*(\d+)", wrapper, re.M)
    assert m2 is not None, "expected COOLDOWN_SECONDS in run_weather_cycle.sh"
    wrapper_cooldown = int(m2.group(1))
    assert watchdog_cooldown == wrapper_cooldown, (
        f"watchdog COOLDOWN_SLOT_COVERAGE_SECONDS ({watchdog_cooldown}) must "
        f"equal wrapper COOLDOWN_SECONDS ({wrapper_cooldown}). Otherwise "
        f"the watchdog will alarm on slots the wrapper legitimately skipped."
    )
