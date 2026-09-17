#!/usr/bin/env bash
# Runner for tools/oracle_freshness_probe.py — detached, survives ssh drops.
set -u
cd "$HOME/ARIA"
mkdir -p logs
exec setsid nohup .venv/bin/python tools/oracle_freshness_probe.py \
  >> logs/oracle_freshness_probe.log 2>&1 &
echo "oracle_freshness_probe started pid=$!"
