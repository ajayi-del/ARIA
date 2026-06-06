# ARIA Implementation Plan v2.0
## Hierarchical Signal Arbiter + Unified Execution Router

**Status:** Phase 1 Hot Fixes In Progress  
**Last Updated:** 2026-06-06  
**Architect:** Kimi K2.6 via Claude Code  
**Constraint:** Live capital on SoDEX mainnet. No deletions. Tests pass before restart.

---

## 0. Diagnosis Summary (Verified from Source)

ARIA holds 5 open positions (SILVER, NVDA, TSLA, GOOGL, AAPL) with `max_concurrent_positions=3`. This is not a deliberate equity bias. It is an emergent outcome of verified architectural defects:

| Defect | Location | Impact |
|--------|----------|--------|
| **Position cap bypass** | `main.py:7870` — Sovereign agent calls `client.place_bracket()` directly without cap check | Allows auxiliary agents to overfill portfolio, blocking regular pipeline |
| **Aftermath cold path** | `main.py:2749` — Aftermath sets `_aftermath_primed=True` but has NO dedicated execution coroutine | High-EV aftermath windows expire unused (9 primed events, 0 executions) |
| **Stop fallback bug** | `main.py:4192` — If native stop fails, `stop_price=0.0`; software guardian uses generic 1.5% instead of intended stop | AAPL entered at 304.1 with no native stop; software stop at 1.5% may be wrong distance |
| **Negative free margin** | `logs/aria.log` — `free=-6359.30` on ~$238 balance during crypto entry attempts | Blocks crypto at the exact moment signals fire; possible margin calc bug or transient state |
| **Crypto orphaned by Tier 8** | `intelligence/macro_signals.py` — NVDA/META/MSFT 4H HTF is the only crypto-equity bridge | Mixed equity signals (GOOGL/TSLA/AAPL short, NVDA long) produce no clear cross-market bias |

---

## 1. Target Architecture

Replace the current "pipeline with bypasses" with a **Hierarchical Signal Arbiter + Unified Execution Router**.

```
LAYER 0: RAW DATA (candles, OB, trades, funding, OI, liquidations, calendar)
    |
    v
LAYER 1: SIGNAL ARBITER (NEW)
    ┌─────────────┐ ┌─────────────┐ ┌─────────────┐ ┌─────────────┐
    │  Macro Arb  │ │  Micro Arb  │ │ Cascade Arb │ │ Calendar Arb│
    │  (SSI/T1-2) │ │   (T3-4)    │ │   (T6-7)    │ │(T2/earnings)│
    └──────┬──────┘ └──────┬──────┘ └──────┬──────┘ └──────┬──────┘
           │               │               │               │
           └───────────────┴───────┬───────┴───────────────┘
                                   v
                    ┌─────────────────────┐
                    │  CONFLICT RESOLVER  │  ← Conditional, not scalar sum
                    └──────────┬──────────┘
                               v
LAYER 2: BELIEF STATE MACHINE (Kant v2)
    Market structure + regime + drawdown + streak → unified belief state
                               v
LAYER 3: PORTFOLIO ALLOCATOR (NEW)
    Dynamic target weights: crypto / equity / commodity by regime
                               v
LAYER 4: UNIFIED EXECUTION ROUTER (REFACTORED)
    Single entry point: `execute_candidate()`
    All agents submit CandidateRequests to a priority queue.
    Router applies: position cap, margin, calendar, L4 spread gate.
```

### Design Principles

1. **Single Execution Router** — No agent calls `place_bracket()` directly. All agents submit to a unified queue.
2. **Conditional Signal Resolution** — When macro says "long" and micro says "short", check empirical win rate for "macro_override_micro" in this regime, not average them.
3. **Dynamic Asset Allocation** — Crypto vs equity weight is a portfolio decision, not an emergent property of per-symbol gates.
4. **L4 Gate for All Entries** — Every entry (not just cascade) checks `is_exit_safe()` before dispatch. If SoDEX spread > 2x baseline, defer.
5. **Calendar as Signal, Not Just Gate** — Earnings dates produce directional bias + vol forecast, not just BLOCK/CAUTION.

---

## 2. Phase 1: Hot Fixes (Week 1) — Stop the Bleeding

### 1.1 Fix Position Cap Enforcement

**File:** `main.py`  
**Problem:** Sovereign agent (line ~7870) and cascade momentum (verified at line 1672) bypass `max_concurrent_positions`. The sovereign agent has NO cap check. The cascade momentum has one but the sovereign path allows overfill.

**Fix:**
- Extract `_active_position_count()` helper that returns `len(position_manager.get_all()) + len(_pending_entry_symbols) + arb_count`.
- Call it in `_sovereign_signal_loop()` before bracket construction.
- Call it in `_execute_cascade_momentum()` (already present — verify it uses same formula as regular pipeline).
- Edge case: Basket TP closes reduce count. Ensure cap check uses live `get_all()` at call time, not cached.

