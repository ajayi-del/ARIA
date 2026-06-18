# ARIA — Claude Code Context

This file is loaded automatically when you run `claude` inside this project.
Extended architecture, AI Fund Manager spec, and agent details live in `~/kingdom_prompt.md`.

## This Project
- ARIA: autonomous perpetuals trading system on SoDEX mainnet
- Local path: /Users/dayodapper/CascadeProjects/ARIA/
- Server path: /home/dayodapper/ARIA/
- Git remote: https://github.com/ajayi-del/ARIA.git (branch: main)
- Server SSH: gcloud compute ssh aria-prod --zone=europe-west3-c
- Language: Python 3.12, tmux session: aria
- Test suite: python3 -m pytest tests/ -q (125 tests — all must pass before restart)
- Venv: .venv/bin/python -m pytest tests/ -v

## Who Is Dayo
Dayo Ajayi. Quant trader and builder. GitHub: @ajayi-del.
Expects senior-engineer-level output. Surgical changes only. No half-measures.
Communicates in terse command-style. ALL-CAPS means urgent.
Quant vocabulary: "gate" = risk filter, "coherence" = signal quality, "WTD" = spec.

## Key Files (ARIA)
  core/config.py              — all constants, thresholds, session params
  core/market_engine.py       — MicrostructureAnalyzer, signal computation
  core/risk_engine.py         — Kant gate, DrawdownGuard, recovery mode
  core/strategy_runner.py     — Nietzsche, signal→order pipeline
  core/chancellor.py          — Kingdom-level position governance
  execution/sodex_client.py   — EIP-712 signed order submission
  data/bybit_feed.py          — Bybit WS: candles, OB, liquidation
  data/ssi_feed.py            — SoSValue SSI WebSocket (MAG7/DEFI/MEME/US)
  display/terminal.py         — Live terminal UI
  vault/vault_manager.py      — Watermark, drawdown tracking
  agents/sovereign.py         — Yield-optimized spot agent (◆)
  monitoring/alerts.py        — Telegram alerts (not the Kimi bot)
  kingdom/chancellor.py       — Cross-agent Chancellor logic

## The Philosophy
### KANT — governs structure
Before any trade: "Is this structurally sound?"
Order type, market regime, liquidity, timing, capital structure.
If Kant says no: no trade. No exceptions.

### NIETZSCHE — governs conviction
After Kant approves: "How convicted am I?"
Formula: hist_wr x coherence x agent_alignment
Will states: AGGRESSIVE / CONVICTED / NEUTRAL / CAUTIOUS / ABSTAIN
Size follows conviction. Never full size without full conviction.

### THE CHANCELLOR — governs the kingdom
Constitution (drawdown stored as PERCENT e.g. 8.0 not 0.08):
  max_kingdom_exposure:    60%
  max_symbol_exposure:     15%
  max_daily_loss:           5%
  veto_drawdown:            8.0 (percent scale)
  emergency_halt_balance: $150

Agreement → size modifier:
  COMPOUND_STRONG:   1.25x
  COMPOUND_WEAK:     1.00x
  CONFLICT:          0.20x (AUGUR stands down)
  SINGLE_ARIA_STRONG: 0.70x
  SINGLE_ARIA_WEAK:  0.40x
  VETO:              0x

## SoDEX Auth Rules
  GET  (balance, positions, orders): wallet 0xdb87899... in URL, NO X-API-Key
  POST/DELETE (orders, leverage):    X-API-Key = 0x36C54F... (signing key, not wallet)
  Min notional gate: $10 before exchange submission
  SOSO_STAKED=168 → 5% fee discount active

## Signal Architecture
  Tier 1: SoSValue SSI — sector rotation (MAG7SSI, DEFISSI, MEMESSI, USSI)
  Tier 2: Equity momentum / earnings
  Tier 3: Microstructure — sweep ratio, VPIN, stop cluster, order imbalance
  Tier 4: ValueChain cascade — eth_getLogs on-chain liquidation detection (THE EDGE)
  Tier 5: Funding rate regime
  Tier 6: SoDEX liquidation signal

  Signal flow: Raw → macro_applied → session_filter → quant_filter → Kant → Nietzsche → Chancellor → execution

