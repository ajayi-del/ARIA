# Tria Execution Workflow — ARIA Bridge

## Overview
ARIA now routes all signals to Tria web UI. SoDEX balance removed. No server execution.

## Signal Pipeline
```
ARIA Engine (server) → aria_outbox.json
        ↓
  gcloud scp (sync every 5s)
        ↓
  Local aria_outbox.json
        ↓
  tria_signal_router.py (portfolio manager)
        ↓
  tria_commands.json
        ↓
  tria_executor.py (bridge state machine)
        ↓
  Tria Web UI ← pyautogui clicks
```

## What Runs Where
- **Server**: ARIA main.py generates signals only (no execution)
- **Local Mac**: `tria_signal_router.py` + `tria_executor.py` execute on Tria
- **Tria**: Web UI must be visible in browser, logged in

## Commands

### Start the signal router (already running)
```bash
cd /Users/dayodapper/CascadeProjects/ARIA/signals
python3 tria_signal_router.py
```

### Start the Tria executor
```bash
cd /Users/dayodapper/CascadeProjects/ARIA
source .venv/bin/activate
# Confirmation mode — you approve each trade
python3 signals/tria_executor.py

# Auto-execute mode — no prompts
TRIA_CONFIRMATION=false python3 signals/tria_executor.py
```

### Attach to running session
```bash
tmux attach -t tria_executor
```

## How Trades Flow
1. ARIA emits signal to `aria_outbox.json`
2. Router picks it up, runs portfolio logic (max 5 positions)
3. Router writes OPEN/CLOSE command to `tria_commands.json`
4. Executor polls `tria_commands.json` every 3s
5. For OPEN commands:
   - Searches symbol on Tria
   - Clicks Long/Short
   - Enters size
   - Sets leverage
   - **Sets Stop Loss (if template captured)**
   - **Sets Take Profit (if template captured)**
   - Confirms order
   - Verifies fill
6. For CLOSE commands: logged for manual action (not yet automated)

## SL/TP Automation Status
- Tria has TP/SL behind a "TP/SL" tab on the order panel
- Templates `stop_loss_field.png` and `take_profit_field.png` must be captured
- Current behavior: bridge logs warning and sends order WITHOUT SL/TP if templates missing
- **Action required**: dismiss any warning banners, click TP/SL tab, capture field templates with `capture.py`

## Confirmation Prompt
In confirmation mode (default), the executor pauses before each trade:
```
Execute on Tria? [y/N/q]:
  y = execute
  N = skip
  q = quit executor
```

## Safety
- `$80` minimum notional enforced by ARIA signal generation
- Leverage cap `5x` enforced
- Coherence floors lowered to allow Tria signal flow
- `TRIA_REQUIRE_SL_TP=true` aborts trades if SL/TP templates missing
- All trade IDs tracked in `.tria_executed_ids` to prevent duplicates