**Quant Impact:** Prevents 5th+ positions from blocking the regular Nietzsche/Kant pipeline, which is the primary crypto entry path.

### 1.2 Fix Aftermath Execution Cold Path

**File:** `main.py`  
**Problem:** `cascade_aftermath_primed` fires but there is NO handler that executes trades. The system passively waits for a regular signal to coincidentally match the aftermath direction within the 5-minute window. In low-activity regimes, nothing fires.

**Fix:**
- Add `async def _execute_cascade_aftermath(direction: str)` mirroring `_execute_cascade_momentum()`.
- Trigger it from `_evaluate_cascade_aftermath()` when priming is confirmed:
  ```python
  if confirmed >= _aftermath_needed:
      asyncio.create_task(_execute_cascade_aftermath(primed_direction))
  ```
- Also trigger from `_on_cascade_aftermath()` event handler.
- Constraints:
  - Use 0.25x size (same as cascade momentum).
  - Require L4 confirmation via `_cascade_basket.rank_entry_symbols()`.
  - 120s expiry from task creation.
  - Check position cap before entry.
  - Build candidate with `cascade_phase="aftermath"` so downstream tagging (lines 2749-2799) can apply overrides OR build candidate directly in the coroutine with aftermath stops/TPs pre-computed.

**Quant Impact:** Aftermath is explicitly designed as "higher-EV than momentum entry" (Type B recovery). 9 primed events with 0 executions = complete waste of alpha.

### 1.3 Fix Missing Stop Fallback

**File:** `main.py` (`_bracket_task`)  
**Problem:** When `place_bracket` succeeds but native stop fails, the Position is created with `stop_price=0.0`. The reconciliation loop then sets a generic 1.5% software stop. For equities with wide ATR, this may be wrong. Also, slow fills (>20s) on equities may race with the 2s deferred retry.

**Fix:**
- **Always set `stop_price=candidate.stop_price`** on the Position object, even if native stop placement failed. The software guardian should protect at the *intended* stop distance, not a generic 1.5%.
- **Add second deferred retry:** After the 2s retry fails, schedule a 10s retry with widened stop (1.5x `_enforce_min_stop_distance`). This handles slow equity fills where SoDEX needs more time to settle.
- If all retries fail, log `CRITICAL` and rely on software guardian with the intended stop.

**File:** `execution/sodex_client.py`  
**Problem:** `_place_native_stop_order` retries once on `"stopPrice is invalid"` with 1.5x multiplier, but if the retry also fails, the bracket still returns `success=True` with `stop_order_id=None`.

**Fix:** No change needed in `sodex_client.py` — the retry logic is correct. The fix belongs in the caller (`_bracket_task`) to persist the intended stop_price for the guardian.

### 1.4 Fix Margin Sanity Check

**File:** `main.py` / `risk/margin_engine.py`  
**Problem:** Log shows `free=-6359.30` on ~$238 balance. This is either a scaling bug or transient over-leverage state. Negative free margin blocks ALL entries at the moment of highest opportunity.

**Fix:**
- Add defensive check in `build_candidate()` before margin evaluation: if `balance > 0 and free_margin < -balance * 0.1`, log `margin_calculation_anomaly` and use `free_margin = 0` for the decision (conservative, prevents negative blocking).
- If `free_margin` is negative but small (within 10% of balance), treat as zero and allow entries that require less than the small remaining capacity.
- Investigate root cause: `used=6599.29` with balance=$238 suggests the margin engine is reporting notional as margin, or leverage is being ignored in the free calc.

**Note:** This fix is a band-aid. Root-cause margin calc audit is Phase 2.

---

## 3. Phase 2: Signal Arbiter Refactor (Weeks 2-3)

### 2.1 Build `intelligence/signal_arbiter.py`

```python
class SignalArbiter:
    """
    Hierarchical conflict resolver for competing signal tiers.

    Rules (configurable via param_store):
      - If cascade_phase in ("expansion", "exhaustion") AND zscore > 3.0:
          -> Cascade direction overrides macro. Micro gets 0.3 weight.
      - If macro_confirmation > 0.8 AND micro_direction opposes:
          -> Check historical WR for "macro_override_micro" in this regime.
          -> If WR > 0.55, macro wins. Else micro wins.
      - If calendar.regime == "BLOCK":
          -> Hard block regardless of other signals.
      - If post_event_alpha is active (within 30min of macro event):
          -> Boost first confirming signal by 1.5x.
    """
```

### 2.2 Integrate Arbiter into `_build_and_publish()`

- Replace scalar weighted sum with arbiter resolution.
- Output: `resolved_direction`, `confidence`, `dominant_tier`, `suppressed_tiers`.
- Log suppressed tiers for audit.

### 2.3 Add `intelligence/regime_memory.py`

