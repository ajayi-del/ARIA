#!/usr/bin/env bash
# Runner for tools/entry_markout_watcher.py — detached, survives ssh drops.
set -u
cd "$HOME/ARIA"
mkdir -p logs
if pgrep -f "[.]venv/bin/python tools/entry_markout_watcher[.]py" >/dev/null 2>&1; then
  echo "entry_markout_watcher already running: $(pgrep -f '[.]venv/bin/python tools/entry_markout_watcher[.]py' | tr '\n' ' ')"
  exit 0
fi
exec setsid nohup .venv/bin/python tools/entry_markout_watcher.py \
  >> logs/entry_markout_watcher.log 2>&1 &
echo "entry_markout_watcher started pid=$!"
