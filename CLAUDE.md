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
  Step 0: CHECK PRICE WEATHER FIRST (added 2026-07-27 per Dayo) — before touching ARIA,
          read the actual market so tuning matches the season:
          BTC/ETH/SOL/BNB 48h klines (Bybit public API), plus CL-USD and TSM-USD
          mark prices (SoDEX) before the US equity open. Note: leader/laggard
          structure, vol regime (ann. vol from hourly returns), day-type lean.
          A fix that fits a low-vol grind-up is wrong in a cascade season.
  Step 1: grep "open_positions" ~/ARIA/logs/aria.log | tail -3
          grep "AUGUR HEARTBEAT" ~/AUGUR/logs/augur.log | tail -3
          cat /home/dayodapper/kingdom/kingdom_state.json | python3 -m json.tool | head -50
          cat ~/aria_watchdog/report.md | head -40   (autonomous watchdog — see below)
  Step 2: Identify precisely — exact log lines, file + line number, root cause
  Step 3: Propose — git diff format, risk level (low/medium/high)
  Step 4: Wait for approval on high risk
  Step 5: Apply → verify within 60s → rollback if unexpected

## Autonomous Watchdog (server crontab)
  Cron: `41 */2 * * * /home/dayodapper/aria_watchdog/run_cycle.sh` — runs every 2h (12x daily, exceeds the 2x-daily minimum).
  Cycle: health check (process, log freshness, exchange vs tracked positions, rejection storms) → writes ~/aria_watchdog/report.md + cycles.log.
  Before any manual restart, read report.md first — it may already have diagnosed the issue.

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
  - **2026-07-28 (pm)** — Fill-gap deadlock surgery + phantom-uPnL flap fix (c2dd873 + 42229c0)
    - Probe of the A–E window (1568 signals → 3 fills) named the chokepoints; fixes wired:
    - **Var gate floor-honoring** (`risk/risk_engine.py`): candidate's post-floor size is now validated for margin/VaR instead of re-derived from the risk budget — `compute_size` was raising "below minimum $80" on re-derived sizes the strategy floor had already approved (24 SPCX rejects in 6h). Margin/VaR checks still run on the honored size.
    - **Dust netting-absorb** (`main.py` order placement): opposite-direction entries oversize by sub-$10 dust qty — one-way netting closes the unclosable leg and frees its margin ($72 was locked, blocking campaign trades by ~$23).
    - **Graduated throttle bypass** (`main.py:2948`): graduated symbols skip the 60s/+1.5 signal throttle — the rally confirmation signal itself poisoned the throttle for the entire median graduation lifespan (13 boosts → 0 reached sizing). `signal_throttled` raised debug→info (silent gate drops are unacceptable; 563 visible in first 3h).
    - **Phantom-uPnL flap root fix** (server watchdog 9e2dffb, rescued via format-patch): `Position` has no `mark_price` attr — the MAM uPnL lambda read `getattr(p,'mark_price',0)=0`, marking every position to zero (phantom -$131) and flapping the DD multiplier 1.0↔0.25 every ~30s. The "37% drawdown" was PHANTOM, not real. uPnL now computed from `mark_price_stores`; missing/stale mark → 0 for that leg. `size_multiplier_changed` now logs balance/peak/day_start (drawdown_manager).
    - Verified live 3.1h: **0 multiplier flaps** (was 366), **7/7 execution_decisions APPROVED** (was 3/30), 0 var rejects, 3 fills (ETH ×2, SPCX campaign — first campaign executions), 26 rally graduations. Boot 11:31 UTC, 0 errors.
  - **2026-07-28** — Surgery A–E: sizing integrity, TP basket, seed leg, staleness gates, rally graduation (3fb7309 + ea3b36d)
    - **A — sizing**: `min→max(dd_mult, dm_mult)` at main.py:4049 (both measure the SAME drawdown; min() double-penalised every losing streak). Terminal campaign floor before the Chancellor — post-floor multipliers (ECS/recovery/HTF/meta/vol) were crushing campaign trades $250→~$50; floor now re-applied once after every multiplier, affordability-checked. Dead guardian campaign floor removed.
    - **C — TP basket**: TP2 12→8%, trend TP2 20→12%, winner escape 12→7% (~1.3R), TP2 gets the small-account stack cap TP1 already had (was unbounded → 27% fantasy exits; observed portfolio ROE peak 6.3%).
    - **D1 — funding basis risk**: `position.funding_aligned` now prefers SoDEX-native rate (carry actually paid) with Bybit fallback. Arb-loop entry signal intentionally untouched (lead-lag IS the edge; exits/accrual already SoDEX).
    - **D2 — equity seed root cause**: `HybridFeed.fetch_historical` only called the Bybit leg — 16/33 SoDEX-only symbols (all equities/commodities) got NO seed and starved the interpreter's 50-candle minimum for hours after every off-hours boot (zero stock trades). SoDEX leg added (exceptions isolated per leg); SoDEXFeed skips symbols the Bybit leg owns. Verified live: all equities seeded 55 candles at boot.
    - **Bonus (timing audit)**: 60s mark-price staleness gate at the entry chokepoint + both cascade fast paths (a stalled WS could execute blind fills). meta_block_entries (distracted) now exempts a primed direction-matched aftermath probe (forced-flow physics, not churn).
    - **B — rally graduation**: CONFIRMED rally (≥3/5 pillars, either direction) graduates the symbol to campaign-lite privileges 4h (TTL'd param_store): +1.0 coherence boost (floor 1.5), c-tier bypass, quiet-filter bypass, 2.0× size, strike-gate override at coh≥6. Loop closes: TTL expiry / fade→2h cooloff / direction flip→2h cooloff / losing close→4h cooloff (privilege revoked, symbol NOT halted — one loss is noise). Guards: recovery suppresses, one per direction, 5-min boot grace revokes stale graduations without cooloff.
    - Deploy verified: 0 errors in boot window, ETH long re-adopted mid-restart, all seed legs loaded, single process. Tests 29F/1260P = pre-existing baseline (#12).
  - **2026-07-26** — Basket harvest unblock + ops recovery (2d70c71)
    - `main.py`: basket age-expiry made sticky (`_basket_age_expired`) — ejected symbols were re-absorbed every 5s tick by the already-active branch, producing 10k+ `basket_age_expiry` log lines/day and unstable time_stop ownership.
    - `main.py`: EMA-smoothed L4 avg depth ratio (α=0.2 per 5s tick, ~25s time constant) — raw depth flapped 0.03↔1.9 between ticks, oscillating eff_tp1 between 6% and 10%; portfolio ROE peaked 6.3% on 07-25 while threshold sat at 10%, blocking every basket harvest since 07-25.
    - Ops: rescued two unpushed server-only watchdog commits (14e10eb dust log-spam, 19fa464 TP-clamp) into main via format-patch — server v2 migration had dropped GitHub auth; restored `id_ed25519` on server.
    - Ops: rebuilt corrupt `logs/calendar.db` (SQLite "disk image malformed" — Gate -1 calendar was fail-stale on cached regime; corrupt file preserved as `calendar.db.corrupt-20260726`, engine re-seeded on boot).
    - SOL short closed +$0.40 by time_stop pre-restart; BTC/ETH dust re-adopted with protective stops.
  - **2026-06-19** — 5 Strategic Fixes + Position Cap Expansion
    - `intelligence/cascade_basket.py`: Fixed critical L4 imbalance bug where long cascade entries used inverted scoring (`-imb` instead of `+imb`), silently killing all long cascade confirmations since deployment.
    - `core/config.py`: Enabled `asymmetric_tps_enabled=True` and `dynamic_stops_enabled=True` (Phase 2 features now live).
    - `main.py`: HTF-aware basket TP — portfolio-weighted HTF bias adjusts harvest thresholds (aligned → let runners run; opposed → harvest faster).
    - `main.py`: Drawdown recovery agent bets — survival mode (0.05×) + high-confidence cross-agent bet (p_joint ≥ 0.75) triggers 3.0× amplification (0.15× effective) to accelerate equity rebuild.
    - `main.py` + `execution/schemas.py` + `risk/position_manager.py`: Calibrated pyramid policy v2 — regime-conditional gating (no pyramid in SCALP/MEAN_REVERSION/TRANSITIONING), coherence-tapered sizing (8.0→32%, 10.0→40%), time-decay since TP1 (15min max), combined-position breakeven stop with 0.4% noise buffer, pyramid layer excludes TP3.
    - `core/config.py` + `core/session_config.py`: max_concurrent_positions raised 5→7; max_daily_trades already at 40.
    - Philosophical note: "The pyramid is not a second bet — it is a tactical add to a proven winner. Size it like conviction decays: fast at first, then not at all."
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
  11. **2026-07-26** — Shutdown hang: process logs "ARIA shutdown complete" but never exits (non-daemon thread or pending task). Observed 2/2 restarts. Workaround: kill -9 after shutdown-complete line before starting new instance. Root cause unfixed — find the lingering thread.
  12. **2026-07-26** — Test gate broken: 32/1289 tests fail at baseline (DrawdownManager ×15, gainhunter sizing, XAUT thermometer, tradfi gates, pyramid). Pre-existing, NOT caused by 07-26 fixes. The "all tests pass before restart" rule is currently unenforceable — fix or prune stale tests.
  13. **2026-07-26** — Trend-day harvest unreachable: basket TP1 stack = 8% trend base × cascade (≤1.5) × depth (≤1.25) × HTF (≤1.2) → up to 18%. Observed portfolio ROE peak ~6.3%. **PARTIALLY RESOLVED 2026-07-28** — TP1 already had a 6% small-account cap; TP2 now capped too (12% small-account), base lowered 12→8%, trend base 20→12%. Remaining: trend-day TP1 base (8%) still exceeds observed peak — cap handles it below $1k.
  14. **2026-07-26** — Dust positions are structurally unclosable: sub-$10 notional and sub-step quantities rejected by exchange; only absorbed by same-direction re-entry (one-way netting). Currently carrying BTC 5e-05 (~$3.22) + ETH 0.0001 dust. Prevent at source: enforce min-notional AND step-multiple on entries so dust never forms.
  15. **2026-07-26** — SOL-USD 2.654 (~$200, entry 75.34) is an UNTRACKED position on the exchange — not in pnl_attribution. Likely filled from a stale GTC maker limit (maker-fallback cancel hole, fixed in 045c118). Needs adoption or manual close. **RESOLVED 2026-07-27** — adopted via untracked_position_synced, closed by software stop.
  16. **2026-07-27** — Live runtime state was git-tracked (logs/calendar.db, funding_history.json, vault.json force-added despite logs/ in .gitignore). Every deploy's `git reset --hard` stomped live state: re-delivered the corrupt calendar.db on 07-27 ("disk image malformed" storm), silently rewound vault watermark + funding history. **FIXED f1e4fe2** — untracked (files kept on disk). Never re-add files the engine rewrites at runtime; on future pulls after untracking, back up live files first — git removes them from the working tree once.
  17. **2026-07-27** — Dual source of truth for the trading universe: server `.env` had a stale `ASSETS=[...]` line that overrode `config.assets` (pydantic-settings) — kept BASED-USD (id 78, delisted) in the universe after code removed it → "symbol not active" leverage rejections every boot. **FIXED** — ASSETS line removed from server .env (backup /tmp/.env.bak-20260727); code is now the single source. Lesson: `.env` is for secrets, not universe config. Same bug class as #16.

## Recent Deployments (continued)
  - **2026-07-27 (pm)** — Log-spam throttle + BASED prune (5447ae0 + server .env)
    - `intelligence/interpreter.py` (5447ae0): insufficient_candles throttled to 1 warning/symbol/5min (was every 5s tick per equity symbol — 10,211 warnings/90min burying real signal). Same rate-limiter idiom as _last_publish_ts.
    - Server `.env`: stale ASSETS override removed (known issue #17) — BASED-USD leverage rejections gone; 32 leverage_set at boot (BTC skipped: open position).
    - Post-restart verified: campaign heartbeat fired + boosted at 10:25:20 unblocked, 0 entry blocks, 0 errors, single process.
  - **2026-07-27** — Consciousness sobering + git state-stomp fix (bbfce92, f1e4fe2)
    - `memory/param_store.py` (bbfce92): clear_ai_param(key) — immediate delete + disk flush. Moods no longer have to wait out their TTL.
    - `main.py` (bbfce92): meta-cognition loop — boot sobering clears meta-cognition-owned params (meta_block_entries, meta_tp_tighten) persisted by a prior process; focused mode releases only keys the pulse itself wrote (_meta_owned set), so ADL's meta_size_mult contraction is never fought by the consciousness organ. Root symptom: stale meta_block_entries survived restart via logs/param_store.json and blocked ALL entries 30+ min — campaign heartbeat fired valid SPCX signals at coherence 3.5 into a wall.
    - Repo hygiene (f1e4fe2): untracked logs/calendar.db, logs/funding_history.json, logs/vault.json (known issue #16).
    - Post-deploy verified: 0 meta_reflex_entry_blocked since boot, campaign trades firing (SPCX short 115.21 → software stop), 0 calendar errors after re-seed, BTC short re-adopted (+$0.47 at check).
  - **2026-07-26 (eve)** — Spine live + cancel-chain root fixes (6a1b52a → 6126ea4)
    - `main.py` (6a1b52a): cybernetic spine — param_store TTL reflexes wire meta-cognition → entries/sizing/TPs (meta_block_entries, meta_size_mult, meta_tp_tighten), funding-carry → sizing (±15%), basket TP1 small-account cap 6%, portfolio trailing lock, trail native-replace throttle (0.25 ATR), XAUT two-tier thermometer.
    - `execution/sodex_client.py` (045c118): cancel chain — numeric orderID + symbolID in replace_stop_order, maker-fallback, _cleanup_orders (reverse symbol_id_map lookup).
    - `execution/sodex_client.py` (5fec167): get_open_orders was blind to the nested API shape ({"data":{"orders":[...]}}) — returned [] forever; every reconciliation loop believed zero open orders while 92 stale stops accumulated.
    - `execution/sodex_client.py` (67710f2): **root cause** — cancel_order JSON key order. Go gateway re-marshals in struct field order (symbolID, orderID); ARIA sent orderID first → signature recovered to garbage address → "API key not found". NO cancel had ever succeeded. Verified vs sodex-tech/sodex-go-sdk-public.
    - `main.py` (ede8a6f): stop-sync crashed on string sides ("SELL") once order visibility was restored; parse BUY/SELL, use stopPrice trigger.
    - `main.py` (6126ea4): stop-sync tighten-only — sync was nullifying the throttled internal trail every cycle by resetting to the lagging exchange stop.
    - Ops: purged 86 stale orders (92 → 6; kept newest 2 per symbol). Post-deploy verified: native_trailing_stop_replaced cancels the old order (first working cancel in ARIA history), meta_reflex_entry_blocked firing (TRUMP/BASED/NEAR), zero position_sync_error.

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