- Track empirical win rate per `(regime, dominant_tier, asset_class)`.
- Updated nightly from trade journal.
- Used by arbiter rule selection.

---

## 4. Phase 3: Portfolio Allocator + Calendar v2 (Weeks 4-5)

### 4.1 Build `intelligence/portfolio_allocator.py`

```python
class PortfolioAllocator:
    def target_weights(self, regime: str, leading_sector: str, dd_pct: float) -> Dict[str, float]:
        # Example: transitioning + index_tech leading -> 50% equity, 30% crypto (BTC/ETH only), 20% commodity
        # Example: alt_season -> 60% crypto, 30% equity, 10% commodity
```

### 4.2 Enforce Allocation in Execution Router

Before dispatching a candidate:
```python
current_weight = sum(p.notional for p in positions if p.asset_class == "crypto") / balance
if current_weight + candidate.notional / balance > target_weight + 0.05:
    defer or reduce size
```

### 4.3 Calendar Intelligence v2

- Extend `CalendarEngine` with:
  - `get_directional_bias(symbol, event_type) -> "long" | "short" | "neutral"`
  - `get_volatility_forecast(symbol, event_type) -> implied vol multiplier`
- Use in arbiter: pre-earnings signals get vol-adjusted sizing (smaller size, wider stop).

---

## 5. Phase 4: AI Fund Manager Phase 1 (Weeks 6-8)

Per `CLAUDE.md` roadmap. Dry-run first.

### 5.1 `intelligence/world_model.py`
- Input: Macro signals, regime, funding, calendar, cascade state
- Output: `WorldState` with `risk_appetite`, `preferred_asset_class`, `volatility_regime`, `correlation_regime`

### 5.2 `intelligence/will_engine.py`
- Kant x Nietzsche x WorldState = will_probability
- Writes to param_store with TTL (expires 1h)

### 5.3 `intelligence/cascade_buildup.py`
- 5-signal anticipation: velocity, acceleration, purity, size growth, funding
- Predicts cascade 60-120s before ValueChain detects it

### 5.4 Extend `risk/param_store.py`
- Add AI-writable keys with TTL
- Fallback to `config.py` defaults on expiry

---

## 6. Phase 5: Integration & Verification (Week 9)

- Dry run: `ai_fm.dry_run = True`, log decisions, apply nothing (7 days)
- Gradual enablement: ATR adjustments -> blacklisting -> Portfolio TP -> autonomous sizing
- Tests: `python3 -m pytest tests/ -q` must pass before each deploy
- Add tests:
  - `test_signal_arbiter_conflict_resolution`
  - `test_portfolio_allocator_regime_weights`
  - `test_execution_router_position_cap`
  - `test_aftermath_execution_cold_path`

---

## 7. Immediate Action Items (Today)

1. **Fix position cap in sovereign loop** — Verified bypass at `main.py:7870`.
2. **Build `_execute_cascade_aftermath()`** — Wire into priming logic.
3. **Fix stop_price fallback** — Always persist intended stop to Position.
4. **Review margin calc** — Negative free margin on $238 balance is a red flag.
5. **Run tests** — `python3 -m pytest tests/ -q` before any deploy.

---

## Appendix: Signal Hierarchy (As-Engineered)

```
TIER 1: MAG7 SSI (USTECH100 proxy)
TIER 2: Equity Momentum / Earnings (calendar gate only)
TIER 3: Structure (ATR, baseline, market type, volume) — ~1x/min
TIER 4: Microstructure (OB imbalance, VPIN, sweep, divergence) — ~20x/sec
TIER 5: Funding Rate Regime
TIER 6: Liquidation Cascade (On-Chain) — ValueChainMonitor
TIER 7: Cross-Venue Bonus (price lag, funding spread)
TIER 8: Cross-Market Lead-Lag (NVDA/META/MSFT 4H -> crypto)
TIER 9: Flow Confirmation (buy/sell volume dominance)
```

**Critical Flaw:** Tier 8 is the ONLY crypto-equity bridge. When equities send mixed signals, crypto is orphaned. Phase 2 arbiter fixes this by making asset allocation explicit.

---

## Appendix: Execution Pipeline (With Latency)

```
CANDLE_CLOSE / OB_UPDATE
    -> Interpreter (~5ms) -> SignalGenerator (~2ms) -> MacroSignalEng (~1ms)
    -> PersonalityEng (~1ms) -> Kant Engine (~0.5ms) -> Conviction (~1ms)
    -> Nietzsche Eng (~1ms) -> Risk Engine (~2ms) -> Candidate Pool (~1ms)
    -> Bracket Order Execution (~50-300ms)
Total pre-dispatch: ~15-20ms
```

**Bypasses Found:**
- Sovereign agent: bypasses entire pipeline
- Cascade momentum: bypasses interpreter
- Aftermath: bypasses everything (no execution path at all)

Phase 1 closes the sovereign and aftermath bypasses. Phase 2 closes the architecture.
