#!/usr/bin/env bash
# Lightsail bootstrap — idempotent one-shot installer for the Kalshi weather bot.
#
# Runs once on a fresh Ubuntu 22.04/24.04 box. Re-runnable: every step is
# idempotent (apt install --no-upgrade, mkdir -p, git pull, systemctl enable,
# etc.) so it's safe to re-execute if something fails partway.
#
# Sets up:
#   - System user `kalshibot` (uid <1000, no shell — matches webull-bot pattern)
#   - Install path /opt/kalshi-weather (owned by kalshibot)
#   - Python venv with requirements
#   - Ollama (optional, controlled by INSTALL_OLLAMA env)
#   - systemd units for cycle, settlements, watchdog, dashboard
#   - Default: LIVE_ORDERS_ENABLED=0 — never places real orders until flipped
#
# Usage (run as ubuntu/admin user):
#   curl -O https://path/to/bootstrap.sh && bash bootstrap.sh
#   # or, with the repo already cloned:
#   bash /opt/kalshi-weather/scripts/lightsail/bootstrap.sh
#
# Env knobs:
#   KALSHI_WEATHER_GIT_URL   Repo URL (default: assume current dir is the repo)
#   KALSHI_WEATHER_GIT_REF   Branch/tag/sha to check out (default: main)
#   INSTALL_OLLAMA           1 = install Ollama + pull llama3.2:3b. Default: 1
#   INSTALL_DIR              Default: /opt/kalshi-weather
#   BOT_USER                 Default: kalshibot
#
# Safety:
#   - This script NEVER sets LIVE_ORDERS_ENABLED=1 or LIVE_ORDERS_DRY_RUN=0.
#     The systemd units it installs hard-code LIVE_ORDERS_ENABLED=0. A
#     separate `flip_to_live.sh` is required to switch to live mode.

set -euo pipefail

# ── Config ───────────────────────────────────────────────────────────────
INSTALL_DIR="${INSTALL_DIR:-/opt/kalshi-weather}"
BOT_USER="${BOT_USER:-kalshibot}"
BOT_GROUP="${BOT_USER}"
GIT_URL="${KALSHI_WEATHER_GIT_URL:-}"
GIT_REF="${KALSHI_WEATHER_GIT_REF:-main}"
INSTALL_OLLAMA="${INSTALL_OLLAMA:-1}"
# 2026-05-19: switched default to 1b on Lightsail's 2-vCPU box. 3b
# inference was hitting the 90s extraction timeout. 1b runs in ~25-60s
# warm and still produces valid JSON for our small extraction task
# (confidence/model_spread_flag — the two signals the path engine
# actually consumes). Operators wanting better quality can:
#   INSTALL_OLLAMA=1 OLLAMA_MODEL=llama3.2:3b bash bootstrap.sh
# and then ``systemctl edit kalshi-weather-cycle.service`` to set the
# env var to match.
OLLAMA_MODEL="${OLLAMA_MODEL:-llama3.2:1b}"

PY_VERSION_MIN="3.10"

# Caller must have sudo. Don't require running AS root — running as ubuntu
# with sudo NOPASSWD (Lightsail default) is the supported path.
if ! command -v sudo >/dev/null 2>&1; then
  echo "[bootstrap] sudo not available — aborting"; exit 1
fi

log() { echo "[bootstrap] $*"; }

# ── Step 1: OS packages ──────────────────────────────────────────────────
log "Step 1/8 — installing OS packages"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3 python3-venv python3-pip python3-dev \
  build-essential pkg-config \
  git curl jq unzip rsync \
  sqlite3 \
  ca-certificates

# Python version sanity check
PY_MAJOR_MINOR=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
log "  python3 = $PY_MAJOR_MINOR (need >= $PY_VERSION_MIN)"
if [[ "$(printf '%s\n' "$PY_VERSION_MIN" "$PY_MAJOR_MINOR" | sort -V | head -1)" != "$PY_VERSION_MIN" ]]; then
  log "  FATAL: python3 < $PY_VERSION_MIN. Install a newer Python first."
  exit 1
fi

# ── Step 2: bot user ─────────────────────────────────────────────────────
log "Step 2/8 — service user '$BOT_USER'"
if ! id "$BOT_USER" >/dev/null 2>&1; then
  sudo useradd --system --shell /usr/sbin/nologin --home "$INSTALL_DIR" "$BOT_USER"
  log "  created $BOT_USER"
else
  log "  $BOT_USER already exists"
fi

# ── Step 3: install dir + ownership ─────────────────────────────────────
log "Step 3/8 — $INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"/{logs,logs/cycle_reports,data/state,data/reference}
sudo chown -R "$BOT_USER:$BOT_GROUP" "$INSTALL_DIR"

# ── Step 4: code (clone or copy) ────────────────────────────────────────
log "Step 4/8 — source code"
if [[ -n "$GIT_URL" ]]; then
  # Clone or update
  if [[ -d "$INSTALL_DIR/.git" ]]; then
    sudo -u "$BOT_USER" git -C "$INSTALL_DIR" fetch --quiet origin
    sudo -u "$BOT_USER" git -C "$INSTALL_DIR" checkout --quiet "$GIT_REF"
    sudo -u "$BOT_USER" git -C "$INSTALL_DIR" pull --quiet --ff-only || true
    log "  pulled $GIT_REF from $GIT_URL"
  else
    sudo -u "$BOT_USER" git clone --quiet --branch "$GIT_REF" "$GIT_URL" "$INSTALL_DIR.tmp"
    sudo rm -rf "$INSTALL_DIR"
    sudo mv "$INSTALL_DIR.tmp" "$INSTALL_DIR"
    sudo chown -R "$BOT_USER:$BOT_GROUP" "$INSTALL_DIR"
    log "  cloned $GIT_URL @ $GIT_REF"
  fi
