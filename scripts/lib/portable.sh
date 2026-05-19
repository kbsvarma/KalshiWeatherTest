#!/usr/bin/env bash
# Portability helpers — abstract over BSD (macOS) vs GNU (Linux) toolchain
# differences in `stat` and `date`. Source from every shell script that
# does timestamp math so the same script runs unmodified on both platforms.
#
# Usage:
#   source "$(dirname "$0")/lib/portable.sh"
#   age=$(_file_mtime "$LOG_FILE")
#   epoch=$(_iso_to_epoch "2026-05-18T17:00:00Z")
#
# No external state; functions return values via stdout.

_KXW_OS="$(uname)"

# _file_size FILE -> bytes (integer)
_file_size() {
  if [[ "$_KXW_OS" == "Darwin" ]]; then
    stat -f%z "$1" 2>/dev/null || echo 0
  else
    stat -c%s "$1" 2>/dev/null || echo 0
  fi
}

# _file_mtime FILE -> mtime epoch seconds (integer)
_file_mtime() {
  if [[ "$_KXW_OS" == "Darwin" ]]; then
    stat -f "%m" "$1" 2>/dev/null || echo 0
  else
    stat -c "%Y" "$1" 2>/dev/null || echo 0
  fi
}

# _iso_to_epoch ISO -> epoch seconds. ISO format: 2026-05-18T17:00:00Z
_iso_to_epoch() {
  local iso="$1"
  if [[ "$_KXW_OS" == "Darwin" ]]; then
    date -j -u -f "%Y-%m-%dT%H:%M:%SZ" "$iso" "+%s" 2>/dev/null || echo 0
  else
    # GNU date accepts ISO 8601 directly when in UTC.
    date -u -d "$iso" "+%s" 2>/dev/null || echo 0
  fi
}

# _et_slot_to_epoch DATE TIME -> epoch seconds (interpreted in America/New_York)
# DATE: YYYY-MM-DD   TIME: HH:MM
_et_slot_to_epoch() {
  local d="$1"
  local t="$2"
  if [[ "$_KXW_OS" == "Darwin" ]]; then
    TZ="America/New_York" date -j -f "%Y-%m-%d %H:%M" "${d} ${t}" "+%s" 2>/dev/null || echo ""
  else
    TZ="America/New_York" date -d "${d} ${t}" "+%s" 2>/dev/null || echo ""
  fi
}

# _epoch_to_et_display EPOCH FMT -> formatted time in America/New_York
_epoch_to_et_display() {
  local epoch="$1"
  local fmt="$2"
  if [[ "$_KXW_OS" == "Darwin" ]]; then
    TZ="America/New_York" date -j -f "%s" "$epoch" "$fmt" 2>/dev/null
  else
    TZ="America/New_York" date -d "@$epoch" "$fmt" 2>/dev/null
  fi
}

# _is_darwin -> 0 if running on macOS, 1 otherwise (for `if _is_darwin; then ...`)
_is_darwin() {
  [[ "$_KXW_OS" == "Darwin" ]]
}