## Regime & Sizing
  13-regime classifier — rank-based momentum, dispersion, coherence
  ATR: 5m for crypto (Bybit-seeded), 15m for equities
  Session multipliers: Asian=0.60x, London=0.85x, US=1.00x, Overlap=1.10x
  Recovery mode: 0.50x cap, floor raised to 5.6 coherence

## Bybit V5 TP/SL (for AUGUR — ARIA uses SoDEX)
  positionIdx: 0 (one-way mode)
  tpTriggerBy / slTriggerBy: "MarkPrice" — prevents wick fills
  Always: entry=mark_price passed to place_order

## Hard Rules For This Project
  1. Never touch tiers 1-6 signal logic without explicit instruction
  2. Never touch Kant, Nietzsche, or Chancellor engines without explicit instruction
  3. Never change leverage cap without explicit instruction
  4. Never restart with open positions — grep "open_positions" logs/aria.log | tail -3
  5. Always run tests before restart: python3 -m pytest tests/ -q
  6. Always show git diff before deploying to server
  7. Drawdown stored as PERCENT (8.0 = 8%) not decimal (0.08) — never mix scales
  8. Grep first, fix later. Never guess.
  9. ALWAYS check exchange API for open positions before restart — never rely on stale log files. The source of truth is the live API (SoDEX positions endpoint), not `logs/aria.log`.
  9. Surgical only. One file, one fix, git diff before deploy.
  10. Kingdom path = /home/dayodapper/kingdom/ (server) never Mac path.
  11. Leverage: 5x max. 7x AUGUR. 10x SMART_MONEY+ARIA only.
  12. Chancellor is absolute. No agent overrides VETO.
  13. Verify within 60s after every deploy. Rollback if unexpected.
  14. Journal is permanent. Never delete hist_wr or journal entries.
  15. NEVER delete any file. Deletion requires 3x explicit written approval from Dayo.

## Deploy Flow
  1. Edit local /Users/dayodapper/CascadeProjects/ARIA/
  2. git add -p && git commit -m "fix: <description>"
  3. git push origin main
  4. gcloud compute ssh aria-prod --zone=europe-west3-c
  5. cd ~/ARIA && git pull && tmux attach -t aria
  6. Restart: Ctrl+C, python3 main.py (only after grep confirms no open positions)
  7. Watch logs for 60s for expected events

## Session Workflow
  Step 1: grep "open_positions" ~/ARIA/logs/aria.log | tail -3
          grep "AUGUR HEARTBEAT" ~/AUGUR/logs/augur.log | tail -3
          cat /home/dayodapper/kingdom/kingdom_state.json | python3 -m json.tool | head -50
  Step 2: Identify precisely — exact log lines, file + line number, root cause
  Step 3: Propose — git diff format, risk level (low/medium/high)
  Step 4: Wait for approval on high risk
  Step 5: Apply → verify within 60s → rollback if unexpected

## Agent Safety Rails
### Pre-Action Checklist
  1. Have I read the relevant file? (not assumed its content)
  2. Have I grepped the logs for the exact error string?
  3. Is this the minimum change that solves the problem?
  4. Will this break any other module that imports the same function?
  5. Is there an open position that could be affected by a restart?
  6. Can this be rolled back in under 60 seconds?
  7. Is there a test I can run before deploying?

### Confidence Disclosure
  HIGH   — I have read the exact code and logs. Root cause confirmed.
  MEDIUM — I have partial evidence. This is my best hypothesis.
  LOW    — I am reasoning from general patterns. Verify before applying.

### Change Blast Radius
  CONTAINED — one function, one file, no shared state
  MODULE    — one module, may affect importers
  SYSTEM    — shared state (kingdom, chancellor, config), could affect both agents
  CRITICAL  — execution layer, risk gates, live order flow

### Rollback Protocol
  git stash — for local uncommitted changes
  git revert — for committed changes already pushed
  State rollback plan before every SYSTEM/CRITICAL change.

### Position Safety Gate
  Before any restart: grep "open_positions" /home/dayodapper/ARIA/logs/aria.log | tail -3
  Confirm positions=[] or positions={}. If positions exist: wait for close or ask Dayo.