elif [[ -d "$INSTALL_DIR/src/kalshi_weather" ]]; then
  log "  no git URL given; assuming code already present at $INSTALL_DIR"
else
  log "  ERROR: KALSHI_WEATHER_GIT_URL not set and no code at $INSTALL_DIR"
  log "  Either set the env var or rsync the repo to $INSTALL_DIR first."
  exit 1
fi

# ── Step 5: Python venv + requirements ──────────────────────────────────
log "Step 5/8 — venv + pip install"
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
  sudo -u "$BOT_USER" python3 -m venv "$INSTALL_DIR/venv"
fi
sudo -u "$BOT_USER" "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip wheel setuptools
if [[ -f "$INSTALL_DIR/requirements.txt" ]]; then
  sudo -u "$BOT_USER" "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
fi
log "  pip install complete"

# ── Step 6: Ollama (optional) ───────────────────────────────────────────
if [[ "$INSTALL_OLLAMA" == "1" ]]; then
  log "Step 6/8 — Ollama + $OLLAMA_MODEL"
  if ! command -v ollama >/dev/null 2>&1; then
    curl -fsSL https://ollama.com/install.sh | sudo sh
  else
    log "  ollama already installed"
  fi
  # Ollama installs its own systemd unit; ensure it's running.
  sudo systemctl enable --now ollama || true
  # Wait for ollama server to be reachable before pulling.
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then break; fi
    sleep 2
  done
  if curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    if ! sudo -u "$BOT_USER" OLLAMA_HOME=/var/lib/ollama ollama list 2>/dev/null | grep -q "^${OLLAMA_MODEL%:*}"; then
      log "  pulling $OLLAMA_MODEL (this can take 2-3 minutes)..."
      ollama pull "$OLLAMA_MODEL" || sudo ollama pull "$OLLAMA_MODEL" || true
    else
      log "  $OLLAMA_MODEL already pulled"
    fi
  else
    log "  WARN: ollama server didn't come up — AFD extraction will degrade gracefully"
  fi
else
  log "Step 6/8 — skipping Ollama (INSTALL_OLLAMA=$INSTALL_OLLAMA)"
fi

# ── Step 7: systemd units ───────────────────────────────────────────────
log "Step 7/8 — systemd units"
UNITS_SRC="$INSTALL_DIR/deploy/systemd"
UNITS_DST="/etc/systemd/system"

if [[ ! -d "$UNITS_SRC" ]]; then
  log "  ERROR: $UNITS_SRC not found"
  exit 1
fi

for unit in kalshi-weather-cycle.service \
            kalshi-weather-cycle.timer \
            kalshi-weather-settlements.service \
            kalshi-weather-settlements.timer \
            kalshi-weather-watchdog.service \
            kalshi-weather-watchdog.timer \
            kalshi-weather-dashboard.service; do
  if [[ -f "$UNITS_SRC/$unit" ]]; then
    # Template substitution: replace __INSTALL_DIR__ and __BOT_USER__
    sudo sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
             -e "s|__BOT_USER__|$BOT_USER|g" \
             "$UNITS_SRC/$unit" > "/tmp/$unit"
    sudo install -m 0644 "/tmp/$unit" "$UNITS_DST/$unit"
    sudo rm -f "/tmp/$unit"
    log "  installed $unit"
  fi
done
sudo systemctl daemon-reload

# ── Step 8: enable timers (NOT services — they're triggered by timers) ──
log "Step 8/8 — enabling timers"
for t in kalshi-weather-cycle.timer \
         kalshi-weather-settlements.timer \
         kalshi-weather-watchdog.timer; do
  sudo systemctl enable --now "$t" || true
done
# Dashboard runs continuously
sudo systemctl enable --now kalshi-weather-dashboard.service || true

log ""
log "═══════════════════════════════════════════════════════════════════════"
log "BOOTSTRAP COMPLETE"
log "═══════════════════════════════════════════════════════════════════════"
log "Install dir:  $INSTALL_DIR"
log "Service user: $BOT_USER"
log ""
log "Status:"
log "  sudo systemctl status kalshi-weather-cycle.timer"
log "  sudo systemctl status kalshi-weather-dashboard.service"
log "  sudo journalctl -u kalshi-weather-cycle.service -n 50 --no-pager"
log ""
log "Force a cycle right now:"
log "  sudo systemctl start kalshi-weather-cycle.service"
log ""
log "Dashboard: http://<lightsail-ip>:8501  (or via SSH tunnel)"
log ""
log "⚠  PAPER MODE: LIVE_ORDERS_ENABLED=0 in the cycle unit."
log "   To flip to live, run: bash $INSTALL_DIR/scripts/lightsail/flip_to_live.sh"
log "═══════════════════════════════════════════════════════════════════════"
