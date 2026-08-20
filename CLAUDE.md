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
  treasury_* / trend_day_* / campaign_* knobs, or restart a HEALTHY bot with
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
  Cost guardrail: watchdog model pinned to kimi-k2.6 (NOT k3 — 3-4x pricier).
  Operator budget cap: $100/month credits. Cadence is 4h by measurement
  (2026-08-16); if costs rise, trim max-turns/prompt before cutting cadence
  further. Kill switch: touch ~/aria_watchdog/DISABLED.

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
