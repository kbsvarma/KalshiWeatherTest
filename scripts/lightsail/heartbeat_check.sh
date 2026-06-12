#!/usr/bin/env bash
# Heartbeat check for the Kalshi weather bot on Lightsail.
#
# Returns 0 if everything looks healthy, 1 with a printed alert if not.
# Designed for manual run or cron:
#   */15 * * * * /path/to/heartbeat_check.sh || echo "ALERT" | mail -s "kalshi-bot down" you@...
#
# Checks:
#   1. Lightsail instance state == "running" (control plane)
#   2. SSM agent PingStatus == "Online"
#   3. Avg CPU > 0.5% over last 30 min (bot is actually cycling, not idle)
#   4. Most recent cycle ended < 60 min ago (per cron_cycle.log via SSM)
set -uo pipefail

INSTANCE_NAME="${KALSHI_LIGHTSAIL_INSTANCE:-kalshi-bot}"
SSM_INSTANCE_ID="${KALSHI_LIGHTSAIL_SSM:-mi-0d7014ef3ea88eee3}"
REGION="${AWS_REGION:-us-east-1}"

ALERTS=()

# 1. Lightsail control plane
state=$(aws lightsail get-instance --instance-name "$INSTANCE_NAME" --region "$REGION" \
        --query 'instance.state.name' --output text 2>/dev/null)
if [[ "$state" != "running" ]]; then
  ALERTS+=("Lightsail instance state=$state (expected running)")
fi

# 2. SSM agent ping
ping=$(aws ssm describe-instance-information \
        --instance-information-filter-list "key=InstanceIds,valueSet=$SSM_INSTANCE_ID" \
        --region "$REGION" \
        --query 'InstanceInformationList[0].PingStatus' --output text 2>/dev/null)
if [[ "$ping" != "Online" ]]; then
  ALERTS+=("SSM agent ping=$ping (expected Online)")
fi

# 3. CPU activity — last 6 windows of 5-min averages
start=$(date -u -v-30M +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -d '30 minutes ago' +%Y-%m-%dT%H:%M:%SZ)
end=$(date -u +%Y-%m-%dT%H:%M:%SZ)
avg_cpu=$(aws lightsail get-instance-metric-data \
          --instance-name "$INSTANCE_NAME" \
          --metric-name CPUUtilization \
          --period 300 --statistics Average --unit Percent \
          --start-time "$start" --end-time "$end" \
          --region "$REGION" \
          --query 'metricData[*].average' --output text 2>/dev/null \
          | tr '\t' '\n' | awk '{s+=$1; n++} END {if (n>0) printf "%.2f", s/n; else print 0}')
# Threshold: 0.5% avg over 30 min. Bot cycling lifts CPU well above 1%.
if awk -v v="$avg_cpu" 'BEGIN{exit !(v < 0.5)}'; then
  ALERTS+=("Lightsail CPU avg=${avg_cpu}% over 30min (expected >0.5% if cycling)")
fi

# 4. Last cycle freshness (only if SSM is online)
if [[ "$ping" == "Online" ]]; then
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  last_end=$(bash "$script_dir/ssm.sh" run "grep -E 'cycle end' /opt/kalshi-weather/logs/cron_cycle.log | tail -1 | awk -F'[][]' '{print \$2}'" 2>/dev/null | tail -1)
  if [[ -n "$last_end" ]]; then
    # last_end is e.g. 2026-05-22T20:45:25Z
    last_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_end" "+%s" 2>/dev/null \
                 || date -d "$last_end" "+%s" 2>/dev/null \
                 || echo 0)
    now_epoch=$(date -u "+%s")
    age=$((now_epoch - last_epoch))
    if (( age > 3600 )); then
      ALERTS+=("Last cycle end ${age}s ago (${last_end}) — expected <3600s (1 hr)")
    fi
  fi
fi

# Report
if (( ${#ALERTS[@]} == 0 )); then
  echo "OK  Lightsail=$state  SSM=$ping  CPU=${avg_cpu}%  last_cycle=${last_end:-?}"
  exit 0
fi

echo "ALERT  kalshi-bot health check failed at $(date -u +%Y-%m-%dT%H:%M:%SZ):"
for a in "${ALERTS[@]}"; do echo "  - $a"; done
exit 1
