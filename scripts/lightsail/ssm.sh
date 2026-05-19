#!/usr/bin/env bash
# Direct SSM wrapper for the Lightsail kalshi-bot — no SSH, no jump host.
#
# Lightsail registered as managed instance ``mi-0d7014ef3ea88eee3``.
# All commands go via AWS SSM SendCommand, which works from any network
# the AWS API is reachable from (i.e. anywhere with internet).
#
# Usage:
#   bash scripts/lightsail/ssm.sh run "tail -20 /opt/kalshi-weather/logs/cron_cycle.log"
#   bash scripts/lightsail/ssm.sh cycle             # trigger a cycle
#   bash scripts/lightsail/ssm.sh status            # systemd timer + service status
#   bash scripts/lightsail/ssm.sh logs              # tail cron_cycle.log
#   bash scripts/lightsail/ssm.sh report            # latest cycle report row
#   bash scripts/lightsail/ssm.sh sync              # git pull + restart dashboard
#   bash scripts/lightsail/ssm.sh shell             # interactive SSM session (needs session-manager-plugin)

set -euo pipefail

INSTANCE="${KALSHI_LIGHTSAIL_SSM:-mi-0d7014ef3ea88eee3}"
REGION="${AWS_REGION:-us-east-1}"

# Send-and-wait helper. Echoes the stdout of the remote command.
run_remote() {
  local script="$1"
  # Pack the script as base64 to avoid quoting hell
  local b64
  b64=$(echo "$script" | base64 | tr -d '\n')
  local cmd_id
  cmd_id=$(aws ssm send-command \
    --instance-ids "$INSTANCE" \
    --document-name AWS-RunShellScript \
    --parameters "{\"commands\":[\"echo '$b64' | base64 -d | bash\"]}" \
    --region "$REGION" \
    --timeout-seconds 600 \
    --query Command.CommandId --output text)
  # Poll
  local status
  for _ in $(seq 1 60); do
    sleep 4
    status=$(aws ssm get-command-invocation --command-id "$cmd_id" \
      --instance-id "$INSTANCE" --region "$REGION" \
      --query Status --output text 2>/dev/null || echo Pending)
    [[ "$status" == "Success" || "$status" == "Failed" || "$status" == "TimedOut" || "$status" == "Cancelled" ]] && break
  done
  aws ssm get-command-invocation --command-id "$cmd_id" \
    --instance-id "$INSTANCE" --region "$REGION" \
    --query StandardOutputContent --output text
  if [[ "$status" != "Success" ]]; then
    echo "[ssm.sh] command status: $status" >&2
    aws ssm get-command-invocation --command-id "$cmd_id" \
      --instance-id "$INSTANCE" --region "$REGION" \
      --query StandardErrorContent --output text >&2
    exit 1
  fi
}

case "${1:-help}" in
  run)
    shift
    run_remote "$*"
    ;;
  cycle)
    run_remote 'sudo systemctl start kalshi-weather-cycle.service && echo "cycle triggered (runs async, ~5 min)"'
    ;;
  status)
    run_remote '
      echo "=== timers ==="
      systemctl list-timers --no-pager | grep kalshi-weather || true
      echo
      echo "=== services ==="
      for s in kalshi-weather-cycle kalshi-weather-dashboard kalshi-weather-settlements kalshi-weather-watchdog; do
        echo "$s: $(systemctl is-active $s.service 2>/dev/null) | enabled=$(systemctl is-enabled $s.service 2>/dev/null) | last-exit=$(systemctl show -p ExecMainStatus --value $s.service 2>/dev/null)"
      done
      echo
      echo "=== heartbeat ==="
      sudo cat /opt/kalshi-weather/logs/heartbeat.txt 2>/dev/null
      echo
      echo "=== latest cycle report ==="
      sudo tail -1 /opt/kalshi-weather/logs/cycle_reports/$(date -u +%Y-%m-%d).jsonl 2>/dev/null || echo "(no report yet today)"
      true
    '
    ;;
  logs)
    run_remote 'sudo tail -60 /opt/kalshi-weather/logs/cron_cycle.log 2>/dev/null'
    ;;
  report)
    run_remote 'sudo tail -1 /opt/kalshi-weather/logs/cycle_reports/$(date -u +%Y-%m-%d).jsonl 2>/dev/null | python3 -m json.tool'
    ;;
  sync)
    run_remote '
      cd /opt/kalshi-weather-clone && sudo -u ubuntu git pull
      sudo rsync -a /opt/kalshi-weather-clone/ /opt/kalshi-weather/ \
        --exclude=.cache --exclude=venv --exclude=secrets \
        --exclude=data/state/runtime.sqlite3 \
        --exclude=logs
      sudo chown -R kalshibot:kalshibot /opt/kalshi-weather
      sudo -u kalshibot /opt/kalshi-weather/venv/bin/pip install -q -r /opt/kalshi-weather/requirements.txt
      sudo systemctl restart kalshi-weather-dashboard.service
      echo "sync complete"
    '
    ;;
  shell)
    if ! command -v session-manager-plugin >/dev/null 2>&1; then
      echo "session-manager-plugin not installed. Install via:"
      echo "  brew install --cask session-manager-plugin"
      exit 1
    fi
    exec aws ssm start-session --target "$INSTANCE" --region "$REGION"
    ;;
  flip)
    bash "$(dirname "$0")/flip_to_live_ssm.sh" "${@:2}"
    ;;
  help|*)
    cat <<USAGE
Usage: ssm.sh <command>

  run <shell-cmd>   Execute arbitrary shell command on Lightsail
  cycle             Trigger one decision cycle now
  status            Show timers + service health
  logs              Tail cron_cycle.log
  report            Show latest cycle_report row (JSON-formatted)
  sync              git pull + restart dashboard
  shell             Open interactive SSM session (needs session-manager-plugin)
  flip [--revert]   Toggle paper→live or live→paper
  help              This message

Instance: $INSTANCE  (region: $REGION)
Override with KALSHI_LIGHTSAIL_SSM / AWS_REGION env vars.
USAGE
    ;;
esac