## Recent Deployments (update after every push)
  - **2026-05-10** — Phase 7: Dynamic Profit Caps + Scalp Leverage
    - `intelligence/trade_regime.py`: TradeRegimeClassifier (TREND/SCALP/DEFAULT)
    - `risk/dynamic_profit_cap.py`: should_cap() with regime-aware ROE caps
    - `core/config.py`: max_leverage raised 5→10 for BTC/ETH/SOL/BNB
    - `execution/sodex_client.py`: update_leverage_with_fallback(chain: 10→7→5→3→2)
    - `main.py`: _dynamic_profit_cap_loop (5s cadence), regime inference in build_candidate
    - Test suite: OrderResult, SignalDeduplicator, FundingHistory, AdaptiveCalibrator, DailyTradeTracker fixed
  - **2026-05-10** — HTF gate verified: TradFi assets skip BTC HTF bias (main.py:2863)
  - **2026-05-10** — Server restart completed; 1 open position (BTC-USD short)
  - **2026-05-20** — Basket TP v3 + threshold surgical fix
    - `main.py`: re-entry cooldown clearing + robust cancel tracking (basket TP v3)
    - `main.py`: basket TP1 threshold lowered 15% → 10% for faster harvest
    - Server restart with override (3 open positions: AAPL short, 2x BTC long)
    - Post-restart: 2 positions tracked, regime geopolitical_stress, all gates active

## Known Issues (update as fixed)
  1. aria_stale_bets_purged fires per-symbol — move outside for loop
  2. velocity_zscore filter bypassed at zscore=6.0 — bypass if velocity_zscore > 3.0
  3. Liquidation notional in tokens not USD — notional_usd = size * price
  4. Min liq threshold too low ($75 pollutes window) — skip if notional_usd < 1000
  5. **2026-05-10** — BTC "quantity is invalid" on time-stop close: position size ~0.00010 may be below SoDEX min notional ($10) at current price. Needs notional guard before close.
  6. **2026-05-10** — 1000PEPE TP orders rejected "notional is invalid": TP split sizes below $10 minimum. Bracket TP logic needs notional pre-check.
  7. **2026-05-10** — Order type selection is structure-based (Kant) not volatility-based. Missing: low-vol → Limit/GTC (maker), high-vol → Market/IOC (taker). Spread/ATR ratio not wired to order_type override.
  8. **2026-05-10** — ADL monitor is observational only; no automatic leverage reduction or position close at "critical" risk.
  9. **2026-05-10** — Trade journal records outcomes but has no cybernetic feedback loop (does not auto-adjust Kant thresholds, Nietzsche sizing, or order_type WR by regime).
  10. **2026-05-21** — Basket TP + time-stop collision causing bleeding. L4 layer is correct; leak is downstream plumbing.
       - SoDEX rejects native stops (stopPrice is invalid) → debug min stop distance / tick size per asset
       - Software stop guardian too tight → widen multiplier or add volatility-scaling
       - time_stop_loser_3h still killing positions before basket can harvest → basket mode now extends time-stop for green portfolio (fix applied)
       - Over-trading in transitioning regime → raise session coherence floor to 4.0+ when regime=transitioning, or blacklist equities during high flip frequency
       - Basket TP threshold too high for $380 NAV → lowered TP1 10%→4%, TP2 25%→12%, with $1 min harvest guard (fix applied)

## Startup Optimizations (applied 2026-06-18)
These 5 fixes ensure every new Claude instance finds ARIA instantly:

1. **Shell alias `aria`** — in `~/.zshrc`: `alias aria='cd /Users/dayodapper/CascadeProjects/ARIA && claude'`
   Typing `aria` drops into project with CLAUDE.md auto-loaded.

2. **Self-contained CLAUDE.md** — Critical kingdom context (this file) is now inlined.
   Previously `CLAUDE.md` deferred to `~/kingdom_prompt.md` for core rules.
   Extended AI Fund Manager spec still lives in `~/kingdom_prompt.md`.

3. **SessionStart hook** — `.claude/settings.local.json` prints on launch:
   git branch, last 2 commits, open positions from logs/aria.log.
   No need to ask "what's the state?"

