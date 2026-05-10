# ARIA — Claude Code Context

This file is loaded automatically when you run `claude` inside this project.
The full system architecture lives in ~/kingdom_prompt.md. This file adds project-specific context.

## This Project
- ARIA: autonomous perpetuals trading system on SoDEX mainnet
- Local path: /Users/dayodapper/CascadeProjects/ARIA/
- Server path: /home/dayodapper/ARIA/
- Git remote: https://github.com/ajayi-del/ARIA.git (branch: main)
- Server SSH: gcloud compute ssh aria-prod --zone=europe-west3-c
- Language: Python 3.12, tmux session: aria
- Test suite: python3 -m pytest tests/ -q (125 tests — all must pass before restart)
- Venv: .venv/bin/python -m pytest tests/ -v

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

## Deploy Flow
  1. Edit local /Users/dayodapper/CascadeProjects/ARIA/
  2. git add -p && git commit -m "fix: <description>"
  3. git push origin main
  4. gcloud compute ssh aria-prod --zone=europe-west3-c
  5. cd ~/ARIA && git pull && tmux attach -t aria
  6. Restart: Ctrl+C, python3 main.py (only after grep confirms no open positions)
  7. Watch logs for 60s for expected events

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

## AI Model
  This project is powered by Kimi K2.6 via Claude Code.
  Base URL: https://api.moonshot.ai/anthropic
  Full kingdom context: ~/kingdom_prompt.md
  One operator. One server. Live capital. Build accordingly.


---
## AI Fund Manager — Implementation Roadmap (Next Build)

Kimi K2.6 is the architect and builder of this system. Full spec in ~/kingdom_prompt.md.

### What It Is
An autonomous layer ABOVE the ARIA trading engine.
AI FM reads ARIA signals, manages a separate budget, and trades with context-awareness
that the rule-based engine cannot match: correlation, win streaks, budget state, world alignment.

### Core Rule
AI FM never touches execution. It writes to param_store. Engine reads param_store.
No USD amounts hardcoded anywhere. All sizing from will probability and param_store percentages.

### Build Order (start here when ready)
  Phase 1: intelligence/world_model.py
           intelligence/valuechain_intelligence.py (net inflow/outflow tracking)

  Phase 2: intelligence/cascade_buildup.py
           (5-signal anticipation: velocity, acceleration, purity, size growth, funding)

  Phase 3: intelligence/calendar_intelligence.py
           (seeds calendar.db, AI-enriched with sector implications)

  Phase 4: intelligence/will_engine.py
           (Kant x Nietzsche x World = will probability -> size)

  Phase 5: intelligence/sector_rotation.py
           (lagging sector detection, catchup trade generator)

  Phase 6: intelligence/ai_fund_manager.py (full integration)
           Three async loops: fast (per signal), slow (30min), autonomous (self-generated)

  Phase 7: risk/param_store.py extended
           AI-writable: leverage, stop_mult, atr_min_pct, blacklist, portfolio_tp, overrides
           All with expires_at. System falls back to config.py defaults on expiry.

### Deployment Protocol Per Phase
  1. dry_run=True -- log decisions, apply nothing (24h validation)
  2. Enable lowest-risk first (ATR adjustments, blacklisting)
  3. Portfolio TP next (highest value, limited downside)
  4. Autonomous trading last (cascade anticipation, sector lag)

### Absolute Safety Rules
  NEVER delete any file. Deletion requires 3x explicit written approval from Dayo.
  AI FM never calls execution functions directly -- param_store only.
  All param overrides expire. The AI cannot permanently alter system behaviour.

### LLM Assignment
  Slow analysis (30min):    kimi-k2.6
  Fast signal eval (<3s):   deepseek-chat
  Kant/Nietzsche verdicts:  kimi-k2.6
  Calendar enrichment:      kimi-k2.6
