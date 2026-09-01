# ARIA — Claude Code Context

This file is loaded automatically when you run `claude` inside this project.
Extended architecture, AI Fund Manager spec, and agent details live in `~/kingdom_prompt.md`.

## This Project
- ARIA: autonomous perpetuals trading system on SoDEX mainnet
- Local path: /Users/dayodapper/CascadeProjects/ARIA/
- Server path: /home/dayodapper/ARIA/
- Git remote: https://github.com/ajayi-del/ARIA.git (branch: main)
- Server SSH: gcloud compute ssh aria-prod-v2 --zone=europe-west3-c
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
  4. gcloud compute ssh aria-prod-v2 --zone=europe-west3-c
  5. cd ~/ARIA && git pull && tmux attach -t aria
  6. Restart: Ctrl+C, python3 main.py (only after grep confirms no open positions)
  7. Watch logs for 60s for expected events

## Migration Runbook (if the VM is ever lost)
  ARIA is portable. Irreplaceable state, in order:
    1. GitHub repo (github.com/ajayi-del/ARIA, main) — all code. Safe by design.
    2. Server .env (~/ARIA/.env) — SoDEX wallet/API key, Bybit keys, Telegram token.
       NOT in git (issue #17). Local copy lives only on the server + /tmp/.env.bak-*.
       If VM is dying, scp .env off FIRST.
    3. logs/ runtime state — trade_journal_*.json (permanent, rule #14), journal_archive/,
       param_store.json (learned stop_mults/min_coherence), funding_history.json,
       calendar.db, vault.json. Losing these = losing ARIA's accumulated experience.
    4. ~/aria_watchdog/ (cron: `41 */4 * * * run_cycle.sh`) and ~/kingdom/kingdom_state.json.
  Fresh-host bring-up (~15 min):
    1. Ubuntu 24.04, 2 vCPU/4GB (Hetzner CX22 ~€4/mo is the standing recommendation;
       netcup/OVH acceptable; avoid free tiers for live capital).
    2. git clone → python3.12 venv → pip install -r requirements.txt
    3. Restore .env + logs/ + kingdom/ + watchdog, re-add crontab line.
    4. python3 -m pytest tests/ -q → expect 29F/1279P baseline (#12).
    5. Start via /tmp/aria_restart.sh pattern (setsid nohup). Verify 60s.
    6. Rebind exchange API keys to the new egress IP (Bybit 401 = IP whitelist).
  No inbound ports needed — ARIA is outbound-only. Latency is non-critical (5s cadence).

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
          cat ~/aria_watchdog/proposals.jsonl   (watchdog→local shared memory — see below)
  Step 2: Identify precisely — exact log lines, file + line number, root cause
  Step 3: Propose — git diff format, risk level (low/medium/high)
  Step 4: Wait for approval on high risk
  Step 5: Apply → verify within 60s → rollback if unexpected

## Autonomous Watchdog (server crontab)
  Cron: `41 */4 * * * /home/dayodapper/aria_watchdog/run_cycle.sh` — every 4h (6x daily; measured 2026-08-16 at ~$0.50-0.67/k2.6-cycle → ~$90-120/month, fits the $100 cap; 2h was $180-240).
  Cycle: health check (process, log freshness, exchange vs tracked positions, rejection storms) → writes ~/aria_watchdog/report.md + cycles.log.
  Before any manual restart, read report.md first — it may already have diagnosed the issue.

### Watchdog Operating Contract (the cron claude reads this file too)
  The watchdog is a junior engineer on call, not a strategist. It MAY: diagnose,
  apply minimal crash/typo fixes, commit+push (server-side commits — expect
  non-fast-forward on next local push, pull --rebase), restart a DEAD bot
  (positions live exchange-side; stops re-place at boot — verified pattern).
  It MUST NOT: touch Kant/Nietzsche/Chancellor, leverage caps, universe lists
  (assets/aster_assets/aster_shadow_assets/aster_kline_assets), explosive_* knobs,
  treasury_* / trend_day_* / campaign_* knobs, capacity-governor / mover-radar /
  aster-maker / rally-slot knobs (daily_cap_*, mover_*, aster_maker_*,
  rally_max_graduated_per_direction), or restart a HEALTHY bot with
  open positions. Hard rules #1-15 bind the watchdog too. Full operative
  contract: ~/aria_watchdog/prompt.md (server).

  **Treasury review (2026-08-19, operator directive)**: the watchdog observes
  the Treasury subsystem slowly, like a quant reviewing a new PM — per-cycle
  health (heartbeat presence with ≥2 positions, order_failed storms, zero
  portfolio_basket_tp_loop_error) plus a daily TREASURY REVIEW inside the EV
  scan: harvest census by reason, runaway-trim over-harvest counterfactuals
  (trim mark vs +4h kline, n≥10 before proposing), margin-recycle quality,
  cluster skew, trailing-lock behavior on reversal days, inert-zone regression
  (heartbeats crossing tp1 with zero orders = the old deadlock in new
  clothes), and the disposition-effect metric (winners should drift toward
  longer holds). All treasury tuning is MUST-NOT class — proposals only.

  **EV scan (2026-08-18, operator directive)**: first cycle after 00:00 UTC the
  watchdog runs a daily quant pass. STEP 0: run `.venv/bin/python
  tools/daily_digest.py` (deterministic precompute — works even when the bot is
  down) and read logs/daily_digest.json + daily_digest_history.jsonl; the
  digest computes gate accuracy/tail-cost, per-symbol expectancy + churn flags,
  size-chain chokepoint, silence census, phantom sweep, fee drag, exit-reason
  pareto, hold asymmetry, per-venue entry slippage vs public klines
  (Bybit/Aster/Yahoo), and ARIA-vs-hodl benchmark (weekly: coherence
  calibration + session attribution on Mondays). The watchdog interprets —
  materiality bar: propose leaks >$1/day or >2× size distortion; quiet book
  with accurate gates = healthy, no proposals.

  **Autonomous 24h fix tier (2026-08-18, operator directive)**: a proposal
  carrying auto_tier:true that ages 24h unvetoed may be implemented by the
  watchdog, ALL of: defect re-verified live; whitelist classes only (false-state
  guards, API contract/shape mismatches, crash/typo/dead-wiring, observability —
  always fail-CLOSED, never more permissive); ≤40 lines/≤2 files; MUST-NOT list
  outranks aging absolutely; suite holds 29F baseline; FLAT book on both venues
  via exchange APIs before restart; one auto-tier fix per 24h; commit "watchdog:
  <slug> (24h auto-tier)" + implemented status line + report.md diff.
  Designed events post-2026-08-16 (do NOT "fix" these): explosive_blocked =
  guards working; explosive_fired / explosive_time_stop / explosive_stop_to_breakeven
  / explosive_cleanup = the armed pilot trading on Aster; Aster positions
  coexisting with SoDEX positions; router_v2_heartbeat dual_listed=3 (BTC/ETH/SOL
  shadow-scored on both venues, SoDEX still owns routing); new report files
  logs/venue_snapshots.jsonl, logs/compression_watchlist.json,
  logs/venue_comparison.json; 32 Aster markPrice streams (29 live + 3 shadow).
  Designed events post-2026-08-19 (Treasury era, also do NOT "fix"):
  treasury_activated / treasury_deactivated (cluster ownership transitions),
  treasury_heartbeat every 60s while active, treasury_native_tp_cancelled
  (managed-cluster members only — inactive positions keeping native TPs is
  intended), treasury_ownership_released, treasury_age_expiry, treasury
  exits in position_closed reasons (treasury_tp1 / treasury_tp2 /
  treasury_trail_lock / treasury_runaway_trim / treasury_margin_recycle),
  treasury_exit_spread_blocked during cascades (L4 gate working).
  Autonomous graduation (2026-08-16, also designed — never "fix" these):
  graduation_eval (hourly evidence eval), subsystem_graduated /
  subsystem_lapsed (TTL privilege transitions), router_graduation_routing
  (graduated router_v2 migrating BTC/ETH/SOL routing to Aster while FLAT —
  lapse flips them back), explosive_blocked with graduated=true (wider caps
  are the earned privilege, not a bug).
  Resolved 2026-08-16: the watchdog's 2026-08-15 combined-equity sizing flag —
  sizing now reads the EXECUTING venue's collateral via venue.venue_balances()
  per-venue cache; kingdom/vault/drawdown keep combined.
  Cost guardrail: watchdog model = kimi-k3 by operator directive 2026-08-29
  (k3 since 08-24; a same-morning k2.6 trial was reversed — k3 quality was
  judged worth it, and the real cost lever is the operator's own Claude
  sessions, so autonomy shifts TO the watchdog for a one-week control
  trial as co-founder / quant / system-optimizer). The watchdog has
  writable cross-cycle memory (~/aria_watchdog/memory/) and Telegram
  reporting (@Portfolioriabot; token in server ~/aria_watchdog/telegram.env
  — secrets never in git). Cost doctrine: deterministic precompute (digest
  + snapshot tools) does the arithmetic, the model judges; plan before
  token spend.
  Mission (operator directive 2026-08-29): profitability is the highest
  priority — grow the fund toward $500k by 2026-12-20 on ~$500/month
  operator deposits + reinvested gains (ARIA pays itself). Mission file,
  milestone ladder, and fund-growth canon live in
  ~/aria_watchdog/memory/mission.md on the server.
  Kill switch: touch ~/aria_watchdog/DISABLED.

### Inter-Node Shared Memory (proposals.jsonl, 2026-08-16)
  ~/aria_watchdog/proposals.jsonl is the shared channel between the two LLM
  nodes (server watchdog cron + local Claude sessions). Append-only JSONL:
  {ts, id, title, rationale, evidence, risk, status, node}. Status flow:
  proposed → accepted/rejected → implemented. The watchdog proposes changes
  outside its fix authority; the local node reads the file at session start
  (Step 1), decides, implements, and appends status updates. Neither node
  edits the other's lines — status transitions are new lines, last wins.
  Since 2026-08-19, effect-claiming proposals also carry estimand,
  identification, n, effect_size, comparisons (see watchdog prompt — the
  causal bar alongside the materiality bar).

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
  - **2026-09-01 (latest)** — Sentinel venue-reference repair + momentum inflight guard + ETF tide calendar age (one operator deploy, 3 defects)
    - **Defect 1 — sentinel reference plane (false quarantines)**: the
      mark-scale sentinel compared each symbol's mark against its candle_buffers
      1m close — but for the 17 tradfi_feed-owned symbols those candles are the
      YAHOO UNDERLYING's plane (SPCX→SPY ~767, USTECH100→QQQ ~716), cross-scale
      by construction against the rebased synthetic perp mark (~141/~29361).
      Yahoo is dark weekends (fail-open = silent) and prints at the US open →
      2026-08-31 13:32 UTC false-quarantined SPCX + USTECH100, blocking all
      stock entries while the venue's own planes agreed (live probe: mark
      142.64 vs venue kline 142.72). Worse, the same Yahoo reference would read
      the REAL 08-22 defect (mark 769 vs trade-plane 140, SPY ~765) as IN-BAND
      — broken in both directions. Fix: `_sentinel_venue_ref_symbol` (Yahoo-
      owned AND not in sodex/aster_kline_assets) routes the reference to
      `_venue_kline_1m_close` (SoDEX REST newest 1m close, fail-open None on
      any error); the existing 180s staleness guard binds both paths. The
      sentinel brain is untouched.
    - **Defect 2 — cascade momentum task race (first double-fill in 1,945
      emits)**: N symbol-level cascade events spawn N `_execute_cascade_momentum`
      tasks; all select the same preferred symbol (BTC→ETH→SOL) and the path
      never writes `_pending_entry_symbols` → 2× BTC long 12:41 UTC 2026-08-31
      ($247 vs $124). Fix: `_cascade_momentum_inflight` set keyed on DIRECTION
      (selection is symbol-agnostic until L4 rank), check-and-set at the head
      BEFORE the first await (asyncio-atomic), `discard` in finally on every
      path, `cascade_momentum_inflight_blocked` telemetry.
    - **Defect 3 — ETF tide weekend staleness (opposed-tide leak)**: SoSoValue
      flow rows stamp the trade date at 00:00 UTC, so Friday's print read 77h
      old Monday 05:24 UTC → spurious >72h abstain → tide veto went dark
      exactly when needed (opposed-tide ETH short leaked, the −$27.42 class).
      Fix: `etf_calendar_adjusted_age` subtracts 24h per non-trading date in
      (last_date, now_date] — weekends + NYSE holiday table 2025-2027; dates
      outside the table count weekdays as TRADING (fail-closed, veto stays
      armed); a week-dead feed still abstains. All consumers (flow_size_mult /
      tide_aligned / flow_poll) read the fixed producer. Kill switch
      ETF_TIDE_CALENDAR_AGE_ENABLED (env, default true; false = raw legacy).
    - Suite 2045P+28x+60xp (+10: 7 calendar-age pins incl. the 77h→29.4h
      Monday-morning pin + stale-feed/holiday/unknown-year fail-closed legs;
      1 momentum-guard wiring pin; 2 sentinel venue-reference pins + 3 kline
      parser tests). Verified live: see entry below post-restart.
    - Designed events (do NOT "fix"): cascade_momentum_inflight_blocked,
      sentinel observations reading venue-kline references for Yahoo-owned
      symbols, ETF tide veto ACTIVE on Monday mornings.
  - **2026-08-30 (latest)** — Mark-scale quarantine + phantom-close firewall (Workstream B) + WPP quantity-delta doctrine (d03b3c7, external audit P0s)
    - **The defect**: SoDEX markPrice served SPCX at pre-rebase scale (769.35)
      while klines/entries served ~140 — a persistent 5.48× split. The 08-28
      `_entry_scale_quarantined` registry compares mark vs the CANDIDATE's own
      price, so campaign-heartbeat candidates priced FROM the bad mark were
      invisible to it (3 unprotected brackets manufactured overnight); the
      mark also fed phantom PnL into close accounting.
    - **`intelligence/mark_scale.py`** (NEW, zero-I/O brain): MarkScaleSentinel
      compares each symbol's mark vs its OWN 1m kline close (the independent
      channel). Band [0.70, 1.43], PERSIST_N=3 consecutive 30s observations
      to arm, HEAL_N=3 to heal, counters reset on regime change, invalid
      inputs fail open without touching state. `_mark_scale_sentinel_loop`
      (30s, supervised) publishes `mark_scale_quarantined:{sym}` via
      param_store TTL 1800s (survives restarts; >900s refresh throttle ≤2
      writes/h/symbol). Stale mark (>90s) or stale/missing kline (>180s) =
      NO observation — off-hours equities and warmup never quarantine.
    - **Entry-side**: all 5 entry paths refuse quarantined symbols
      (entry_blocked_mark_scale, shadow gate mark_scale_quarantine —
      counterfactual unmeasurable on a split plane; records exist for the
      block census with a conservatively-biased read, documented in
      shadow_journal.py). Paths: standard, cascade_momentum,
      cascade_aftermath, campaign_heartbeat, explosive.
    - **Close-side firewall**: `_record_close` zeroes mark-derived PnL on
      quarantined symbols (phantom_close_suppressed with raw_pnl) — the
      close is real, the number is ghost; real PnL reconciles through the
      wallet-balance classifier when the venue heals. Skips: time_stop,
      trailing ratchet, pnl_attribution uPnL leg, ADL score. exchange_close
      path deliberately NOT skipped (position really vanished; firewall
      zeroes the number, tracking still drops).
    - **WPP quantity doctrine (audit P0)**: `_diff_one` classifies on
      QUANTITY delta, not notional — a constant-qty hold through a 10% pump
      emits NOTHING (before: ADDED → live ×1.25 consensus boost on zero
      behavior). Notional decomposes into estimated_trade_notional (|Δqty|×
      event px = behavior; the $10k floor applies here) and mtm_change_usd
      (prev qty × Δprice = revaluation). Events carry qty_delta /
      estimated_trade_notional / mtm_change_usd. Pre-boot journal confirmed
      the defect class: ±$12k notional "flows" on flat-quantity positions.
    - Kill switch MARK_SCALE_QUARANTINE_ENABLED (env, default true; false =
      pre-module bit-for-bit).
    - Verified live (boot 23:16 UTC): 0 pane tracebacks, single process,
      SPCX long 1.33 @ 140.88 + CYS long 47.0 @ 0.8453 re-adopted with
      protective stops, treasury_heartbeat post-boot, 0 loop errors incl.
      mark_scale_sentinel. Sentinel correctly SILENT — SPCX mark read
      ~140.6 = kline scale at boot (split healed/dormant exchange-side);
      organic proof pending the next live split (watchdog watches for
      mark_scale_quarantine_armed). Suite 2035P+28x+60xp (+17).
    - Ops note: operator withdrew $15 pre-restart; unclassified (no clean
      poll) → ~1.7% phantom DD read, below the 3% recovery trigger, washes
      out at next anchor adjust.
    - Designed events (do NOT "fix"): mark_scale_quarantine_armed/healed,
      entry_blocked_mark_scale (all 5 path= values),
      phantom_close_suppressed, mark_scale_sentinel_loop_error, whale
      flow events carrying qty_delta/estimated_trade_notional/mtm_change_usd.
  - **2026-08-30** — Whale evidence stack: WPP position plane + TAC ladder + WAS shadow (2b8508d, operator directive "build test and ship" + 10-point spec audit)
    - **WPP (data/whale_positions.py)** — the DaVinci bypass, live: Aster RPC
      `aster_getBalance` (tapi.asterdex.com) + Hyperliquid clearinghouseState
      polled for the whole registry (same EVM address space, both venues).
      Delta engine emits WhaleMirror-contract flows ingested via
      `WhaleMirror.ingest_flows` (dedup by contract key) — the dark Aster
      inferred leg upgrades to DIRECT without touching mirror doctrines.
      Aged bags silent (Hasbrouck); opened_at_confidence high only on
      observed 0→nonzero; margin/leverage native-only (never derived).
      Verified live: journal carries 0xE1d71a BTC long 44.835 @ $3.5M and
      0xb79C809 BTC long 15.36 @ $1.2M — real mainnet account data.
    - **TAC (intelligence/tide_consensus.py)** — the audit's core directive
      (evidence → distribution → EV → risk, NOT evidence → multiplier):
      bounded placeholder ladder 1.00/1.05/1.15/1.25 replaces the legacy
      ×1.25/×1.5 wallet-count boost, over EFFECTIVE breadth (40% leviathan
      cap + venue-cluster inverse-HHI √ deflation — correlated whales are
      one risk factor). ETF tide amplifies strong consensus or abstains the
      boost when opposed (never vetoes). tide_consensus_enabled=false =
      legacy ladder bit-for-bit. sizing_chain carries tac_rung/tac_breadth.
      Verified live: tide_consensus_verdict BTC short abstain_opposed_tide.
    - **WAS (intelligence/whale_absorption.py)** — SHADOW-ONLY: forced liq
      window (EXPANSION/EXHAUSTION + |z|≥2.5) × whale identity flows × L4
      wall refill × 5-min post-event stabilization (knife check 0.4%).
      TRUE (identity present) vs FOOTPRINT-ONLY classes; absorption_ratio
      and impact_efficiency are FEATURES (no hard thresholds); thesis
      half-life metadata (0-15m full / 15-60m decay / 60-180m reduced /
      >180m stale). Shadow gate "whale_absorption". ZERO live orders until
      graduation (n≥50 AND EV>+0.15R AND CI>0 AND PF>1.15 AND OOS).
    - **Evidence layer (intelligence/whale_evidence.py)** — SignalEvidence
      dataclass; four-confidence GEOMETRIC mean (one zero leg zeroes all);
      EV-from-samples with k=20 shrinkage → None before data (abstain, no
      fake precision). Whale score is a FEATURE VECTOR, not a confidence.
    - Kill switches: tide_consensus_enabled, whale_positions_enabled (data
      plane), whale_absorption_enabled (shadow accrual) — all default True.
    - Verified live (boot 07:29 UTC): 0 pane tracebacks, single process,
      SOL short 1.827 + UNI long 7.0 re-adopted with protective stops,
      treasury_heartbeat post-boot, 0 whale_positions/whale_absorption loop
      errors, whale_positions.jsonl writing real snapshots. Suite
      2013P+28x+60xp (+76).
    - Designed events (do NOT "fix"): tide_consensus_verdict,
      whale_positions_flows_ingested, whale_absorption_candidate,
      whale_mirror_candidate with venue=aster quality=direct (WPP leg),
      shadow records gate=whale_absorption.
  - **2026-08-30** — ETF tide veto on explosive + whale-probe entry paths (53f246c, operator directive "audit aster trade path and ensure it's correctly blocked")
    - **Audit matrix** (6 pre-existing sites OK): cascade momentum :2751
      STRICT, cascade aftermath :3319 STRICT + :3514 ×0.5 haircut, standard
      path :6778 post-Skeptic, aster_swing adds :11570 abstain, probe runner
      conversion :15406 abstain. **Two holes found and wired**: (1) explosive
      executor (long-only MARKET IOC — zero tide check before leverage set)
      → `explosive_blocked reason=etf_tide`; (2) whale probe ENTRY (50x on
      BTC/ETH/SOL only — exactly the symbols with tide data — while only the
      runner abstained) → `whale_probe_blocked reason=etf_tide` before the
      50x leverage set. Both use the established idiom (_etf_flow +
      tide_aligned == "opposed", etf_tide_veto_enabled kill switch, stale
      >72h abstains neutral); alts abstain neutral (no tide data).
    - Verified live (boot 06:17 UTC): 0 pane tracebacks, single process,
      SOL short 1.827 re-adopted with stop 106.849 (order 18953466781),
      pnl_attribution post-boot, SPCX long closed cleanly pre-restart
      (time_stop_loser_momentum_cont_120min −0.016R, journaled). Suite
      1937P+28x+60xp (+2 wiring pins: veto ordered before leverage set).
    - Designed events (do NOT "fix"): explosive_blocked with reason=etf_tide,
      whale_probe_blocked with reason=etf_tide.
  - **2026-08-30** — Size-sync booking: native-fill partial close + dust purge + stop resize (58676c0, operator directive "snip abc" after the SOL 0.11 autopsy)
    - **The incident**: SOL short 1.215 ($128, healthy sizing chain) — native
      merged-TP (TP2/TP3 dust-merged under the $80 floor into TP1) filled
      1.214 @ 105.13 at 01:08 (+$0.41 realized, exchange-verified). The
      reconciliation silent-adopt never journaled the win (beliefs layer
      blind), software-TP looped close failures on the unclosable 0.001
      ($0.11 — the number the operator saw on the UI) for 77min, and a
      1.215-sized native stop sat on the dust position.
    - **classify_size_sync** (pure, module-level): every tracked-vs-exchange
      divergence routes to none / grow (silent adopt, legacy bit-for-bit) /
      shrink_silent (no mark or entry=0 — legacy adopt) / shrink_book
      (_record_partial_close the closed fraction + resize the resting native
      stop to the synced size) / shrink_purge (sub-$10 SoDEX remnant —
      partial booked, _record_close purges dust AND cancels the oversize
      stop via _schedule_cancel_resting; Aster remnants stay book —
      closePosition has no notional floor).
    - **Ops folded in**: two stale SOL stops cancelled live pre-restart
      (18908233932 @1.215 + 18933186143 @0.913); the 0.001 dust was absorbed
      by the designed one-way-netting re-entry (03:34 cascade_momentum
      $192 → 1.827 tracked). SOL step is 0.001 (sodex_client:138) — the
      sub-STEP purge in the TP loop can never fire for SOL dust; the
      sub-NOTIONAL purge at the sync site is the catch.
    - **Leverage question answered with journal data** (operator: "all trades
      10x"): ladder intact — 08-29 SPCX 8x COIL (TREND base), HYPE 7x FLOW
      (SCALP 6 + personality 1); the 10x skew is cascade_momentum forcing
      TREND+2 by design, and momentum has been the dominant executor.
    - Verified live (boot 03:49 UTC): 0 pane tracebacks, single process,
      SPCX long 1.33 + SOL short 1.827 re-adopted with fresh stops
      (106.849 / 138.6018), treasury heartbeat managing both, 0 post-boot
      loop errors. Suite 1935P+28x+60xp (+10 test_size_sync_booking).
    - Designed events (do NOT "fix"): position_size_synced with verdict=,
      partial_close_recorded with reason exchange_fill_adopted,
      sync_dust_purged, sync_stop_resized / sync_stop_resize_failed.
  - **2026-08-29 (latest)** — Venue-aware dynamic floor + aster conviction base 0.75 (9aaf890, operator directive "9usd is not efficient margin use... that 80 usd cap is a bug it should be dynamic and grow with account")
    - **HYPE sizing-chain autopsy**: sizing_chain $421 → risk-parity $180 →
      Nietzsche basket cap 0.35 × aster ladder base (sleeve/2 = $179.66) =
      $62.88 BINDING → 0.75 HYPE @ 7x = **$9 margin (2.5% sleeve util)**.
      Two structural defects: (1) the $80 SoDEX floor sat mid-chain and was
      venue-wrong on Aster ($1 exchange min) — UNI long passed every gate,
      basket cap shrank it to $69.06, nietzsche_min_notional_fail killed it
      UNSCORED; (2) ladder 1.0-conviction base hardcoded cap/2.
    - **Floor**: `_venue_min_notional` = max(venue_min, sleeve × 2%) — aster
      min $3 (3 bracket legs × $1), SoDEX $80 unchanged at current book,
      grows past $4k ("grows with account"). Wired at all 4 consumers:
      Nietzsche call site, build_candidate SoDEX init, aster ladder,
      Kelly branch. Kill switch FLOOR_VENUE_AWARE_ENABLED (env, default
      true; false = flat $80 legacy bit-for-bit).
    - **Ladder**: `aster_conviction_base_frac` 0.75 (was cap/2) — +50%
      standard aster size (HYPE-class fill $62.5 → ~$94 notional); 2.0
      conviction still hits the cap, never exceeds (Vince). 0.5 = legacy.
    - **Observability** (kills the silent-multiplier gap): nietzsche_output
      gains notional_usd + floor_used; nietzsche_min_notional_fail gains
      direction + notional_usd + actual floor, shadow-registered as gate
      `min_notional` (counterfactually scored from birth); bracket_placed
      gains size + notional_usd.
    - Verified live (boot 17:50 UTC): 0 pane tracebacks, single process,
      both venues FLAT pre-restart (exchange APIs — HYPE closed ~breakeven
      pre-deploy), pnl_attribution post-boot, sizing_chains flowing (SPCX
      campaign path), 0 post-boot loop errors. Suite 1925P+28x+60xp (+19:
      18 test_venue_floor + legacy-ladder equivalence pin; the Fix B conv1
      pin re-encoded for the 0.75 doctrine with justification).
    - Designed events (do NOT "fix"): nietzsche_output with floor_used,
      bracket_placed with size/notional_usd, shadow records gate
      min_notional.
  - **2026-08-29** — Beliefs-layer repair: phantom filter + direction conditioning + skeptic decay (71c97ad + c375d42, operator directive "ultrathink and wire this in with proper engineering")
    - **The corruption (3 layers, proven from production data)**: (1) phantom
      SPCX closes — bimodal census: 561 real closes ALL <$5 pnl vs 4 unique
      ghosts ALL >$100 (+$1,576 fake, 807 cross-file dupes made it look like
      64); the 08-24 date-bound purge caught the personality stats but the
      journal read-path stayed exposed. (2) Direction blindness — ETH longs
      19 @ 100% WR pooled with shorts 86 @ 17% WR into ONE belief that
      throttled both (symbol_edge pooled ETH at 0.50×). (3) Regime-window
      contamination — rally-week shadow records voting at full weight weeks
      later in skeptic base rates.
    - **Repair**: `is_phantom_record` generalized to ANY-date SPCX |pnl|>$100
      (the $100 threshold separates the bimodal clusters exactly), moved to
      trade_journal, filtered at `get_closed()` read-path (journals permanent,
      rule #14); `get_symbol_edge(symbol, journal, direction=)` — fail-OPEN
      to 1.0 on thin same-direction sample, never falls back to the poisoned
      pool; `skeptic.base_rate(direction=)` + recency decay 0.5^(age/14d)
      with weighted shrinkage blend and effective-n for VETO_MIN_N. All 7
      call sites wired (5 symbol_edge incl. time-stop hold-bias via position
      side; 2 skeptic).
    - **Kill switches** (False = legacy bit-for-bit): JOURNAL_PHANTOM_FILTER_
      ENABLED, SYMBOL_EDGE_DIRECTION_ENABLED, SKEPTIC_DIRECTION_ENABLED,
      SKEPTIC_DECAY_HALFLIFE_DAYS (0 = decay off).
    - **tools/beliefs_audit.py** (verification-before-activation, run on
      server pre-restart): phantom census + non-SPCX large-pnl suspects,
      per-symbol pooled-vs-split edge diff, skeptic 4-config veto flips,
      agent winrates stored vs phantom-filtered recompute →
      logs/beliefs_audit.json. Live diff: ETH long freed 0.50×→1.00× (short
      stays 0.50×), BTC long 0.90→1.00, SPCX short freed (stocks covered),
      XAUT short freed, CL-USD short veto FLIPS off (0.29→0.37 dir+decay).
    - **Agent-drawdown sizing audit (operator question)**: `_win_rate_band`
      has a hard 25% floor — personality stats cap but never block; k=20
      shrinkage toward 0.5 protects small-n; only AFTERMATH was phantom-
      corrupted (repaired 08-24). Kingdom bets verified clean (live signal
      registrations, no phantom-driven chancellor blocks).
    - Verified live (boot 16:43 UTC): 0 pane tracebacks, single process,
      both venues FLAT pre-restart (exchange APIs), performance_restored
      phantoms_skipped=4 dupes_skipped=807, config_sizing 600/750, main loop
      processing signals post-boot, 0 post-boot loop errors. Suite
      1906P+28x+60xp (+17 new pins; 2 stale date-bound phantom pins
      re-encoded with justification).
    - Designed events (do NOT "fix"): symbol_edge_applied with dir= in
      reason, skeptic_base_rate with decay-shrunk n, performance_restored
      phantoms_skipped ≥ 0.
  - **2026-08-29** — Recovery trend-day exemption + gate-economics cadence (862b251 + gate tool, operator directives)
    - **Refused-trades audit** (14,075 shadow-scored refusals): gate stack net
      +3,205% (defense works) — but recovery_skip netted **-912%** (n=1321:
      avoided +305% over 1,121 saved losers ≈0.27%/trade vs missed +1,217%
      over 200 missed winners ≈6%/trade) and **199/200 missed winners were
      trend-day-aligned** (VELVET +84%, TRUMP +46%, ZEC +44%). Disposition-
      effect geometry: the 5.6 recovery floor sold the right tail to avoid
      the chop. Dollar estimate at current sizing: ~$200-400 over 3 weeks.
    - **Exemption (862b251)**: `recovery_trend_exempt` in day_type_classifier
      — aligned `_trend_day_verdict` waives the recovery coherence floor ONLY;
      the 0.5× size cap and 0.8 TP factor still bind (half-size participation,
      not offense). counter/unknown fail closed. Kill switch
      RECOVERY_TREND_DAY_EXEMPT_ENABLED (default true); throttled
      recovery_trend_day_exempted event (300s/symbol).
    - **Gate-economics cadence (operator directive)**: tools/gate_economics.py
      (stdlib, standalone) — 3d/7d/all gate-value rollups from
      shadow_scored.jsonl → logs/gate_economics_{window}.json + CSV
      spreadsheets + history jsonl; recalibration flags (n≥30 both windows,
      net<0 both, ≥2× tail asymmetry; disable_candidate on <60% accuracy).
      Watchdog runs it on a 3d/7d cadence; recalibration authority bounded
      per prompt.md (single bounded constant, one/gate/72h, MUST-NOT outranks).
    - Verified live (boot 11:37 UTC): 0 pane tracebacks, single process,
      both venues FLAT pre-restart (exchange APIs), pnl_attribution flowing,
      sizing 600/750, zero post-boot loop errors. Suite 1889P+28x+60xp (+17).
    - Designed events (do NOT "fix"): recovery_trend_day_exempted.
  - **2026-08-29 (late)** — ETF tide veto: opposed-tide entries blocked on journal evidence (85b81df, operator directive "finish this fix and redeploy")
    - **The evidence** (journal × SoSoValue backfill, 251 majors trades,
      07-30→08-28): aligned-tide entries 11/11 (+$8.52); neutral 47% WR
      (-$44.95); OPPOSED 27% WR / avg -$0.26 (n=110, -$28.36 — ETH
      momentum_cont shorts into +$488-607M 3d inflows were the leak).
      Counterfactual "skip opposed": +$28.36 (~4%/mo from one filter).
    - Chan/Thorp: a negative-expectancy class gets size ZERO (same doctrine
      as base_rate_veto). Standard path: veto post-Skeptic, Hugo-aligned
      trend days downgrade to the trend_offensive discount (daily-lagged
      tide is regime-stale — trend_offensive_tide_veto_downgraded).
      Cascade momentum + aftermath guard loops: STRICT veto, no overrides;
      the aftermath ×0.5 haircut stays as the degradation ladder when the
      veto is off. Shadow gate "etf_tide" — the veto's own cost is scored
      from birth. Kill switch etf_tide_veto_enabled (default true).
    - **Watchdog handover (operator directives)**: one-week control trial
      as co-founder/quant/optimizer via k3; writable memory
      (~/aria_watchdog/memory/: mission/canon/handover/economics +
      MEMORY.md); Telegram reporting @Portfolioriabot (token in server
      ~/aria_watchdog/telegram.env chmod 600 — reports + quick questions
      ONLY, never a control channel); mission = profitability first,
      $500k-by-2026-12-20 north star on $500/mo deposits (honest math in
      mission.md — the ladder binds, not the star); prop-firm purchase is
      OPERATOR-ONLY (watchdog prepares evidence, never buys); polymath
      canon weekly book loop. prompt.md amended (bak-20260829-handover).
    - Verified live (boot 10:35 UTC): 0 pane tracebacks, single process,
      both venues FLAT pre-restart (exchange APIs), pnl_attribution
      post-boot, tide tilt live (ETH short ×0.9 vs +$529M, age 32.9h),
      0 veto fires yet (correct — few post-boot majors signals). Suite
      1872P+28x+60xp (+2 wiring pins).
  - **2026-08-29 (eve)** — SoSoValue ETF-flow tide gauge: sizing tilt + duration-class gates + Chancellor poll (f12e853, operator directives: touches all points, sharper offense, watchdog first-class consumer)
    - **data/sosovalue_feed.py** (NEW, data department): supervised daily
      fetcher — `/etfs/summary-history` BTC/ETH/SOL in the 06/22 UTC windows
      (≤6 calls/day) + `/macro/events` once daily (1 call) ≈ 2.7% of the
      10k/mo demo budget with the 2-call/day LLM reserve. Atomic cache
      (logs/sosovalue_flows.json + sosovalue_macro.json) + append-only
      history; one-bad-line doctrine. Pure brains: flow_verdict (last/3d
      sum/signed streak/net assets), flow_size_mult (bounded ±10%,
      $150M 3d materiality, staleness decay 36h → abstain 72h — the
      professional dead-feed fallback), tide_aligned, flow_poll
      (flow×price divergence quadrants: accumulation/distribution/
      confirmed_risk_on/off), macro_due_today.
    - **Live consumers** (all bounded, all shadow-scored, kill switches
      sosovalue_enabled / etf_flow_sizing_enabled /
      etf_aftermath_haircut_enabled — false = pre-module bit-for-bit):
      sizing-chain ±10% tide tilt on the majors STACKING with the whale
      boost (etf_flow_size_tilt; sizing_chain carries etf_3d/streak/mult);
      cascade-aftermath opposed-tide ×0.5 haircut; whale-probe runner
      conversion abstains on opposed tide (long-duration margin is never
      committed against the institutional tide — the margin-efficiency
      doctrine); aster_swing pyramid adds same abstain; Chancellor
      veto/clamp events carry flow_posture (telemetry ONLY — engine
      untouched, rule #2).
    - **Shadow journal**: every record now carries etf_3d / etf_streak /
      etf_age_h / macro_today cohort context — gate accuracy slices by
      flow regime from birth (the self-tuning rail: cohort expectancy →
      digest → watchdog proposals with n + effect size; nothing
      auto-retunes).
    - **Watchdog plane**: tools/soso_snapshot.py (one fetcher, many
      readers — serves the cached snapshot every cycle, spends API only
      on >30h stale top-up when the bot is down); prompt.md amended
      (bak-20260829-sosovalue): deviation-detection mandate (ARIA acting
      against the tide without hedge reason = flag), feed-health watch,
      designed-events list.
    - Verified live (boot 08:31 UTC): 0 pane tracebacks, single process
      (PID 442811), both venues FLAT pre-restart (exchange APIs),
      pnl_attribution flowing, deploy seed (4 calls) confirmed live data:
      BTC 3d +$272.6M (last −$201.8M Friday), ETH 3d +$529.0M streak +5,
      SOL below materiality (neutral). First bot-owned fetch 22:00 UTC;
      macro calendar lands 06:00 UTC 08-30 — dark until then is the
      designed fail-closed abstain. Suite 1867P+28x+60xp (+23).
    - Not built (proposal class): market-overview endpoint (404 on this
      plan), crypto-stocks module (universe overlap research first),
      macro-calendar → calendar-gate enrichment (measure CPI-day trading
      first), eviction tie-break by tide (shadow first).
  - **2026-08-29 (pm)** — Aster-wiring audit fixes: leverage cache + probe fail-closed + aftermath quarantine (7fce3c0, 2-agent audit, operator directive)
    - **CRITICAL (never fired live)**: `_leverage_set` was membership-only —
      `update_leverage_with_fallback` short-circuited forever after first set.
      The whale probe would have sized for 50x while the exchange sat at 10x
      (5× margin usage), and the finally-restore was a no-op. Cache now maps
      symbol→confirmed value; short-circuit only when target == cached;
      fallback chain records the ACTUAL leverage. 2 pins in
      test_aster_client.TestLeverageCache.
    - **Probe stop fail-closed**: native stop rejection on a 50x probe now
      retries 3× then closes at market and stands down (the 60s monitor tick
      cannot guard that leverage class). whale_probe_stop_failed /
      whale_probe_emergency_close_failed (P0) events.
    - **Aftermath scale-quarantine asymmetry**: cascade aftermath symbol
      filter skipped `_entry_scale_quarantined` (momentum had it) — binds
      both executors now.
    - Cascade audit findings (reported, NOT changed — doctrine calls):
      soft WR-recovery does not gate the cascade fast paths (only the
      standard-path aftermath prime suppresses); risk-parity does not bind
      cascades (design); explosive path honors none of the 4 re-entry
      registries (Workstream C still unbuilt — unified reentry guard).
    - Watchdog prompt amended (bak-20260829-cascade): CASCADE-PATH CRITICAL
      AUDIT — guard-chain integrity, fastpath journaling, venue symmetry,
      sizing bounds, recovery doctrine census, leverage integrity.
    - **Aster leaderboard is now AUTH-GATED** (research): the public bapi
      campaign endpoint still answers but empty (deliberate darkening);
      `/bapi/futures/v1/private/campaign/trade/pro/leaderboard` exists and
      401s without a web-session JWT. fapi v3 EIP-712 signature does NOT
      unlock it. Options: operator manual relay (live), SIWE-login
      automation (untested), bridge-deposit on-chain leg (buildable).
    - Verified live (boot 07:44 UTC): 0 tracebacks, single process, flat
      book pre-restart, pnl_attribution flowing, sizing 600/750. Suite
      1844P+28x+60xp (+2).
  - **2026-08-29 (am)** — env-sizing-override cleanup + probe margin fix (2d4fd4e, watchdog proposal accepted)
    - Watchdog cycle-2 key finding: server `.env` (mtime 2026-08-15) carried
      BASE_TRADE_USD=200 / MIN_TRADE_USD=80 / MAX_TRADE_USD=300 /
      MIN_TRADE_NOTIONAL_USD=80 — pydantic env>code binding silently killed
      the 0e7e455 3× step-up on the SoDEX path since 08-24 (the "verified
      live" boot only exercised the Aster path). 4 lines commented out in
      server .env (backup /tmp/.env.bak-20260829-sizing); code values now
      bind: config_sizing_loaded base=600/max=750 verified post-boot.
      Issue #17 class — second occurrence (.env = secrets, not config).
    - whale_probe_margin_pct 0.05 → 0.10 (4277007 documented 10% of aster
      sleeve ≈ $30 on the $600 book; knob landed at 0.05 — one-line fix to
      the operator-approved value; floor $15/cap $50 unchanged).
    - Verified live (boot 06:27 UTC): 0 pane tracebacks, single process,
      both venues FLAT pre-restart (exchange APIs), config_sizing_loaded
      600/750, pnl_attribution flowing. Suite 1842P+28x+60xp.
  - **2026-08-29** — Deploy 5: whale-mirror subsystem LIVE (4277007, operator directives: live day one, size differentiator, 110% runner, pyramid+conviction wiring)
    - **data/whale_feed.py** (NEW): SoDEX positions poller (60s, DIRECT leg —
      signed-size diffs, direction certain) + Aster leaderboard poller (300s,
      INFERRED leg — sign(Δpnl) vs sign(Δprice), churn abstains below $50).
      Campaign-dark detection: the pro campaign went dark 2026-08-29 (empty
      all periods) → leg abstains + whale_feed_campaign_dark (never trade a
      dark data plane). Snapshots → logs/whale_snapshots.jsonl (append-only).
    - **intelligence/whale_mirror.py** (NEW, zero-I/O brain): snapshot-diff
      classifier (opened/added/trimmed/closed/flipped; aged bags silent —
      Hasbrouck), Grinold-Kahn consensus (distinct addresses, 30-min window),
      has_direct_flow (the single-boost gate), reversal_flows (O'Hara PIN:
      DIRECT-leg CLOSED/FLIPPED against the held side; FLIPPED carries the
      NEW side), whale_probe_bracket (Thorp/Vince: risk = notional × stop,
      INVARIANT under leverage).
    - **LIVE wires** (all bounded, all shadow-scored from birth): size boost
      ×1.25 single direct / ×1.5 consensus ≥2 AFTER risk-parity in the sizing
      chain (whale_mirror_size_boost, sizing_chain carries whale_n/whale_mult);
      aster_swing pyramid ADD boost (same steps — whale_mirror_pyramid_boost);
      conviction-review thesis support (fresh direct whale agreement = informed
      same-direction signal — kills the 30-min-clock abandons on whale-
      confirmed names; whale_conviction_support_enabled).
    - **50x consensus probe** (n≥2 only, BTC/ETH/SOL only, 1 concurrent, cap
      3/day): margin = 10% aster sleeve (floor $15 cap $50 → ~$30 on the $600
      book, stop risk ≈1.6%), leverage set→entry→RESTORED in finally
      (leverage-race guard — watchdog alerts on stray 50x), native stop 0.6%,
      TP1 0.8% banks half + stop→breakeven, TP2 1.2% → RUNNER conversion
      (bank half, trailing 2.5%, NO time-stop) while consensus alive —
      the 110% mechanism (whales hold weeks at 20-75x); 15-min time-stop
      pre-runner only (Hasbrouck: ignition decays). Runner exits: trail or
      direct-leg whale exit (whale_probe_runner_whale_exit).
    - **Reversal harvest** (exit side): direct-leg whale closing the side we
      hold + ROE ≥1.5% → bank 50% via _close_with_retry + _record_partial_close
      (whale_mirror_reversal_harvest), once per position, venue-aware dust floor.
    - Registry (5 whales, operator-supplied portfolios): 0xb79C80a5…,
      0xE1d71a…(770k BTC/ETH 50x), 0xb79C809…(86k BTC 75x),
      0x4ea29D…(146k ETH 50x — fresh entrant 2026-08-29),
      0xc8F703…(118k ENA 15x +486%) + SoDEX 0xefe127…. Poll set 19 symbols
      (majors + DOGE/TRUMP/NEAR/AKE + liquid alts).
    - Verified live (boot 01:11 UTC): 0 tracebacks, single process, book FLAT
      both venues pre-restart, sizing_chain whale_n/whale_mult live,
      whale_snapshots.jsonl writing, whale_feed_campaign_dark detected (correct
      abstain), 0 whale_mirror_loop_error. Suite 1842P+28x+60xp (+24).
    - Watchdog prompt amended (bak-20260829): whale health + comparative
      review (left-on-table metric, restrategize vs ARIA) + 4-day fix-audit
      settle-checks + silent-gate hunt.
    - Designed events (do NOT "fix"): whale_mirror_candidate, whale_mirror_
      size_boost, whale_mirror_pyramid_boost, whale_probe_fired/_blocked/_tp1/
      _runner_armed/_time_stop/_closed/_cleanup/_runner_whale_exit,
      whale_mirror_reversal_harvest, whale_feed_campaign_dark/_live,
      conviction_decay_deferred with whale_support=true.
    - **Forensic grounding (operator portfolios vs ARIA journal 08-25→29)**:
      whales long majors from 64k BTC/1.9k ETH held +230→+973% ROE through
      −3% days; ARIA same window: 60+ majors trades, median hold ~10 min, max
      win $2.44, shorts churned INTO whale accumulation. The edge gap is the
      HOLD, not the entry — hence runner/harvest/conviction-support wires.
    - Deferred: ValueChain whale leg (brain is venue-agnostic — flows carry
      venue tags; source identification pending); Aster DIRECT positions
      endpoint (operator's portfolio tool sees live positions — if it has an
      API, the Aster leg upgrades from inferred to direct, campaign-
      independent); margin-as-ALM (Aster Multi-Asset Mode BNB 95%/$ASTER 80%
      collateral + 5% fee token — treasury North Star, not built).
  - **2026-08-29** — Deploy 4: day-move provider extraction (6661490, pure refactor)
    - `data/day_move_provider.py` (NEW): ONE measurement plane for
      from-midnight moves — anchored day_move_elapsed, trend_day_move_pct,
      crypto_day_moves (60s memo), dg_symbol_evidence. Injected buffer
      accessors + clocks (zero I/O); owns the midnight-anchor cache (boot seed
      writes through). The 5 pure helpers (day_move_elapsed_anchored,
      sigma_from_closes, vol_ratio_from_volumes, rank_pctile,
      _alt_breadth_vote) moved with it; main re-exports for legacy importers.
      Doctrines/thresholds stay in their owning brains — this measures, never
      decides.
    - main() closures delegate; all ~15 call sites untouched. No kill switch
      (pure refactor) — 9 bit-for-bit pins in tests/test_day_move_provider.py.
    - Verified live (boot 00:09 UTC): 0 tracebacks, single process,
      midnight_anchor_seeded n=0 (correct — fresh day, buffers reach midnight),
      dispersion_hugo_aligned_exempt fired (NEAR long), counter-trend guard
      reading true day moves (-0.28% fresh day). Suite 1818P+28x+60xp (+9).
  - **2026-08-28** — Midnight-anchor day moves + Hugo dispersion exemption + frozen-mark park + zero-entry guard (0196fe3, ultrathink operator directive)
    - **Deploy 0 REFUTED by production data**: the 82.6% stop-rejection rate was
      stale — all 72 "stopPrice is invalid" rejections in the 35-day log occurred
      on 2026-07-25 only; zero since. stop_widened_min_distance (7-21×/day) is
      the prophylactic working. TP first-attempt enforcement stood down (would
      re-introduce the widening deliberately removed 08-22; retry has fired
      zero times in August).
    - **Day-move truncation (the systemic finding)**: CandleBuffer default
      maxlen=200 (data/candle_buffer.py:16) — after 03:20 UTC the true 00:00
      bar falls off and every from-midnight measurement silently read a
      trailing 3.33h window (live: TAO -0.15% measured at 21:46 UTC vs -6.71%
      true, Bybit daily kline). Blinded Hugo's trend-day lock, dispersion
      self-move legs, capacity day_move_aligned, swing FOMO guard. Module-level
      `day_move_elapsed_anchored` caches the true day-open while visible (≤30min
      tolerance) and serves it all UTC day; Bybit daily-kline boot seed
      (midnight_anchor_seeded, n=57 verified) repairs post-03:20 boots.
      Fail-open: no anchor → legacy truncated read.
    - **Hugo-aligned dispersion exemption** (:5046): on a locked trend day the
      beta trade IS the trade — TAO short (coh 5.9, aster-recovery-exempt) was
      vetoed by the range-day "low alt dispersion" doctrine while 496 aligned
      Hugo boosts fired with zero executions. Kill switch
      DISPERSION_HUGO_ALIGNED_EXEMPT_ENABLED; all other gates still apply.
    - **Frozen-mark protective park (operator root cause)**: equity marks freeze
      when the underlying closes; SoDEX validates TP distance against the frozen
      mark → permanent failure (SPCX weekend 07-25/26: 8 fails, TPs naked 15h).
      Failed retries park on a 900s cadence while mark age >15min and place on
      the first fresh mark (96-park ≈24h cap); software guardian owns the
      position meanwhile. Fresh-mark failures still critical-log instantly.
    - **Zero-entry guard** (sodex_client._place_native_stop_order): entry≤0 made
      the short-side stop sign check vacuously pass; place_protective_orders had
      no upstream guard.
    - **Signal-death funnel measured** (15:00-22:35 UTC): 3,565 signal_ready →
      throttle ×1,318 / coherence_tier ×903 / dispersion ×801 → 426 sizing_chain
      → 9 kant_frame → 0 execution_decision. Signal coherence tonight: n=1055,
      mean 3.73, only 9.8% ≥5.6 — the recovery floor blocks ~90% of production.
      Rally graduation = churn machine: 6,237 grants / 6,237 revocations (80%
      "noise"), privilege consumed 154× (2.5%).
    - Verified live (boot 23:37 UTC): 0 pane tracebacks, single process, book
      FLAT both venues (exchange API), midnight_anchor_seeded n=57, 0
      balance_monitor_loop_error post-boot, Hugo trend_day_aligned_boost ×10 in
      3 min, VIRTUAL long refused counter-trend at day_move_pct -4.84 (the
      repaired reading). Suite 1809P+28x+60xp (baseline 1801 + 8 new).
    - Designed events (do NOT "fix"): midnight_anchor_seeded,
      dispersion_hugo_aligned_exempt, protective_orders_parked_frozen_mark,
      protective_orders_park_succeeded / _park_released, zero_entry_guard
      rejections.
  - **2026-08-27** — Phantom-DD netting + venue-decoupled recovery + symbol-local mover doctrine (a9d85fb + 1424fde, operator directive)
    - **Root cause of the recurring "missing money"**: Dayo's repeated SoDEX
      withdrawals (API-credit funding) never classified as external flows — the
      flat-book branch was blocked by structurally-unclosable $0.25 ETH dust,
      and the open-book wb branch permanently missed any withdrawal sharing a
      30s poll window with a close (the wb anchor advanced every poll
      regardless of veto). Withdrawals booked as drawdown → DD latched ~10% →
      recovery (0.5× cap + 5.6 floor) suppressed the book overnight.
    - **Netted classification** (accounting identity Δwb = realized + funding +
      external): `_close_realized_pnl` accumulates alongside
      `_close_event_counter`; `classify_external_flow_netted(wb_delta,
      realized_in_window)` isolates the external leg — a pure-loss window nets
      to ~0 and stays fail-closed. Dust-aware flatness via
      `_has_actionable_position` (notional ≥ venue close min: aster $1 / else
      $10). DD reset consumed 16:40 UTC (peak 709.98, mult 1.0); post-restart
      recovery events: ZERO (was 251 applied + constant skips).
    - **Venue-decoupled recovery** ("a withdrawal on SoDEX should not affect
      trades on Aster"): DD-reason recovery exempt for aster-routed candidates
      (sleeve self-governs via 30% session halt); WR-reason stays global
      (strategy evidence). `_recovery_params_for(symbol)` closure; 4 swaps
      incl. `_hugo_sym_aligned`. Kill switch ASTER_RECOVERY_EXEMPT_ENABLED.
    - **Alt-rally blindness fixed (the systemic finding)**: every participation
      path was gated on market-WIDE conditions while opportunity was
      SYMBOL-LOCAL. Dispersion gate gains the self-move exemption — three
      vol-aware legs, first hit wins: Raschke fast (|1h ret| ≥ 2σ_1h ∧
      vol_ratio ≥ 2), Clenow/Carver vol-z (|day move| ≥ 1.75× elapsed-scaled
      daily σ, Brownian √t), Murphy rank (top decile of the crypto complex) —
      plus Steenbarger participation veto (vol_ratio ≥ 1.5 binds z/rank). All
      doctrine constants in the gate; call site gathers zero-I/O evidence
      (5m-buffer daily σ, 1m base-window σ_1h anti-contamination, 60s-memoized
      complex moves). Kill switch DISPERSION_SELF_MOVE_EXEMPT_ENABLED;
      rejection events carry the evidence bundle for shadow calibration.
      Hugo gains the alt-breadth day-move tiebreak (≥5 crypto alts ≥5%
      same-direction when majors EW reads 0 — BTC +1.7% on a +10% alt day);
      knobs trend_offensive_alt_breadth_enabled/_min/_move_pct.
    - **Fast-path gate hole (watchdog-found, 1424fde)**: direction-loss strike
      lockout never bound the cascade executors (ETH short re-entered 10s
      after direction_loss_block_armed). `_direction_loss_blocked` strict
      version (no overrides — standard path owns those) in both guard loops.
    - **Fast-path journaling**: momentum/aftermath fills journaled post-fill
      with entry_id registered — closes take the primary outcome path instead
      of synthetic orphans (all 6 closes in the window were synthetic).
    - Verified live (boot 17:48 UTC): 0 pane tracebacks, single process, 2
      positions re-adopted, treasury_heartbeat fresh, sizing_chain
      dd_mult_effective 1.0, ZERO recovery events post-boot. Suite
      1796P+28x+60xp (29F baseline cleared) + 75 new/touched.
    - Watchdog prompt amended (bak-20260827): SECOND-CYCLE CONFIRMATION TIER
      (2 consecutive confirmed cycles → autonomous fix, 24h-tier bounds
      unchanged), dust=flat deploy window, standing questions (aster
      participation, leverage-class presence, equity famine, Hugo silence).
    - Designed events (do NOT "fix"): dispersion_self_move_exempt,
      aster_recovery_exempted, trend_offensive_alt_breadth,
      direction_loss_block_active with source=, fastpath_entry_journaled,
      evidence fields on signal_rejected_dispersion_gate.
  - **2026-08-26** — Risk-parity sizing + journal orphan-close repair + Aster margin 80% (operator directive)
    - **Root causes**: (1) the sizing chain sized NOTIONAL — stop distance never
      entered the denominator, so a 0.4% stop and a 3% stop carried ~7x
      different risk at the same size ("all trades size the same"); (2) closes
      for cross-midnight positions vanished from the journal after every
      restart — `journal.load()` reads TODAY's file only, `_open_entry_ids` is
      memory-only, so entry_id pop + in-memory orphan scan both missed and
      `_record_close` logged position_closed but journaled NOTHING (08-26: 5
      log closes, 1 journaled) — Skeptic base rates, personality stats, churn
      flags, and capacity-governor journal_evidence all ate the bias.
    - **Risk-parity resize (Carver/Van Tharp/Thorp/Vince)**: new pure module
      `intelligence/risk_parity.py` — ratio = ref_stop(1%)/actual_stop,
      clamped [0.25, 3.0] (Aronson: bound every new degree of freedom),
      applied once after the correlation cap in the shared sizing chain (both
      venues). A stop at the reference distance is bit-for-bit unchanged;
      tight stops earn notional, wide stops lose it — risk per trade is
      equalized, not notional. Abstains on missing/degenerate stops. All
      existing governors (conviction stack, recovery 0.5x, session, dd, floor-
      resize, post-multiplier cap) still apply multiplicatively around it.
      sizing_chain event gains stop_dist_pct + risk_parity_ratio; resize logs
      risk_parity_resized. Kill switch RISK_PARITY_SIZING_ENABLED (env,
      default true per operator directive); RISK_PARITY_REF_STOP_PCT /
      _MIN_RATIO / _MAX_RATIO envs.
    - **Journal orphan-close repair**: TradeJournal gains
      find_open_entry_in_files (read-only scan of previous 4 day-files; rule
      #14 — source files never mutated), close_already_recorded (120s + pnl-
      matched dedup), record_cross_day_close (tier 1: migrated copy of the
      real entry with close_migrated_from — personality/margin/pnl_r survive;
      tier 2: synthetic orphan_close record from the Position object).
      `_record_close` falls through to it when entry_id + in-memory orphan
      scan both miss; perf/Chancellor/kant feeds run unchanged and now see
      the close. Kill switch JOURNAL_ORPHAN_CLOSE_ENABLED (default true).
    - **Aster margin 0.50 → 0.80** (both aster_margin_pct and
      aster_tradfi_margin_pct, ordering preserved): with stop distance as the
      risk governor, the margin budget is a ceiling — 80% lets tight-stop
      high-conviction trades reach risk-parity size (~$45 margin ≈ 0.7%
      sleeve risk at a 1% stop on the $336 sleeve).
    - Suite 1737P+28x+60xp (baseline + 23 new: test_risk_parity 11,
      test_journal_orphan_close 12).
    - Designed events (do NOT "fix"): risk_parity_resized, sizing_chain with
      stop_dist_pct/risk_parity_ratio, journal_orphan_close_recorded (with
      migrated_from date or null = synthetic), journal_orphan_close_deduped.
  - **2026-08-26 (am)** — FLOW payoff repair: personality TP floor + structure snap + treasury runner (38b2c5c)
    - **Root cause**: generic bracket ladder set TP1 at 1.0-1.5R while FLOW/
      SCOUT/APEX declare rr_min 2.0/2.5/2.0 — the right tail was amputated at
      birth (FLOW payoff 0.56, avg win ≈1.5R). Freeman-Shor: the fix is
      payoff, not win-rate.
    - `intelligence/tp_ladder.py` (pure): floor_ladder_to_rr_min (TP1 ≥ rr_min,
      ladder re-rung upward; None = bit-for-bit when already ≥) +
      swing_levels/structure_target (manual-trader "lines": TP1 snaps to the
      nearest 15m swing level within [rr_min, rr_min+1.5R], 0.1R buffer).
      Spliced post-personality-size in main.py; skips campaign/recovery/
      aftermath/non-directional. Kill switches PERSONALITY_TP_FLOOR_ENABLED,
      STRUCTURE_TP_SNAP_ENABLED (default true).
    - **Treasury runner (the 5R/7R/10R mechanism)**: TP2 banks 75%, 25%
      runner exits only on a 50%-of-peak-ROE trail (treasury_runner_trail);
      runners exempt from re-harvest/runaway/recycle/loss-cut; reconciled
      when the position disappears. Knobs treasury_runner_enabled/_ratio/
      _trail_giveback.
    - Verified live: personality_tp_floored firing (XAUT 4777→4847, TAO x2,
      TRUMP short). Suite 1715P+28x+60xp (+22).
    - Designed events: personality_tp_floored, personality_tp_structure_
      snapped, treasury_runner_trail, TP2 partial exits at 75%.
  - **2026-08-25** — 15% doctrine wiring: treasury-owned exits + alt_season cap 7 (f987ee0 + 0ccef19, operator directive)
    - **f987ee0** raised the treasury threshold stack to 15% (TP1/TP2 15/25,
      runaway 15, small-acct caps 15/25, trail giveback 60% of peak) — the
      4-6% first harvest was the disposition-effect class the Treasury exists
      to repair. **0ccef19** wires the ownership through: `_basket_managed_syms`
      registry written by the treasury loop each tick; `_software_tp_loop` and
      `_dynamic_profit_cap_loop` skip treasury-MANAGED symbols entirely (the
      1.5R software TP had clipped ETH at 4% ROE 07:49 — the early harvest one
      layer down; the BTC close the same morning was a stray pre-restart
      native TP — startup sync recovers stops only, native TPs stay live
      exchange-side. Seam flagged as watchdog proposal candidate).
      Trailing stops still protect independently; unmanaged symbols keep
      every safety-net behavior bit-for-bit.
    - **alt_season_max_positions 3 → 7**: the alt_season clamp was the binding
      cap ("active 3, cap 3" in replacement-eviction events) — the book never
      held more than 3. Now matches max_concurrent_positions.
    - Verified live (boot 15:14 UTC): 0 pane tracebacks, single process,
      SOL short 1.537 + BTC dust short re-adopted with protective stops,
      treasury_heartbeat crypto_beta n=2 tp1=15.0 tp2=22.5, no harvest at
      3-5% ROE (correct idle). Suite 1693P+28x+60xp (baseline restored after
      6 test_treasury pins re-encoded for the 15% stack).
    - Designed events (do NOT "fix"): treasury-managed symbols showing NO
      software_tp / dynamic_profit_cap closes below 15% ROE.
  - **2026-08-24 (pm)** — SoDEX kline ownership for SILVER/COPPER (0e6b4cb) + 3× capital step-up (0e7e455, operator directive)
    - **Metals unblocked (0e6b4cb)**: Yahoo futures 1m lags ~10 min structurally
      → the interpreter's 90s staleness guard vetoed every SILVER/COPPER signal
      (~60-70/hr all session; same defect class as the 08-18 XAUT/CL fix, but
      neither metal has an Aster listing). New `sodex_kline_assets` config list
      + `tradfi_owns()` redefined as candle WRITER (healthy AND not yielded) —
      the dark-plane pin: naive yield would have had every feed yielding and
      nobody writing. SoDEX gate also yields aster-kline symbols (no AsterFeed
      race). SoDEX REST seeds 55 bars at boot (zero effective warmup); tradfi
      keeps polling Yahoo for the basis-divergence guard (health unaffected).
      Verified live (boot 16:40 UTC): 0 tracebacks, SPCX re-adopted with
      protective stop, SILVER/COPPER seeded 55 each, zero stale_data for
      either since. Suite 1693P+28x+60xp (+9 test_sodex_kline_ownership).
    - **3× capital step-up (0e7e455)**: base_trade_usd 200→600, max_trade_usd/
      max_notional_usd 250→750, aster_margin_pct 0.25→0.50,
      aster_tradfi_margin_pct 0.40→0.50 (tradfi ≥ base ordering preserved).
      ~$102 typical / $150 max margin per trade ≈ 13-20% of a $763 book;
      Chancellor 60% total exposure ceiling unchanged; aftermath cascade cap
      (base × 1.5) auto-scales to $900; balance safety cap ($1,144) stays
      non-binding. Verified live: post-boot sizing_chain BCH $338.50 notional
      (old ceiling was $250), execution approved and filled.
    - Watchdog prompt.md gained WATCH ITEMS for both deploys (metals
      stale_data must stay zero; per-trade loss >$15 or book margin >40% =
      alert) + sodex_kline_assets added to the MUST-NOT universe list
      (backup prompt.md.bak-20260824).
    - Designed events (do NOT "fix"): sodex_historical_loaded for
      SILVER/COPPER at every boot; sizing_chain notional up to $750.
  - **2026-08-24** — Win-rate shrinkage on cascade caps + phantom-purge of personality stats (5dba8be)
    - **F1 — sizing integrity**: `_win_rate_band` gains optional `n_trades` with
      empirical-Bayes shrinkage (k=20, same doctrine as the Skeptic base rate).
      Root cause: APEX at 0W/1L (one −$0.44 loss) raw-banded at the 0.25 floor
      and capped every cascade entry at 25% of venue equity (BTC cascade
      executed $49 vs ~$200 uncapped). n=1 now shrinks to ≈0.476 → 0.75 band;
      large-n personalities barely move. Both cascade call sites (APEX momentum
      main.py:~2749, AFTERMATH ~3317) pass journal n + log `win_rate_n`.
      Standard path untouched — Skeptic base rate is already k=20-shrunk
      (n=None = legacy bit-for-bit). Venue-agnostic: personality stats are
      global and the cap wraps cascade entries on BOTH SoDEX and Aster.
    - **F2 — phantom purge**: `is_phantom_record` (SPCX-USD 2026-08-21/22,
      |pnl|>$100) filters the four scale-mismatch ghost closes (+$1,578 fake
      AFTERMATH pnl) out of derived stats; journals permanent (rule #14),
      eafedde already blocks new ghosts at the triggers. Purge ran live:
      AFTERMATH 17W/22L +$1,576.73 → 14W/21L −$1.07 (backup
      agent_winrates.json.bak-phantom-purge-20260824).
    - **Bonus defect killed**: `restore_from_journal` now dedups rolling-window
      day-files by (entry_id, closed_at_ms) — every trade was being counted
      2-4× (414 dupes skipped at first boot post-fix).
    - Verified live (boot 09:00 UTC): 0 pane tracebacks, single process,
      ETH+XAUT re-adopted with stops, `performance_restored` dupes_skipped=414
      phantoms_skipped=4, `agent_winrates_loaded` shows clean AFTERMATH 14/21.
      Suite 1684P+29x+59xp (baseline + 20 new test_winrate_shrinkage pins).
    - Note: VM pytest run blocked by pre-existing eth_typing/web3 import
      break in conftest on the 3.11 venv (unrelated test files fail
      identically) — fix in the evening session.
  - **2026-08-23 (am)** — Hugo trend-offensive engine + recovery gate (1d54680/6bfc6ec/6dbe271)
    - `intelligence/trend_offensive.py` (Hugo, pure brain): on a locked BTC
      trend day (day-move ≥3%), aligned symbols earn offensive modifiers
      (coherence boost, size, strike-gate relief) — the pyramid-into-strength
      doctrine. 7 consumer reads spliced in main.py.
    - Recovery gate (6dbe271): `_hugo_sym_aligned` returns False while
      `_adaptive_calibrator.get_recovery_params()` is truthy — capital
      preservation outranks graduation; fail-closed on calibrator error.
      All 8 Hugo consumers suppress in recovery via the single gate.
    - Verified live (boot 11:43 UTC): new PID, 0 tracebacks, 0
      trend_offensive_loop_error; 0 Hugo activations expected (BTC day moves
      <3% threshold). Designed events: trend_offensive_* loop telemetry.
  - **2026-08-23 (pm)** — Capacity governor + mover radar + Aster maker-first (ddf58e4, operator directive)
    - **HYPE/MUBARAK = one failure class, two pipes**: HYPE +41%/7d fired
      1011 signal_ready but the per-symbol daily cap (4/day, a churn guard
      from the ETH 35-trades-in-5-days episode) choked it by 02:54 UTC —
      403 blocks, none shadow-scored, invisible to every report. MUBARAK
      was the same miss one pipe upstream (silence). Root doctrine error:
      capacity was allocated by trade COUNT; risk doctrine allocates by RISK.
    - **`intelligence/capacity_governor.py`** (pure brain): evidence legs
      exempt trend participation from the count cap — graduated, Hugo-aligned,
      day_move_aligned (symbol's OWN day move ≥3% in signal direction; no BTC
      dependency, no graduation-slot contention — the legs that would NOT have
      saved HYPE are no longer the only legs), mover_relief (radar-armed
      TTL param), journal_evidence (the shadow journal's measured per-symbol
      cap accuracy, n≥10, accuracy ≤0.35 — the journal feeding the entry path
      LIVE, an engine not a library). Steenbarger churn signature: same-day
      direction flip kills the soft legs. Carver R-budget: ALL legs bounded
      by per-symbol daily stop-risk (1% of book, daily_symbol_risk_budget_pct).
      Recovery suppresses everything. 7-book grounding in module docstring
      (Livermore/Taleb/Carver/Thorp/Raschke/Steenbarger/Aronson).
    - **`intelligence/mover_radar.py`** + supervised loop: the cross-pipe
      missed-move detector — PUBLIC Bybit 24h ticker moves (feed-independent;
      a dead internal feed cannot blind it) crossed with participation
      (trades/signals today). blocked class arms mover_relief:{symbol} (TTL
      1h, R-budgeted); silent class warns only — fail-CLOSED, never
      auto-trades a dark data plane.
    - **Shadow journal**: daily_trade_cap_reached registered (gate daily_cap,
      counterfactually scored from birth — the 403 HYPE blocks would have
      indicted the cap within days); gate_symbol_verdict() live readout.
    - **Graduation slots 1→2 per direction** (rally_max_graduated_per_direction;
      slot_taken ×141 during HYPE). DailyTradeTracker: per-symbol direction
      mix + consumed 1R (risk_usd at record_open).
    - **Aster maker-first entries** (the −15.5bps systematic taker tax):
      GTX at the touch from the live L4 book, 8s fill window
      (aster_maker_timeout_s), cancel + fill-race adopt + ONE taker retry;
      no_taker_fallback fail-closed. Selector rule 4; coherence ≥7.5 stays
      market. Digest size_chain flag now per-venue (combined-balance
      reference false-alarmed $65 median on $754; Aster median ≈35% of its
      OWN $188 sleeve — Vince doctrine, healthy).
    - Verified live (boot 14:08 UTC): 0 pane tracebacks, single process,
      aster 52/0, ETH+SOL longs re-adopted with protective stops,
      treasury_heartbeat fresh, radar FIRED first pass (TRUMP +13.1% silent;
      AAVE/CYS/ZEC +11-12% blocked, relief armed). Suite 1664P+29x+59xp.
    - Kill switches: DAILY_CAP_DAY_MOVE_EXEMPT_ENABLED,
      DAILY_CAP_JOURNAL_EVIDENCE_ENABLED, MOVER_RADAR_ENABLED,
      ASTER_MAKER_FIRST_ENABLED, rally_max_graduated_per_direction=1.
    - Designed events (do NOT "fix"): daily_trade_cap_exempted (reason ∈
      graduated/hugo_aligned/day_move_aligned/mover_relief/journal_evidence),
      daily_trade_cap_reached with direction+reason (shadow gate daily_cap),
      mover_radar_blocked, mover_radar_silent,
      aster_maker_entry_unfilled_taker_fallback, aster_maker_entry_unfilled,
      rally_graduation_slot_taken with slots_used/max_slots.

  - **2026-08-22 (eve)** — Mark-entry scale guard + Kant Gate-8 daily-pnl feed (eafedde)
    - **SPCX phantom (+$792/+$799 journaled, balance untouched)**: SoDEX
      markPrice served the pre-rebase scale (765.72) while klines/entries
      served ~135 — a PERSISTENT 5.66x split, not a tick jump, so the
      discontinuity quarantine never armed. software_tp "won" instantly
      against the entry-scale ladder; phantom flowed into chancellor
      daily-realized (5% daily-loss gate disarmed until day roll).
    - `_mark_entry_scale_ok`: close triggers skip a position whose mark
      diverges >30% from its OWN entry (knob mark_entry_scale_guard_pct).
      Spliced at all 5 mark-driven close triggers: software stop, software
      TP, treasury ledger, conviction review, coherence decay. Fail-closed:
      position kept, mark_entry_scale_mismatch logged.
    - **Kant Gate-8 was blind since birth**: risk_engine.daily_pnl existed
      but was never fed (gate reason daily_pnl:0.00 all day) — 5% daily-loss
      breaker + 10%/48h weekly pause never could fire. record_close(pnl,
      day) with UTC day roll, wired in _record_close.
    - Verified live (boot 19:34 UTC): 0 tracebacks, single process, 3 shorts
      re-adopted (BTC 7x protective stop queued), treasury heartbeats fresh.
      Suite 1621P+29x+59xp. Restart also re-armed the chancellor gate
      (in-memory accumulator resets at boot).
    - Designed events (do NOT "fix"): mark_entry_scale_mismatch.
  - **2026-08-22 (morning)** — Distracted-mode deadlock fix + IS sampler cadence (f1833a0 + 4ebfb92)
    - **Root cause of the missed TRUMP/alt rally**: the meta-cognition dust
      census counted STRUCTURALLY UNCLOSABLE dust (close notional below the
      venue minimum — SoDEX $10 / Aster $1). BTC 1e-05 ($0.77) → dust_ratio
      0.5-1.0 → meta_block_entries re-armed every 30-min pulse from 05:26;
      291 TRUMP signal_ready blocked ×144. No purge exists in the pulse, and
      the block prevents the designed cure (same-symbol re-entry netting).
    - Fix: `_actionable_dust_ratio` counts only dust ARIA can close (notional
      ≥ venue close minimum). Census-only — zero sizing/routing impact.
    - Also in 4ebfb92: Hasbrouck IS sampler 5s→1s (5s cadence is
      unidentifiable — lead-lag is sub-5s, Cholesky bounds spanned [0,1]);
      deque 720→3600, estimate floor 900; lppl_conf row forwarded onto the
      compression watchlist.
    - Verified live (boot 09:04 UTC): 0 pane tracebacks, single process,
      meta_cognition_pulse mode=focused dust_ratio=0.0 WITH the BTC dust
      still on the book (the pin), 0 meta_reflex_entry_blocked post-boot,
      149 signal_ready, XAUT longs executing (09:24/09:36). Suite
      1613P+29x+59xp.
    - Designed events (do NOT "fix"): dust_ratio=0.0 while sub-$10 dust
      exists on the book.
  - **2026-08-22 (late night)** — Paper synthesis bundle: LPPL + Hasbrouck IS + YZ/VR + winner-side inversion + aster anchor (ee0686b)
    - **LPPL (Sornette 1996, `intelligence/lppl.py`)**: dragon-king confidence
      by grid (tc, m, ω) + linear lstsq with the Sornette filter conditions
      (B<0, damping ≥1). Wired as an ADDITIVE boost in explosive readiness
      (min(1, base+0.25×conf) at conf ≥0.5) — NOT a 5th precursor (would
      dilute 3/4 candidate scores). metrics["lppl_conf"] on the watchlist.
    - **Hasbrouck IS (1995, `intelligence/price_discovery.py`)**: VAR→VMA
      information share, Cholesky both orderings → bounds + midpoint. New
      `_price_discovery_loop` (5s paired SoDEX/Aster mark sampler on the
      shadow-dual majors, deque cap 720, hourly estimate) →
      `price_discovery_share` log + information_share section in
      venue_comparison.json. Answers "does Aster lead or follow?" with data.
    - **Yang-Zhang + Lo-MacKinlay (`intelligence/volatility.py`)**: the
      conviction-review noise band takes max(ATR15, YZ) — gap risk stops
      misreading as thesis failure; VR(8) path class (5-min memo from 15m
      closes) shortens grace ×0.75 when "mr" (recoveries come fast or not
      at all). path_class on both conviction telemetry events.
    - **Winner-side offensive mirror (Frazzini)**: green beyond the noise
      band + counter verdict + fresh opposite signal → `winner_inversion`
      banks early (the disposition effect cut in the opposite direction);
      hold_winner otherwise. Loser logic untouched.
    - **trim_winner dead wire CLOSED (Freeman-Shor)**: coherence-decay winner
      trim now executes a real 50% partial close (_close_with_retry +
      _record_partial_close, 30-min cooldown, dust-floor guard both halves)
      — was log-only "trailing_stop_will_protect" since 2026-08-16.
    - **Aster book anchor**: 1Hz mark seam closed — entries re-anchor to the
      ≤250ms depth20 L4 mid at all 3 bracket call sites
      (`aster_entry_anchored` with delta_bps). Fail-open to the mark; SoDEX
      untouched.
    - **Digest**: fundamental_law (Grinold-Kahn IC×√breadth, weekly, n≥10)
      + recheck_yield (deferred holds vs abandon pnl by reason, daily).
    - Kill switches (every False = pre-bundle bit-for-bit): LPPL_ENABLED,
      PRICE_DISCOVERY_ENABLED, VOLATILITY_ESTIMATORS_ENABLED,
      CONVICTION_WINNER_INVERSION_ENABLED,
      COHERENCE_DECAY_TRIM_WINNER_ENABLED, ASTER_BOOK_ANCHOR_ENABLED.
    - Verified live (boot 00:55 UTC): zero pane tracebacks, single process,
      aster 52/0, BTC+XAUT re-adopted with stops, startup_sync_complete,
      treasury_heartbeat fresh, zero price_discovery_error, digest writes
      recheck_yield (13 abandons −$9.56 read from history).
    - Suite 1609P+29x+59xp (1571 + 38: 23 synthesis, 10 volatility, 5 IS).
    - Designed events (do NOT "fix"): price_discovery_share,
      aster_entry_anchored, coherence_decay_trimmed, conviction_decay_closed
      with reason winner_inversion, lppl_conf in compression_watchlist.json.
  - **2026-08-22 (night)** — Conviction Review v2: thesis-tested, regime-conditional abandon brain (40b35d6)
    - **The audit that forced it** (12 v1 abandons, 2026-08-21): actual −$8.10
      vs hold-to-stop-or-4h counterfactual **+$2.77** — the exit class had
      ≈−$10.9/day negative expectancy. All 6 costly abandons were trend-aligned
      LONGS (TAO −2.18→+6.37, BTC −0.89→+4.09, XAUT ×2, SOL ×2); all 4 saves
      were shorts. The stops were never the problem (1 of 8 longs would have
      hit its stop in 4h) — the 30-min clock was early, and v2 addresses the
      clock, not the brackets.
    - **`intelligence/conviction_review.py`** (pure brain, zero-I/O, department
      template): (1) **Raschke** — same-direction guardian-passed signal inside
      the grace window = thesis ALIVE → hold (`_last_signal_dir` map written at
      all 3 guardian-passed sites; opposite-direction never counts as support);
      counter verdict + fresh opposite signal + bleeding + age ≥900s = thesis
      INVERSION → abandon early. The v1 "no supporting signal" log claim was
      never tested (`_last_signal_ts` read, never used) — now true telemetry.
      (2) **Lo** — grace = 1800s × `conviction_decay_aligned_grace_mult` (4.0)
      when `_trend_day_verdict` (the aster_swing helper, main.py:2396) returns
      aligned; counter/unknown keep base. SOL 22:31 case pinned in tests.
      (3) **Carver** — bleeding band = max(0.4%, `conviction_atr_noise_mult`
      ×ATR15) — no flat-price cliff at the boundary; ATR from interpreter cache
      → 15m buffer → flat-floor fallback. (4) **Chan** — the multiplier is a
      stand-in until gate_accuracy n≥30 measures recovery half-life. (5) **Van
      Tharp** — every abandon opens a "continue holding" counterfactual shadow
      record carrying the REAL bracket stop (`shadow_journal._commit`
      stop_override + `record_exit_counterfactual`, gate `conviction_decay`) —
      exit efficiency lands in the EXISTING gate_accuracy aggregation, zero new
      measurement machinery.
    - **Debt killed**: unreachable 60-min winner branch (ROE gate excluded all
      winners — removal is behavior-identical); false "no supporting signal"
      telemetry; aster_swing exemption wired (own 8h doctrine).
    - **Defers are observable** (Carver): `conviction_decay_deferred` (throttled
      5min/symbol+reason) only when v1 WOULD have fired (ROE ≤−2%, age >1800s).
    - Kill switches: `CONVICTION_REVIEW_V2_ENABLED=false` = v1 bit-for-bit;
      `CONVICTION_INVERSION_ENABLED=false` kills the accelerator alone.
    - Verified live (boot 23:52 UTC): zero tracebacks, single process, BTC+XAUT
      re-adopted with stops, treasury heartbeats fresh. Suite 1571P+29x+59xp
      (#12 + 32: 28 brain pins, 4 exit-counterfactual).
    - Designed events (do NOT "fix"): conviction_decay_deferred,
      conviction_decay_closed with reason ∈ {signal_absent, thesis_inversion,
      v1_abandon}, shadow records with gate=conviction_decay whose entry is the
      abandon mark (the "hold" counterfactual).
  - **2026-08-22** — ZEC autopsy: aster place_bracket size contract + base-rate expectancy veto (79ff55d)
    - **The ZEC kill shot (−$5.89, 22:11→22:17)**: entered the standard path
      at **10× intended size** with a KNOWN 18.7% base rate. Two stacked
      defects: (1) `aster_client.place_bracket` re-derived `notional =
      equity × margin_pct × lev` and IGNORED `candidate.size` — Fix B
      (8fa1855) laddered the candidate in build_candidate but the venue
      boundary never consumed it, so Kant/Nietzsche (0.124)/WillEngine
      (×0.475 → 0.062) were all discarded and the exchange filled 0.619.
      Every aster bracket entry since the 07:48 boot fired at the raw
      sleeve ceiling regardless of conviction (KAITO/XRP/APT losses same
      class). Fix: candidate size = intent, equity size = ceiling (min),
      floored to step (rounding can never exceed the cap), fail-closed
      `aster_candidate_size_missing`. bybit_client.py:399 carries the same
      pre-Fix-B pattern — venue inactive, flagged, not touched.
      (2) Nietzsche's basket cap shrank the 0.187-WR candidate to 25% and
      fired anyway. Chan/Thorp: negative-expectancy setup class gets size
      ZERO. New `base_rate_veto()` in skeptic.py — veto only when the
      k=20-shrunk blended WR < 60% of the candidate's breakeven WR (from
      its own rr_ratio, default 0.5) with n ≥ 10; spliced at the standard
      path (post-Skeptic, pre-Nietzsche) and the cascade-aftermath guard
      loop. Shadow-scored from birth: `signal_rejected_base_rate` → gate
      `base_rate_veto`. Kill switch BASE_RATE_VETO_ENABLED.
    - Exit fired through `portfolio_loss_cut` (armed by the day's
      conviction_decay losers) 6 min after entry, mark already through the
      bracket stop — the bracket never got to act at 10× size.
    - Verified live (boot 22:49 UTC): zero tracebacks, single process,
      3 positions re-adopted (incl. ETH aftermath long 2525.4),
      startup_sync_complete, heartbeats fresh. Suite 1539P+29x+59xp
      (#12 + 9: 3 bracket contract tests, 6 veto pins).
    - Designed events (do NOT "fix"): signal_rejected_base_rate,
      aster_candidate_size_missing.
  - **2026-08-21 (night)** — Cascade aftermath rotation filter: Murphy blocks, Chan ranks, Aronson measures (6f651b9)
    - **Murphy (weak form)**: in a confirmed rotation (confidence ≥0.6), a
      cascade-aftermath LONG into the LAGGING category is a knife (money is
      leaving it) → blocked `lagging_knife`; a SHORT into the LEADING category
      fades strength → blocked `leading_strength`. Neutral categories pass;
      unknown/low-confidence state abstains = pre-module behavior. Weak form
      only — the strong form (require aligned category) would starve the
      sample at ARIA's trade frequency.
    - **Chan (cross-sectional mean reversion)**: survivors ranked by RESIDUAL
      overshoot — per-symbol cascade move (pre-cascade snapshot → current
      mark) minus its category mean; singleton/uncategorized symbols fall back
      to the all-symbol mean. Ilmanen: beta-reversion (residual ≈ 0 in a
      market-wide cascade) is a valid trade, so Chan RANKS, never blocks.
    - **Aronson**: every block logs `signal_rejected_rotation_filter` → shadow
      gate `rotation_filter` scores it counterfactually from birth; the data
      argues for tightening, not the narrative.
    - Wiring: `aftermath_rotation_verdict` + `residual_overshoots` in
      intelligence/rotation.py (pure, zero-I/O); verdict gate inside the
      `_execute_cascade_aftermath` guard loop; residual re-sort of the
      L4-confirmed list (`cascade_aftermath_residual_ranked`). Kill switch
      CASCADE_ROTATION_FILTER_ENABLED=false = pre-module system bit-for-bit.
    - Verified live (boot 21:53 UTC): zero pane tracebacks, single process,
      BTC/XAUT re-adopted with stops + ETH stop placed, treasury_heartbeat +
      pnl_attribution fresh. Suite 1530P+29x+59xp (#12 + 13 new).
    - Designed events (do NOT "fix"): signal_rejected_rotation_filter,
      cascade_aftermath_residual_ranked.
    - Deferred: momentum-path rotation filter; tightening from weak to strong
      form until rotation_filter shadow data accrues.
  - **2026-08-21 (eve)** — Rotation laggard-catch-up modifier, LIVE sizing (ec641a6, operator directive)
    - Operator overrode the proposed 2-week shadow phase: wire to live sizing
      now. Modifier form only (not a standalone entry): a laggard inside the
      confirmed leading category earns up to **+0.5 coherence** (half the
      graduation privilege). Long-side only — bearish macro bias zeroes it.
    - Book grounding: Murphy (leaders first, same-sector laggards catch up),
      Chan (trade the residual category−asset, only while the sector factor is
      confirmed positive), Clenow (absolute-momentum floor −0.5% keeps falling
      knives out), Ilmanen (intraday reversal edge is small → cap 0.5),
      Aronson (measured from day one — rotation_boost_applied events feed the
      shadow journal counterfactually).
    - Wiring: `intelligence/rotation.py` (pure, zero-I/O) → `MarketContext.
      rotation_boosts` (built once per tick with the regime matrix) →
      coherence **Tier 10**. Fail-closed: confidence <0.6, no leader,
      cat_score ≤0, gap <0.5%, broken trend → no boost. Kill switch
      ROTATION_MODIFIER_ENABLED=false = pre-module system bit-for-bit.
    - Verified live (boot 21:11 UTC): zero tracebacks, 4 positions re-adopted
      with stops (ETH/BTC/SOL/XAUT), treasury heartbeat fresh, modifier firing
      within 4 min — regime alt_season, leading alt_l1, laggards boosted:
      SOL ×12 / AVAX ×11 / XMR ×12 (0.5 cap). Suite 1517P+29x+59xp (#12 + 15).
    - Designed events (do NOT "fix"): rotation_boost_applied.
    - Deferred: short mirror (unfallen leader inside a lagging category) until
      the long side shows journal evidence.
  - **2026-08-21 (pm)** — Silent-failure guards: close confirmation + rebase quarantine + close dedup (f7733d6)
    - **Three phantom-state defect classes** from the session audit: (1) XAUT
      ghost — a successful-but-partial aster position poll booked a phantom
      exchange_close on ONE sighting of absence; the phantom close cancelled
      the live stop and 0.059 XAUT ran naked 9h. `absence_confirmed()` now
      requires 3 straight 5s reconciliation passes of absence before booking;
      presence resets (close_absence_pending designed event). (2) SPCX 5.7x
      synthetic rebase — the rebased mark fired a software stop against the
      pre-rebase entry and journaled a phantom −$649.78 while the balance was
      untouched. `MarkPriceStore` quarantines trigger consumers 60s on a >15%
      single-tick jump (mark_discontinuity_quarantined); software stop AND
      software TP triggers skip quarantined symbols (software_stop_quarantined
      / software_tp_quarantined); reconciliation re-anchors entry/stop/TP1-3/
      size via `rebase_reanchor()` only when the exchange size moved by the
      INVERSE factor (mark_rebase_reanchored) — a real violent move fails the
      inverse-factor check and is never re-based. (3) SOL double-journal —
      exchange_close + external_close raced one fill and booked it twice in
      one second; `close_is_duplicate()` blocks a booking inside the 30s
      `_recently_closed` grace with no live tracked position
      (close_record_deduped); a fresh re-entry inside the window still books.
    - Error-cluster analysis (no fix needed — benign fail-closed races):
      software_stop_close_failed ×6 (ReduceOnly on already-closed), "position
      not found" ×11 (trailing cancel-after-close race), bracket_failed ×4
      (margin race).
    - XAUT ghost resolved: manual breakeven stop placed pre-deploy (aster
      order 658310786 @ 4594.0); at the 20:29 boot the position was ADOPTED
      (startup_position_synced XAUT-USD long + startup_stop_placed) — bot
      now owns it with a managed stop.
    - Verified live (boot 20:29:00 UTC): zero pane tracebacks, BTC long +
      XAUT long re-adopted with stops, startup_sync_complete, pnl_attribution
      + treasury_heartbeat fresh post-boot. Suite 1502P+29x+59xp (#12 + 17
      new test_silent_failure_guards).
    - Designed events (do NOT "fix"): mark_discontinuity_quarantined,
      mark_rebase_reanchored, software_stop_quarantined,
      software_tp_quarantined, close_absence_pending, close_record_deduped.
  - **2026-08-21 (am)** — Bull-run structural bundle (fixes A–E) + 52-symbol Aster universe (8fa1855)
    - **Fix A — close-verify**: `find_residual_qty` venue-shape matrix (normalized
      side, BUY/SELL, int 1/2, signed-size inference) + `_close_verify_flat` 8s
      post-close — flattens residual via `_close_with_retry` WITHOUT a second
      `_record_close` (no double journal entry).
    - **Fix B — aster fixed-fractional sizing** (Tharp/Vince): notional =
      sleeve_equity × aster_margin_pct × lev is the CEILING; conviction ladder
      scales down (1.0 = cap/2, 2.0 = cap). `balance_cap` neutralized for aster
      candidates so the SoDEX 20-30% margin cap cannot crush the 40% doctrine
      (regression pin: conv2 → $406 on $203 sleeve, not $203). Sleeve-equity
      integrity: combined-equity fallbacks at all 3 balance call sites gated
      SoDEX-only (Vince: fraction of the venue's OWN capital). Zero equity and
      sub-$1 base fail CLOSED. Kill switch `aster_standard_path_fixed_fraction`.
    - **Fix C — the MUBARAK-class silence solved**: AsterFeed markPrice
      write-through to shared `mark_price_stores` (aster-routed symbols had no
      store writer → mark_ok=False forever → symbol_ready unreachable, all
      aster symbols dead since launch) + 5000ms freshness margin for
      aster_assets (Horowitz: ≥5× the 1Hz signal; SoDEX keeps 500ms).
    - **Fix D — trend-aware eviction**: counter-trend candidate may not evict
      (`replacement_eviction_blocked_counter_trend`); offensive half (operator
      audit): counter-trend INCUMBENT scores 0.5× in the weakest-scan loop.
    - **Fix E — prune_age_expired contract**: expired symbols kept while HELD,
      dropped once flat (the clear() oscillation fired 8 cycles/60s on 08-20).
    - **Universe 37→52 aster_assets** (operator directive toward 70, tempered by
      cluster families): DOGE + 7 SoDEX→Aster migrations (XRP/1000PEPE/SUI/
      AVAX/LINK/LTC/NEAR — Aster book mechanically better: 0% maker, $1 min
      notional, native trailing) + 8 new (WLD/BOME/ICP/XMR/ORDI/WLFI/LIT/PAXG —
      dual-verified Aster TRADING + ≥$390K 24h vol + Bybit perp path; registered
      in config.assets + ASSET_CONFIG + BYBIT_SYMBOL_MAP + SUPPORTED_ASSETS +
      ASSET_CATEGORIES). Rejected with data: PUMP/NEIRO (no Bybit perp),
      ATOM ($83K/day Aster).
    - Verified live (boot 07:48 UTC): aster_venue_registered 52/0, feed 52,
      3 positions re-adopted (BTC/XAUT startup-sync + ENA via deferred
      protective retry — 2 ReduceOnly rejects raced adoption, retry landed the
      stop), treasury_heartbeat + pnl_attribution fresh, zero pane tracebacks,
      single process. Suite 1485P+29x+59xp (#12 + 23 test_bull_run_bundle).
    - Designed events (do NOT "fix"): close_residual_detected / flattened /
      flatten_failed / close_verify_error, replacement_eviction_blocked_
      counter_trend, build_candidate_aster_no_equity,
      build_candidate_aster_below_exchange_floor.
    - Deferred from the 7-book audit (proposals, NOT built): GTX post-only
      entries, triple-barrier shadow scoring, multi-asset margin (operator
      decision), treasury circuit breaker, staircase trailing widening.
  - **2026-08-21** — Aster swing class + pyramid add, Stage 1+2 (05fe743 + 702421d)
    - **The pyramid carrier on Aster** (operator directive: Aster swings AND
      scalps alts; two-hands doctrine — SoDEX carries majors/tradfi swings,
      Aster grinds alt scalps + runs pyramided swing runners). Zero new entry
      aggression: adds require a banked TP1, size off the BASE (never equity),
      one add ever, floor at combined VWAP breakeven ∓0.4% buffer — a
      pyramided trade cannot turn red beyond the buffer.
    - Stage 2: aftermath entries on aster symbols with trend-day verdict
      "aligned" (new `_trend_day_verdict` helper) tag `trade_type="aster_swing"`
      — no loser time-stop (_TT_CUTOFFS breakout semantics, 8h), native
      trailing loop owns the runner. Unknown verdict stays scalp (fail-safe).
      FOMO guard: no fresh swings at |day move| > 8%; adds tolerate 10%.
      Momentum path never tags — chasing is not swinging.
    - Stage 1: `_aster_swing_loop` (30s, position sub-gather) — post-TP1,
      30-min window, verdict re-check, execution-venue L4 not contradicting
      (depth-5 imbalance ±0.10, spread <25bps — the 8ed4cde book), not
      recovery → MARKET add 0.40×base, exchange-confirmed combined qty, VWAP
      re-anchor, native floor replace (tighten-only). Max 2 attempts;
      standard-path pyramid excluded on swing symbols; residual-order cleanup
      on close. Pure helpers (aster_swing_floor_price, aster_swing_add_gate)
      at module level. Kill switch ASTER_SWING_ENABLED=false.
    - **Ops incident folded in**: 83a84f2 put the bot in a NameError crash
      loop (closure def annotation `-> Optional[float]` evaluated at def
      execution; Optional never imported at module level). My 60s verify
      missed it — boot logs from pre-crash setup code looked healthy, and
      tracebacks go to the tmux pane, NOT aria.log. Watchdog auto-tier fixed
      it live (65c940f). **Corrected verify protocol (mandatory)**: (1) after
      RESTART_OK wait ≥90s (the script's pgrep check at 35s passes a
      deep-boot crasher), (2) `tmux capture-pane -t aria -p | grep -i
      traceback`, (3) confirm a GATHER-LOOP event (treasury_heartbeat /
      pnl_attribution) with a post-boot timestamp — only loop events prove
      main() survived its def section.
    - Coherence-8 review (operator question): kept for majors — boost stack
      (aligned +0.5, graduation +1.0) makes 8.0 reachable exactly on pyramid-
      appropriate days; alts use native evidence (alignment + TP1 + L4).
    - Verified live (boot 05:29 UTC): 36/0 aster, 2 positions re-adopted,
      treasury_heartbeat fresh at +47s/+107s, zero pane tracebacks, single
      process. Suite 1462P+29x+59xp (#12 + 16 new test_aster_swing pins).
    - Designed events (do NOT "fix"): aster_swing_registered,
      aster_swing_entry_stays_scalp, aster_swing_add_blocked (throttled 5min),
      aster_swing_pyramided, aster_swing_closed, aster_swing_cleanup.
  - **2026-08-20 (night)** — 7-book trend-guard bundle + Aster margin 40% (83a84f2)
    - **Autopsy**: -$12.06 day — 9 counter-trend BTC/ETH/SOL shorts 01:35–10:15
      into a locked trend-day rally (BTC 69.3k→72.9k). Cascade momentum/aftermath
      fast paths bypassed the quant filter's htf_counter_trend gate AND the
      trend-day guard (aftermath explicitly exempt); the guard itself was blind
      on majors (breakout="" all day, change_24h <5% threshold — BTC day move
      was +3.7% from midnight by 08:00 with no carrier); 12 losing
      conviction_decay abandons never armed the 2h cooloff portfolio_loss_cut
      arms → abandon/re-enter churn.
    - Fix 1 (Raschke/Link/Carver/Clenow): `_trend_day_veto` + `_loss_cooloff_blocked`
      helpers in main.py; both cascade executors drop vetoed/cooling candidates
      post-L4-rank. Refusals logged `signal_rejected_counter_trend` (exact name
      → shadow gate "counter_trend", counterfactually scored per Davey).
    - Fix 2: `trend_direction_guard` third direction source `day_move_pct`
      (move from 00:00 UTC open, own knob `trend_day_move_threshold_pct` 3.0%);
      any-source conflict fails open. Wired in standard path + both fast paths.
    - Fix 3 (Steenbarger/Van Tharp): losing conviction_decay closes arm
      `loss_cut_cooloff:{symbol}` (2h, direction-keyed); winners exempt.
    - Operator directive: `aster_margin_pct` 0.10→0.40, `aster_tradfi_margin_pct`
      0.20→0.40 (tradfi ≥ base ordering preserved) — size up the venue whose
      executions (XAUT +$0.58, XLM +$0.28 native brackets) were the day's only
      clean winners. Sleeve halt 30% DD unchanged.
    - Verified live (boot 23:40 UTC): 36/0 aster, 2 shorts re-adopted w/ TPs,
      0 post-boot errors, single process. Suite 1446P+29x+59xp (#12 + 7 new).
    - Designed events (do NOT "fix"): signal_rejected_counter_trend with
      source=cascade_momentum|cascade_aftermath; loss_cut_cooloff_blocked with
      source= on cascade paths; conviction_decay closes arming loss_cut_cooloff.
  - **2026-08-20 (eve)** — Aster L4 wire + dust-fix v2 + exit-mark throttle (8ed4cde, 92676b5)
    - AsterFeed depth20@100ms → orderbook_stores for aster-routed symbols —
      cascade/sweep/imbalance now read the book ARIA executes against (was
      Bybit's book for signal, Aster's for fills). Live-probed: Aster sends FULL
      top-20 per depthUpdate (no Binance partial-book semantics); reconciled via
      update_l4_diff so queue-age/cancel-velocity survive 10Hz; publishes
      ORDERBOOK_UPDATED (interpreter Tier-4 fast path is event-driven). Bybit
      yields those stores in main.py.
    - close_position_market v2: exchange-reported qty closes via place_order
      (Aster V3 rejects closePosition=true on MARKET — live-verified).
    - Verified: DEPTH_OK 20/20 levels 135ms fresh on VIRTUAL; 0 rejections.
  - **2026-08-19 (b)** — Open-book withdrawal detection (445d98c, operator directive)
    - Bug: flat-book-only withdrawal guard (`_open_pos == 0`) turned the 08-18
      operator withdrawal (~$21.5, book open) into a phantom 3.63% DD →
      recovery mode (floor 5.6, 0.5× size) suppressed ALL entries 28h.
    - Fix: `DrawdownManager.classify_external_flow` (pure, fail-closed: any
      close in the poll window disqualifies) keyed on wallet balance `wb`
      (excludes uPnL/MAM repricing) via new `sodex_client.get_wallet_balance`
      + `_close_event_counter` bumped in `_record_close/_record_partial_close`.
      Same-day ops: drawdown_state.json manually reset to current balance
      (backup: logs/drawdown_state.json.bak-withdrawal-20260819). Designed
      event: `withdrawal_anchors_adjusted` with note "open-book wb detection".
  - **2026-08-19** — Treasury (accounting department) + conviction floor + gate day-type accuracy (bcee090 + 48eb268 + 98ece5f)
    - **Autopsy evidence**: basket_harvest=0 all-time, 3 winner escapes, 95k/66k
      capped-threshold log lines. The basket loop disarmed books it could not
      act on (range day-type demanded 3 positions; modal book is 2), every
      close path gated on the SoDEX id map (Aster legs unharvestable yet
      counted toward activation), and the 7% escape valve clipped winners
      full-close while losers rode 2h to time_stop — machine-built
      disposition effect (winners cut 6-38min, per digest hold asymmetry).
    - `intelligence/treasury.py` (NEW, pure brain): venue-aware ledger
      (margin ghost-repair at default leverage), correlated clusters
      (crypto_beta/equity/commodity — Taleb book management), per-cluster
      threshold stack + depth EMA + 40%-giveback trailing lock, TP1 trim /
      TP2 full / runaway trim (banks 50% at 7% ROE, 10.5% trend — keeps the
      right tail), margin recycling (Goldratt: ≥75% margin util → oldest
      ≥45min stale-flat cut), loss-cut guard preserved. Cooldowns block
      re-firing, never ledger membership (B5). Native TPs cancelled ONLY
      for managed-cluster members (B1); per-symbol ownership handback;
      treasury_enabled kill switch reverts to individual TPs.
    - main.py: 646-line basket loop → 245-line thin executor; dust guard in
      _close_with_retry now venue-aware (Aster $1 spec vs SoDEX $10).
    - Phase 2a: campaign floor conviction-scaled (coh bands ×1.0/0.75/0.5)
      at all 3 floor sites — a 3.5 never out-sizes a 9.7 again.
    - Phase 1a/4: gate_accuracy_by_day_type in shadow aggregator (dispersion
      trend-day blind spot now measured, not argued); digest trend section
      (7d pnl, gate trajectory, chronic churners).
    - Verified live (boot 00:55:38 UTC): treasury_activated on the
      crypto_beta cluster (BTC short + ETH long re-adopted), heartbeats
      flowing (book ROE, cluster thresholds, peak trailing), aster 29/0,
      0 treasury errors; only the known transient startup stop rate-limit.
    - Suite 29F/1448P = baseline (#12) + 28 new (test_treasury 21,
      test_phase2_sizing 7). SSH dropped mid-restart leaving the bot dead —
      reconnected and started per the dead-bot playbook; verify process
      liveness after any restart-command ssh drop.
  - **2026-08-18 (night)** — False-state guards: phantom trough freeze + phantom closes (39136a5)
    - **Autopsy of the frozen day**: the 08-18 US session was lost to a phantom
      67.16% DD at 10:20 UTC — one Cloudflare HTML error zeroed the SoDEX leg of
      venue_balances, the degraded sum overwrote the equity cache, recovery mode
      activated and NEVER exited (recovery_mode_exited count was 0 in log history
      — a structural deadlock: the 5-win-streak/WR>50% exits are unreachable
      under recovery's own 0.5× size + 5.6 floor). 4 execution decisions all day,
      all before 08:52. The TradFi/XAUT/CL fixes from the morning WORKED (3 equity
      stale_data in-session vs thousands Monday; XAUT 0 stale post-boot, 341
      signal_ready) — 260 equity signals died at quality gates + recovery skips.
    - venue.py: per-venue poll failure tracking (positions_failed_venues /
      balance_failed_venues) — exceptions were silently merged as "no positions"
      / summed as 0.0 balance. Same swallow pattern, three functions.
    - main.py balance loop: substitute last-good equity for failed legs (a real
      wipe reports successfully and flows through; only exception-legs use cache).
    - main.py reconciliation: close detection skips symbols on venues whose
      position poll failed (close_detection_degraded) — 3 poll failures had
      booked 4 fake exchange_close PnL entries (journal corruption).
    - adaptive_calibrator: DD-triggered recovery exits when DD < 1.5% (half the
      3% trigger, hysteresis); win-rate-triggered recovery untouched. Calibrator
      now fed every balance update per its documented contract (was close-only,
      so recovery could never observe the DD clearing).
    - Restart verified: 0 recovery skips/applied post-boot, BTC short + ETH long
      re-adopted (software stops active; native stops hit a transient per-account
      rate limit at boot, retry via normal trail loop), aster 36/0, 0 errors.
    - Suite 29F/1420P = baseline (#12) + 7 new (test_false_state_guards.py).
    - Proposals: phantom-recovery-trough + false-position-close marked
      implemented; NEW silver-copper-underlying-source proposed (SILVER 1090 /
      COPPER 1479 stale post-boot — no Aster listing to migrate to).
  - **2026-08-18 (pm)** — Daily EV digest: deterministic precompute for the watchdog
    - `tools/daily_digest.py` (NEW, standalone — stdlib+httpx, repo imports lazy):
      runs even when the bot is DOWN. Writes logs/daily_digest.json (atomic
      tmp+replace) + one history line to logs/daily_digest_history.jsonl.
      Sections: per-symbol expectancy + churn_leak flags (n≥10, exp<-$0.02),
      size_chain (mean per multiplier field → chokepoint; size_leak flag at
      median notional <15% of a >$400 book), hold asymmetry, fee drag
      (gross vs net), exit-reason pareto (parses position_closed __main__),
      silence census (top veto, data-vs-gate kind), phantom sweep (peak ratio
      suspect >1.3×, recovery/deposit-veto/basket counts), gate accuracy +
      tail_cost_top5 missed wins from shadow_scored.jsonl, per-venue entry
      slippage in bps vs public klines (aster_assets→Aster, TRADFI→Yahoo v8,
      else Bybit; SSI skipped), benchmark = ARIA realized vs equal-weight
      BTC/ETH/SOL open→close OF THE DIGEST DAY (daily-bar date match — not
      prev-close→cur-close). Mondays add weekly coherence calibration +
      session attribution + venue_comparison.json. Best-effort doctrine:
      every section self-errors, exit code always 0.
    - Watchdog prompt.md EV SCAN rewritten: STEP 0 = run the digest, interpret
      its numbers instead of recomputing (cost guardrail — judgment, not
      arithmetic, is what the LLM is for).
    - Suite 29F/1413P = baseline (#12) + 24 new (tests/test_daily_digest.py).
    - No bot restart needed — standalone script, zero trade-path surface.
  - **2026-08-18** — Phantom-DD fix + campaign churn choke + TradFi signal unblock
    - **Two-day quant autopsy findings** (journal + 592 scored shadow verdicts):
      gates 85.6% accurate (dispersion 91.5% — NOT the blocker); real blockers
      were (1) phantom 42.6% drawdown taxing all sizing, (2) SPCX heartbeat
      churn, (3) structural staleness veto on every TradFi signal.
    - **Fix 1 — phantom DD (watchdog dd-peak-inflation-fix, ACCEPTED)**: balance
      monitor deposit branch (main.py) now requires flat book AND delta ≤50% of
      balance. Root cause: 08-17 08:15 a +$421 MAM/uPnL transient read as an
      external deposit → peak $1040.5 → fake 42.6% DD → 0.6× sizing + 2132
      recovery_mode_coherence_skips + 0.8 TP factor over 24h. Deposit branch had
      no position check (withdrawal branch had one) — asymmetric guard.
    - **Fix 2 — SPCX churn choke**: campaign heartbeat flipped direction to
      evade the per-direction Livermore block (70 trades/3d, 26% WR, -$2.23 =
      the entire book's net loss; ex-SPCX book was +$1.34, 12/12 wins).
      Symbol-level `_campaign_loss_cooloff` armed on any losing close
      (config campaign_loss_cooloff_s, default 2h), checked in the heartbeat.
    - **Fix 3 — TradFi signal unblock**: interpreter's 90s staleness guard
      measures from the tail candle's OPEN time; tradfi_feed wrote CLOSED bars
      only → tail always 60-120s+ old → every SoDEX equity signal vetoed all
      session (Mon: META 911, ORCL 1427, SILVER 878, TSLA 487 signal_stale_data;
      zero signal_ready from any equity). Now writes the FORMING bar too
      (buf.add in-place, Bybit contract); CANDLE_CLOSED still publishes only on
      newly-closed bars. XAUT/CL: Yahoo futures 1m lags ~10min even intra-
      session → new aster kline_1m ownership (config aster_kline_assets,
      AsterFeed kline_symbols + REST seed 200 bars + forming-bar writes,
      tradfi_feed set_candle_yield keeps Yahoo underlying for divergence but
      never writes their candles).
    - R:R autopsy: best signals got dust size (ETH coh 9.69 → $40 notional,
      +$0.17) while weakest got the floor (SPCX coh 3.5 → $250, -$2.23).
      $250 floor × 0.6 phantom × 0.5 recovery × ~0.5 conviction ≈ $40 — the
      phantom was ~⅓ of the size leak; TPs also cut 20% early (0.8 factor).
    - Suite 29F/1389P = baseline (#12) + 6 new. Watchdog proposals.jsonl:
      dd-peak-inflation-fix marked implemented; augur-restart remains open.
  - **2026-08-17 (am)** — Aster venue-contract triple fix (c8190f6 + fcb6436 + 1961db4)
    - First aster executions (aftermath fallback: UNI 23:52/00:19, ADA 00:22) exposed
      three contract breaks between the venue boundary and aster_client:
      (1) close_position_market took qty=/returned bool (boundary passes size=/reads
      .success) → qty 0.0 "Quantity less than zero" ×21,832 + AttributeError past the
      circuit breaker ×16,361 — an infinite ~1.2Hz close storm on ADA/UNI shorts;
      (2) _set_position_stop + replace_stop_order sent reduceOnly with closePosition
      → rejected "not required" → both shorts ran with NO exchange-side stop;
      (3) replace_stop_order swallowed new_stop_price= → stop 0.0 + None.success →
      startup_stop_exception, software-only protection after every restart.
    - Fixes: uniform venue contract (size/new_stop_price aliases, OrderResult
      returns), reduceOnly dropped from closePosition orders, explosive callers
      adapted. Watchdog 7ddd65d (partial close fix + startup-sync entry fields)
      absorbed via 9d0fd86; server reset --hard origin/main after content check.
    - Live consequences: ADA closed by software_stop 04:55 (−$1.83, fix working);
      UNI short native stop 3.3165 placed manually then re-placed by startup sync
      (order 1093155829, single stop exchange-side verified); storm = 0 post-boot;
      native aster trailing unlocked (order_ids["stop"] registered → 8724 loop).
    - Suite 29F/1383P = baseline (#12) + 7 new. LESSON: venue-boundary adapters
      need contract tests for BOTH axes (kwarg names AND return types) — all three
      bugs were the same shape.
  - **2026-08-17** — Shadow-journal restart-amnesia fix (7dbe303)
    - Root cause of "0 scored ghosts": `_scored` (finalized verdicts) was memory-only.
      The scorer worked perfectly (603/603 open records tracked live, 402 stops hit),
      but every restart wiped the verdict base; only records crossing their 24h
      birthday inside one process lifetime ever re-entered (registry load filter
      26h). At the 08-16 restart cadence (3 boots in 35 min), 1063 opened shadows
      produced ~5 retained verdicts → empty gate reports, graduation_eval n=0,
      Skeptic base rates starved.
    - Fix: finalize appends the full record to logs/shadow_scored.jsonl (append-only,
      one-bad-line doctrine); wire() loads it back (35d window, dedup by id
      last-wins, 20k cap). shadow_journal_wired now logs scored=N.
    - Verified live: first tick persisted 3 verdicts (OP/XLM/ARB), ETH short
      re-adopted, aster 36/0, zero scorer errors. Suite 29F/1380P = baseline (#12) + 4 new.
    - Consumers unstarved by this fix: Skeptic base rates (conviction layer),
      graduation explosive evidence (scored_records gate≈"explosive*"), nightly
      nine-questions + gate_accuracy reports, terminal dashboard gate card.
  - **2026-08-16** — Shadow-dual venue dataset + explosive breakout LIVE path (6607287)
    - **Shadow-dual (data-only)**: `aster_shadow_assets = [BTC, ETH, SOL]` — code-only
      list NEVER passed to `venue.assign_symbols` (routing isolation pinned by tests).
      Specs unioned into sync_symbol_specs (listed()=true → router shadow dual_listed=3),
      AsterFeed subscribes markPrice@1s + bookTicker for the 3 majors. Every SoDEX
      BTC/ETH/SOL fill snapshots both venues' books/marks/funding →
      logs/venue_snapshots.jsonl (append-only). Zero margin, zero routing change.
    - **3 reports**: gate_accuracy in nightly _aggregate (per-gate {gated, would_profit,
      would_profit_4h, accuracy, verdict} + _total "GATES CORRECT/TOO LOOSE");
      _compression_watch_loop (15min) → logs/compression_watchlist.json (score ≥0.5,
      days_compressed persisted, ARMED ≥0.75); _venue_report_loop →
      logs/venue_comparison.json (daily until ≥200 snapshots, then Monday-weekly).
    - **Explosive live path (the AKE catcher)**: ExplosiveScanner candidates (score ≥3/4,
      long-only, aster-routed symbols) → MARKET entry → STOP_MARKET at breakout-candle
      low (wick-capped at 5%) → NEW `place_trailing_stop` (TRAILING_STOP_MARKET,
      MARK_PRICE, callbackRate 10%, activation +15%, reduceOnly always). Actual filled
      qty polled from get_positions (partial-fill safe). Breakeven stop at +7% (hollow
      middle), 4h time-stop, residual-order cleanup on close. Guards all fail-closed
      with explosive_blocked reasons: kill switch, daily cap 10, 3 concurrent, 24h
      symbol dedup, sleeve halt 30% DD, fresh mark, $1 min notional.
      Caps per operator: 3 concurrent / 10 daily — first 30 trades are learning.
    - Verified live (boot 23:54:52 UTC): aster_venue_registered 29/0, dual_listed=3,
      startup_sync 3 positions (ETH long, SPCX short, BTC long), zero aster errors.
    - Suite 29F baseline (#12) + 18 new (test_shadow_dual.py 12, test_explosive.py 10).
    - Rollback: git revert + restart; EXPLOSIVE_ENABLED=false kills workstream C alone.
  - **2026-08-15 (night)** — Thinking Modes bundle + Aster FUNDED (e8041be + a258e10)
    - Phase B: Skeptic base-rate layer (intelligence/skeptic.py — shadow-journal
      scored records, dims coherence±0.5/regime/energy±10/category, shrinkage
      k=20, 60s memo) replaces _historical_wr at the conviction layer; Interpreter
      COMPRESSION switch (coherence.py structure tier reads Dreamer
      breakout-readiness: ≥0.75 → 2.0, ≥0.5 → 1.25, else 0.5).
    - Phase C: Router v2 SHADOW (execution/router_v2.py — score = −fee −carry
      −staleness-health; compare() logs router_v2_shadow on divergence +
      15-min heartbeat; log-only until shadow data proves it) + storm mode
      (market_energy>70: conviction coh 0.40→0.30 / cascade 0.15→0.25,
      stop_atr_mult ×1.25). market_energy=None default = calm bit-for-bit.
    - **ASTER FUNDED**: operator deposit confirmed via V3 accountWithJoinMargin —
      totalWalletBalance $202.95 (0.35 BNB joint-margin + dust), available $203.15,
      canTrade=true, hedge_mode=false, multiAssetsMargin=true, 0 positions.
      Sizing: $203 × aster_margin_pct 0.10 × 5x ≈ $101 notional — clears $1 min.
    - Live-verified (boot 22:42:50 UTC, single process): zero aster errors since
      boot (V1 -2015 spam ended with old process 22:29), aster_venue_registered
      29/0, aster_feed_connected 29, market_energy publishing, router_v2_heartbeat
      (dual_listed=0 — expected: aster_assets are aster-routed, majors stay SoDEX),
      shadow_journal_wired (375 open shadows), 3 SoDEX positions re-adopted w/ stops.
    - Suite 29F/1348P = baseline (#12) + 23 new. NOTE: VM is aria-prod-v2 (old
      aria-prod name deleted — gcloud ssh target updated).
  - **2026-08-15 (eve)** — Aster V3 auth adapter: the -2015 blocker SOLVED (d6bdf57)
    - Root cause was never IP binding: Aster's V1 (Binance-HMAC) protocol rejects
      ALL newly-issued API wallets from any IP (3 keys × 2 IPs tested). V3 auth:
      nonce (µs, monotonic) + signer (API wallet address) + EIP-712 signature
      (domain AsterSignTransaction v1, chainId 1666) over urlencoded params.
    - execution/aster_client.py: HMAC→EIP-712, all 15 paths v1/v2→v3 (account →
      accountWithJoinMargin). Live-verified on server with the real client class:
      hedge_mode=False, equity read, positions/orders/specs/health all 200,
      canTrade=true. NOTE: docs' fapi3.asterdex.com host is stale — v3 paths
      live on fapi.asterdex.com.
    - Keys: Aria2-final (Read+Perp Trade, to 2027-01-12) in server .env; Aria3
      (Read+Perp+Spot, to 2026-10-04) verified working as backup, not stored.
    - REMAINING: (1) Aster account equity ~$1.39 — operator must deposit before
      Aster trades; (2) running bot still on V1 code until next restart
      (fail-closed, harmless) — restart bundles with Phase B/C deploy.
    - Suite 29F/1326P = baseline (#12) + 1 new.
  - **2026-08-15 (pm)** — Aster ACTIVATED + incubation universe live (586b0ad + 9a82ea6)
    - Universe: 29 aster_assets (17 migration + 12 expansion: TRX/BCH/XLM/FARTCOIN/
      VELVET/AKE/CYS/ASTER/ACE/MUBARAK/DOS/SNXX — dual-verified Aster TRADING +
      quoteVolume + Bybit perp data path). Expansion joins config.assets immediately;
      fetch_symbol_ids aster exemption keeps them in the universe with no SoDEX ID;
      shadow journal scores stragglers under gate "no_venue". Rejected with data:
      ETC ($457/day), 1000SHIB (no Bybit perp), SPACE ($6K/day), COOKIE (not listed).
    - Verified live: aster_venue_registered symbols=29 skipped=0, specs synced,
      aster_feed_connected (29 markPrice + !forceOrder), aster_liq_tier6_wired,
      3 positions re-adopted with stops (ETH/BTC/LINK), no symbols_not_found,
      single process. Suite 29F/1314P = baseline (#12) + 35 new passing.
    - **BLOCKER — RESOLVED same-day (d6bdf57, see eve entry)**: the -2015 was
      protocol sunset (V1 HMAC dead for new API wallets), not IP binding.
      V3 EIP-712 adapter verified live; awaits bot restart + account funding.
    - Git hygiene: rescued 5 server-only watchdog commits via rebase (incl. SPCX
      phantom-basis hard-block fix, ssi_agent None-mark crash) — all now on GitHub.
      Server signals/aria_outbox.json keeps diverging pulls (runtime state, tracked —
      same bug class as #16; stash → pull → pop pattern used twice this deploy).
    - Margin research (docs): $ASTER IS margin-eligible — Multi-Asset Mode, BNB
      Chain, 80% collateral ratio (BTC/ETH/BNB 95%, USDT 99.99%). Toggle via
      GET/POST /fapi/v1/multiAssetsMargin (signed). Cross-margin only; auto-exchange
      at thresholds; negative USDT/USD1 to −1,000 USD interest-free. Phase 2 item.
  - **2026-08-15** — Aster venue Phase 1: second execution venue + 2nd cascade lens (133ce99)
    - PUSHED, NOT DEPLOYED (inert code — no restart needed; deploy happens when keys land).
    - `execution/aster_client.py` (NEW): Binance-protocol HMAC (docs test vector byte-exact).
      Hooks SoDEX lacks: $1 min notional (#14), maker 0% on ALL contracts (SoDEX 0.012%),
      native STOP_MARKET/TP_MARKET/TRAILING_STOP_MARKET on MARK_PRICE (#10), hedge mode
      (detected at boot, orders adapt positionSide; never changed by us), auto-cancel-all
      dead-man switch, ADL quantile (#8). 503 = status UNKNOWN → reconcile, never blind-retry.
    - `data/aster_feed.py` (NEW): !forceOrder@arr all-market liqs → Tier-6 liq_phase_engine
      as second confirmation venue (venue="aster" tag; breadth = deferred Bybit item 6c).
      markPrice@1s per tracked symbol for cross-venue basis. 4h silent-death watchdog.
    - Fee facts (docs): crypto taker 0.04%, stock-perp taker 0.009%, USD1-margined taker
      0.005%, maker 0% everywhere, 5% off paying in $ASTER. Stock perps trade near-24/7
      incl. weekends w/ EWMA-smoothed marks + ±5% aggression cap off-hours — reference
      design for the weekend-commodities question.
    - Activation: add ASTER_API_KEY/ASTER_API_SECRET to server .env, ASTER_ENABLED=true,
      populate aster_assets in config.py (code-only universe, issue #17) → restart.
      Enable hedge mode on the Aster account BEFORE keys if dual-side hedging is wanted.
  - **2026-08-14 (pm)** — Shadow journal Phase 1: counterfactual gate scoring (a7e88f6)
    - `intelligence/shadow_journal.py` (NEW): every gate refusal (14 rejection event types)
      opens a shadow position — entry from mark store, hypothetical stop (max 2×ATR15, 0.3%),
      scored at 1h/4h/24h with MFE/MAE + stop-hit. Dedup per (symbol,direction,gate)/30min.
    - Q10 lucky-gate census (Dayo's persistence test): gate value at refusal vs +30min;
      |Δ|>50% of threshold = TRANSIENT, <20% = PERSISTENT. Quadrants: PERSISTENT+saved=wise,
      PERSISTENT+cost=correct_unlucky, TRANSIENT+saved=**lucky** (most dangerous — gate
      learns "refusing works" on noise), TRANSIENT+cost=broken. luck_dominated flag >30%.
    - Nightly aggregator (after UTC midnight) → logs/gate_report.json + logs/shadow_report.md:
      Nine Questions (gate FNR w/ shrink k=20 + 14d half-life decay, anchoring, near-miss ≤10%,
      skew, GVR, fragility, silence gaps >4h, symbol edge, session map) + Q10.
    - Zero trade-path changes: single structlog processor (main.py:322) + 2 supervised loops
      (shadow_scorer 5min, shadow_aggregator). Storage JSONL append-only — chosen over SQLite
      per the 07-26 calendar.db "disk image malformed" precedent (one bad line kills one
      record, not the DB; journal permanence rule #14). SHADOW_JOURNAL_ENABLED=false kills.
    - Verified live: shadow_journal_wired at boot, registry created, BTC long re-adopted,
      single process. Tests 29F/1292P = 29F baseline (#12) + 13 new passing.
    - NOTE: full local pytest run hangs at Py_FinalizeEx on a non-daemon ThreadPoolExecutor
      worker (same class as issue #11) — results print only after kill. Suite itself = 19s.
  - **2026-08-14** — TradFi event starvation fix + per-symbol calendar regimes (b6059a7)
    - **Autopsy (13 silent days)**: ARIA traded zero times 08-02→08-14 while healthy. Chain:
      SoDEX v-token migration (08-01 ~06:31 UTC) emptied `/state.av` → Kant `balance_floor_halt`
      (correct gate — balance read $0.00). Funds were never lost: $480 vUSDC visible via
      `/balances.total` while `/state.av` read 0. Operator deposit 08-14 restored av.
      LESSON: when balance reads 0, query BOTH endpoints + spot before concluding blowup.
    - `data/tradfi_feed.py`: now publishes CANDLE_CLOSED per newly-closed 1m bar — the 07-29
      feed split took candle ownership but never published the event the interpreter's slow
      path runs on; equity/commodity signal generation collapsed ~99% (3333 → 15/day) for
      16 days. Off-hours the tail never advances → interpreter correctly silent.
    - `intelligence/interpreter.py` + `main.py`: calendar regime was ONE GLOBAL stamped by the
      last-ticked symbol — a single symbol's earnings BLOCK hard-blocked the whole book in
      the arbiter. Now per-symbol (`_calendar_regimes` dict), refreshed by calendar_loop
      (all symbols, 5min) + per-tick.
    - Verified: balance $480 read post-restart, 0 errors, signals flowing, single process.
  - **2026-07-30** — Bybit venue subsystem + Phase 3 liquidation lead
    - `execution/bybit_client.py` (full V5 client, mainnet-only): bracket (entry → position-confirm → MarkPrice trading-stop → TP reduce-only limits), atomic stop replace, pct-of-venue-equity sizing (`bybit_margin_pct` 10% × 5x → $50 notional at $100, scales linearly), 5-position venue cap, 10x leverage clamp, fail-closed on 0 equity.
    - **Chancellor venue partition**: sleeve self-halts at 30% sleeve drawdown (`bybit_sleeve_halt_dd_pct`) ≈5.6% of combined equity — a Bybit bleed can never reach the 8% kingdom veto. Session-scoped; top-ups lift equity above the halt.
    - `execution/venue.py` (NEW): symbol-partition dispatch — every symbol trades on exactly one venue; `bybit_enabled=False` resolves everything to SoDEX (zero behavior change). `all_positions()`/`combined_balance()` merge venues for the vault watermark.
    - Universe: 17 Bybit-only symbols (HYPE, ADA, UNI, ONDO, TAO, ENA, KAITO, WIF, ZEC, VIRTUAL, AAVE, 1000BONK, SEI, PENGU, INJ, TIA, APT) in `config.assets` + `bybit_assets` + `ASSET_CONFIG` + `ASSET_CATEGORIES` + bybit_feed maps. Selected against live Bybit turnover/OI; majors stay SoDEX (maker 0.012% < Bybit 0.02%).
    - `main.py`: ~30 call sites dispatched via `executor_for`; venue-aware symbol_id gates (entry/cascade/trailing-stop); `fetch_symbol_ids` keeps Bybit symbols in universe; leverage adapter (`venue.update_leverage`) bridges SoDEX-id vs Bybit-symbol signatures.
    - **Phase 3**: Bybit `liquidation.{symbol}` stream re-enabled (subscribed in SEPARATE frames — fixes the 2026-05-12 silent batch-drop) → Tier-6 `liq_phase_engine` as LEADING cascade indicator for SoDEX symbols. Notional-based z-score = natural venue weight; `venue` tag on LiquidationSignal/_EventRecord. Silent-death watchdog: `bybit_liq_stream_silent` warning after 4h without a liq push.
    - Tests: 17 bybit_client unit tests + suite at 29F/1277P = pre-existing baseline (#12).
  - **2026-07-29** — TradFi signal-source split + phantom-TP1/cancel-hole/.env root fixes (6463cb7 + cdaa7ae)
    - `data/tradfi_feed.py` (NEW): Yahoo v8 1m candles for 17 TradFi symbols (indices→SPY/QQQ, single-names→tickers, metals→GC=F/SI=F — v8 404s on `=X` forex). Owns candle buffers via `tradfi_owns()` (SoDEX feed yields). Signals from the deep market; execution stays on SoDEX marks.
    - Basis guard: scale-invariant 5-min RETURN divergence (SoDEX perps are rebased synthetics — level basis meaningless), 0.3% block / 0.2% unblock hysteresis. Convergence loop: divergence ≥15min → `personality="CONVERGENCE"` candidate, 0.5× size, stop = 2× divergence.
    - Single-names maker-only at the order-type selector (touch price + 120s timeout, taker fallback only at coherence ≥7.5). Turnover gate bypasses ADV floor when underlying feed is healthy.
    - Bug A (phantom TP1): same-side merge anchor `initial_size = total_size` (dust's stale initial_size inflated the anchor → TP1 fired on the entry itself → instant stop-outs). Plus price-confirm + 30s min-hold + through-mark stop rejection.
    - Bug B (cancel hole): `_schedule_cancel_verify` 60s post-cancel recheck (ghost → re-cancel, late fill → adopt); immune purge treats unknown-age orders as ancient; DCI purges non-RO orders opposed to tracked position side.
    - Bug C: `.env` was git-tracked — first `git reset --hard` after untracking DELETED server .env (bot aborted LIVE_MODE_CONFIRMED; restored from /tmp backup, ASSETS line commented). Config `field_validator` now hard-ignores env universe overrides. **API secrets are in git history — rotate.**
    - Deploy verified: 17/17 symbols seeded 200 candles, 0 poll errors, 0 boot errors, 3 positions carried, 6 open orders all reduce-only (zero ghosts).
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
  17. **2026-07-27** — Dual source of truth for the trading universe: server `.env` had a stale `ASSETS=[...]` line that overrode `config.assets` (pydantic-settings) — kept BASED-USD (id 78, delisted) in the universe after code removed it → "symbol not active" leverage rejections every boot. **FIXED** — ASSETS line removed from server .env (backup /tmp/.env.bak-20260727); code is now the single source. Lesson: `.env` is for secrets, not universe config. Same bug class as #16. **ESCALATED 2026-07-29** — `.env` itself was git-tracked; every `git reset --hard` re-stomped it. Untracked in 6463cb7 + config validator hard-ignores env universe overrides. CAUTION: the first reset after untracking DELETED server .env (restored from /tmp backup). **API secrets live in git history — rotation still pending.**
  18. **2026-07-29** — SPCX carries duplicate reduce-only trailing stops (2× same stopPrice 118.8 + 1 stale 126.8). Harmless (first trigger wins, rest no-op) but trail-replace should cancel the old order every cycle — watch `native_trailing_stop_replaced` for missed cancels.

## Deferred — Aster Program (Phase 1 pushed 2026-08-15, inert until keyed)
  1. **Hedge triggers (Phase 2)** — (a) funding-spread arb: long negative-funding venue /
     short positive (Aster funding history endpoint vs funding_history.json); (b) cascade
     tail-hedge: offset on Aster instead of closing into thin SoDEX book at extreme z,
     unwind both at normalization; (c) mark-price basis convergence pair when
     |SoDEX−Aster mark| > fees+slippage. All need hedge-mode account + shadow-journal data.
  2. **Dead-man refresh loop** — aster_deadman_seconds > 0 needs a supervised loop calling
     set_deadman_switch per active symbol. Client method exists; loop unwired by design.
  3. **Aster stock perps** — near-24/7 incl. weekends, EWMA-smoothed marks, ±5% off-hours
     cap, taker 0.009%. Candidate hedge/execution venue for SoDEX equity legs — needs
     symbol universe research (which tickers overlap SoDEX's 17 TradFi symbols).
  4. **$ASTER fee token** — 5% fee discount paying in $ASTER; requires spot acquisition +
     perp-wallet transfer ops. Revisit when Aster notional > $5k/day.
  5. **Router v2 scoring** — today: static symbol partition (venue.py). Phase 2: dynamic
     score = fee + slippage_at_size + funding_carry + venue_health(ADL quantile, WS
     staleness) for dual-listed symbols; every routing decision shadow-journaled.
  6. **User data stream** — Aster account updates over WS (listenKey keepalive) replacing
     REST position polling for the Aster sleeve; latency + rate-limit relief.

## Deferred — Bybit Program (revisit at 2-week review 2026-08-13, or earlier if behavior demands)
  1. **Chancellor full venue partition** — interim fix live (sleeve self-halt at 30% sleeve DD). Full version: kingdom DD computed per-venue; revisit when Bybit equity > $200.
  2. **Rally graduation size expression on Bybit** — graduation's 2.0× size is currently clamped away by venue pct-sizing. Fix: graduated symbol → 2× `bybit_margin_pct`, hard ceiling 25% of venue equity per trade.
  3. **Position cap partition** — today 7 shared across venues (Bybit can take 5 of 7). At scale: 7 SoDEX + 5 Bybit, independent.
  4. **Campaign mode on Bybit** — if a Bybit tournament is entered: `campaign_min_notional_usd` ($250) conflicts with pct-sizing ($50 at $100) — resolve before activating any Bybit campaign symbol.
  5. **Phase 2 dynamic universe selector** — weekly re-rank of `bybit_assets` from journal per-symbol WR + turnover/OI-growth/liq-frequency (all collected from 2026-07-30). Discovery score = relative volume + OI growth + funding stability + ATR expansion (holder/TVL + social deliberately cut — no new feeds).
  6. **Phase 3.5 liquidation stream hardening** — (a) 250ms event batching per symbol (10-50× load cut, z-score preserved since notional-sums commute); (b) baseline contamination: replace rolling-deque z baseline with multi-day hourly percentile so sustained cascades don't blind the engine mid-event; (c) Binance `forceOrder` breadth: z × venues_confirming / venues_tracked.
  7. **Bench candidates** (liquidity-verified 2026-07-30, add on review): JUP, WLD. Rejected: TRIA/SPACE (<$1M turnover), FET/AI16Z (dead ticker data mid-migration).
  8. **Key hygiene** — Bybit API key pasted in chat 2026-07-30: IP-bind to the server IP on Bybit; withdrawals already disabled. SoDEX secrets still pending rotation (issue #17).

## Deferred — Other (from 2026-07 sessions)
  - SPCX pre-US sub-cap (proposed main.py:2837 07-30, never approved)
  - Darvas box detector, Simons pairs (Phase 4 strategy work)
  - Cascade ABI decode (replace 6/18-decimal notional guessing in valuechain_monitor)
  - Moonshot key recharge (watchdog LLM blind)

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

### Tri-Frame Thinking (Philosopher + Psychologist + Quant Engineer)
Every non-trivial task on ARIA runs through three frames. New Claude instances:
these are not optional garnish — they are how this project thinks. Live capital
is at stake; a frame you skip is the frame that bills you.

**🔍 PHILOSOPHER — "What is true, and why?"**
  - Root cause vs symptom. The empty-body 401 is never the disease — find whose
    IP whitelist, whose schema migration, whose missing event publish.
  - Second-order effects: what does this change break downstream? Who else reads
    this field, this event, this global?
  - Axioms: Chancellor is absolute. Journal is permanent. Fail closed, never open.
  - Grep first, fix later. A hypothesis without a log line is gossip.

**🧠 PSYCHOLOGIST — "Who is emotional, and how does that distort the system?"**
  - The MARKET: fear/greed, liquidation hunts, crowd positioning. Cascades are
    mass psychology made visible — Tier 4/6 exist to read them.
  - The OPERATOR (Dayo): ALL-CAPS = urgency, not precision. When the operator is
    scared (balance zeroed) or euphoric (winning streak), requests skew toward
    action. Your job is correct action, not fast action. Say the hard thing.
  - The SYSTEM: ARIA has moods — recovery mode, meta-cognition blocks, drawdown
    multipliers, conviction decay. A halted system is not a broken one; read its
    state before prescribing. Tilt exists in machines too: after losses the gates
    tighten; after wins the size grows. Know whether the system is thinking
    clearly before you let it trade.
  - Anticipate regret asymmetry: a missed trade is recoverable; a blown account
    is not. When in doubt, the gate stays shut.

**📊 QUANT ENGINEER — "What do the numbers say, at what scale, with what EV?"**
  - Probabilistic impact: WR × payoff, not win counts. Expectancy per trade,
    per gate, per symbol. BTC at 22.5% WR with 0.45 payoff is a bleed, not a
    strategy — the data decides, not the narrative.
  - Number scales: drawdown in PERCENT (8.0 not 0.08), sizes in USD notional,
    fees in bps. Mixing scales is the classic kill shot.
  - Statistical discipline: 3 trades is noise; 30 is a pattern; 300 is policy.
    Never tune a threshold on a sample that fits in one screenshot.
  - Test before deploy: baseline is 29F/1279P (#12). A fix that can't show its
    diff and its tests doesn't ship.

Execution Order:
  1. Philosopher — is my model of the problem true? Root cause proven from logs?
  2. Psychologist — what does this change do to a system/operator under stress?
     Is this request itself coming from fear or greed?
  3. Quant — does the EV survive fees, slippage, and the sample size?
  4. Fix only if all three pass — smallest change, comment the WHY, run tests.
  5. Verify within 60s — logs, not hope.

Output Format:
```
🔍 Philosopher: [assessment]
🧠 Psychologist: [market/operator/system state]
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

## North Star — The Small Autonomous Fund (set 2026-08-19)
The architecture is already organizational — what evolves is the staffing.
Build toward this SLOWLY, as data proves each step. Nothing here jumps the
queue ahead of positive-expectancy evidence in the journal.

1. **Treasury → ALM desk.** Funding-spread arb, cross-venue collateral
   optimization, Aster margin-eligibility — the accounting department becomes
   the treasurer that MAKES money on the balance sheet, not just protects it.
2. **Shadow journal → counterfactual engine → gate self-tuning.** The data is
   being collected; day-type slicing was the first consumer. Endpoint: gates
   that retune within shrinkage-bounded limits, watchdog as auditor.
3. **Watchdog + digest → research department.** Today it hunts leaks; the
   EV-scan discipline (materiality bars, n≥10 counterfactuals) is already the
   scientific method. Give it the mandate to propose NEW signals against
   shadow-journal evidence and it becomes an alpha researcher.
4. **Router v2 → best execution.** The shadow-dual dataset answers "which
   venue" with data; graduation flips routing when earned.
5. **The kingdom as a fund.** ARIA (execution + treasury), AUGUR (revived,
   strategy sleeve), AI FM (PM above them), Chancellor (risk committee).
   Departments: research, trading, accounting, risk, ops — each already has
   a seed in the codebase.

### Org chart and growth doctrine (set 2026-08-19 — VSM/Beer, Simon, Ashby)
  Kingdom map (Viable System Model): S1 operations = execution sleeves (SoDEX
  book, Aster sleeve, future AUGUR); S2 coordination = router, param store,
  cooldown registries; S3 control = Treasury; S3* audit = shadow journal +
  watchdog; S4 intelligence = EV scan / digest / future research dept; S5
  policy = Chancellor + these hard rules; algedonic channel = Chancellor veto
  + watchdog fix authority. One VSM function per department — if a module does
  two, split it. Every NEW department MUST follow
  `docs/DEPARTMENT_TEMPLATE.md`: zero-I/O brain module with injected callables,
  exactly one splice point in main.py, a kill switch whose False state
  reproduces the pre-module system exactly, own telemetry namespace, own test
  file — plus the variety budget (kill switch + telemetry + digest coverage +
  designed-events entry ship WITH the module; no unobserved degrees of
  freedom).

### The 90-day gate, statistically defined (set 2026-08-19 — Aronson)
  "Positive expectancy" is a test, not a phrase: a bootstrap 95% CI (≥10k
  resamples) over journal daily PnL must EXCLUDE zero before any North-Star
  step graduates from doctrine to trading behavior. Any retune/research
  proposal must carry n, effect size, and a multiple-comparison
  acknowledgment (how many slices were examined before this one looked
  significant). One good week proves nothing.

Full potential: strategies proposed by research, sized by conviction,
executed by venue-optimal routing, accounted by the treasury, vetoed by the
Chancellor, audited by the watchdog — Dayo as GP reading the digest over
coffee instead of grepping logs at 1am.

The honest gap between here and there isn't code. It's ~90 days of the
current machine proving, in the journal, that its expectancy is positive.
Plumbing is done. Now the numbers have to speak.

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