4. **Memory index trim** — `MEMORY.md` kept under 20 lines (was already lean).
   Only project-level pointers; no ephemeral state.

5. **Consolidated `CLAUDE.local.md`** — Operating procedures merged into this file.
   `CLAUDE.local.md` now points here to avoid duplicate context loading.

## Claude Code Operating Procedures
### Thinking Modes
- Say "ultrathink" in any prompt to trigger deep analysis mode
- Default thinking is enabled for all model calls
- Use "adaptive" for quick checks, "enabled" for complex architecture work

### Tool Concurrency
- Read-only tools (Read, Bash with ls/grep/cat/find) run in PARALLEL
- Mutating tools (Write, Edit, Bash with kill/rm/git push) run SERIAL
- Batch all reads together, then do writes separately

### Task System
- Use TaskCreate for multi-step work (3+ steps)
- Mark in_progress BEFORE starting, completed when done
- Use blockedBy dependencies when order matters
- Prefer TaskList to check status before claiming new work

### Error Handling
- Always check isAbortError before retrying — don't retry user-canceled ops
- Parse token counts from prompt-too-long errors to decide compact vs truncate
- Use TelemetrySafeError for logs that must not contain code/paths

### Agency / Coordinator Mode
- Research phase: spawn parallel agents for independent angles
- Synthesis phase: YOU read findings and write specific specs
- Implementation phase: one worker at a time per file set
- Verification phase: spawn fresh agent with clean eyes
- Never write "based on your findings" — synthesize yourself
- Continue vs Spawn Fresh: high context overlap -> continue, low overlap -> fresh

### Compact / Summarization
- When context window is full, preserve: user requests, file paths, code snippets, errors, pending tasks
- Strip <analysis> blocks after drafting — they are scratchpads
- Always include "Optional Next Step" with direct quotes from user

### Dual-Thinking Framework (Quant + Philosopher)
When the user says "fix" (or requests any bug fix, patch, or correction), apply both `/quant` and `/philosopher` skills before making any code change.

Execution Order:
  1. Philosopher first — root cause vs symptom, second-order effects, safety axioms
  2. Quant second — probabilistic impact, risk metrics, number scales, EV
  3. Fix only if both pass — smallest change, comment the WHY, run tests
  4. Verify — re-run scenario, check logs, confirm no regressions

Output Format:
```
🔍 Philosopher: [assessment]
📊 Quant: [numerical impact]
🔧 Fix: [what changed]
✅ Verify: [test + log result]
```

## AI Model
  This project is powered by Kimi K2.6 via Claude Code.
  Base URL: https://api.moonshot.ai/anthropic
  Full kingdom context: ~/kingdom_prompt.md
  One operator. One server. Live capital. Build accordingly.

---

## AI Fund Manager — Implementation Roadmap (Next Build)
Full spec lives in `~/kingdom_prompt.md`. This is the summary.

An autonomous layer ABOVE the ARIA trading engine.
AI FM reads ARIA signals, manages a separate budget, and trades with context-awareness
that the rule-based engine cannot match: correlation, win streaks, budget state, world alignment.

### Core Rule
AI FM never touches execution. It writes to param_store. Engine reads param_store.
No USD amounts hardcoded anywhere. All sizing from will probability and param_store percentages.

### Build Order
  Phase 1: intelligence/world_model.py + intelligence/valuechain_intelligence.py
  Phase 2: intelligence/cascade_buildup.py (5-signal anticipation)
  Phase 3: intelligence/calendar_intelligence.py
  Phase 4: intelligence/will_engine.py (Kant x Nietzsche x World)
  Phase 5: intelligence/sector_rotation.py
  Phase 6: intelligence/ai_fund_manager.py (full integration)
  Phase 7: risk/param_store.py extended (AI-writable params with expiry)

### LLM Assignment
  Slow analysis (30min):    kimi-k2.6
  Fast signal eval (<3s):   deepseek-chat
  Kant/Nietzsche verdicts:  kimi-k2.6
  Calendar enrichment:      kimi-k2.6

### Absolute Safety Rules
  NEVER delete any file. Deletion requires 3x explicit written approval from Dayo.
  AI FM never calls execution functions directly -- param_store only.
  All param overrides expire. The AI cannot permanently alter system behaviour.
