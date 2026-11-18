#!/usr/bin/env bash
# Prop fix-intent monitor — host cron entry (deterministic collector only).
# Kill switch: touch "$MONITOR_ROOT/DISABLED"
set -euo pipefail

MONITOR_ROOT="${MONITOR_ROOT:-/root/prop_fix_monitor}"
ARIA_ROOT="${ARIA_ROOT:-/root/ARIA}"
STATE_PARENT="${STATE_PARENT:-/root}"
PY="${PROP_FIX_PYTHON:-$ARIA_ROOT/.venv/bin/python}"
if [[ ! -x "$PY" ]]; then
  PY="$(command -v python3)"
fi

mkdir -p "$MONITOR_ROOT"
cd "$MONITOR_ROOT"

# Kill switch is enforced inside collect.py (rewrites report.md + cycles.jsonl).
# Do not early-exit here — otherwise report.md stays stale.

# Prefer installed copy under MONITOR_ROOT; fall back to repo tools/
COLLECT="$MONITOR_ROOT/collect.py"
if [[ ! -f "$COLLECT" ]]; then
  COLLECT="$ARIA_ROOT/tools/prop_fix_monitor/collect.py"
fi

export MONITOR_ROOT STATE_PARENT ARIA_ROOT
export EXPECTED_DEPLOYED_SHA="${EXPECTED_DEPLOYED_SHA:-520ff6c}"

"$PY" "$COLLECT" \
  --monitor-root "$MONITOR_ROOT" \
  --state-parent "$STATE_PARENT" \
  --aria-root "$ARIA_ROOT" \
  >>"$MONITOR_ROOT/cycles.log" 2>&1
