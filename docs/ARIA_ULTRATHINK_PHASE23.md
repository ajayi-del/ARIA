# Ultrathink: ARIA Phase 2→3 Architectural Synthesis
## Brain, Organs, Muscles, and the Missing Nerves

**Date:** 2026-06-06  
**Scope:** Signal Arbiter (Phase 2) → AI Fund Manager (Phase 3) integration  
**Constraint:** Live capital. No deletions. Tests pass before restart.

---

## 1. The Anatomy (What Exists Now)

### Sensory Layer (Nerves)
| Source | File | Data |
|--------|------|------|
| Bybit WS | `data/bybit_feed.py` | candles, OB, trades, funding, liquidations |
| SoDEX WS | `execution/sodex_client.py` | mark_price, fills, positions |
| SSI | `data/ssi_feed.py` | MAG7/DEFI/MEME sector rotation |
| Calendar | `risk_calendar/engine.py` | BLOCK/CAUTION/CLEAR + hours_to_event |
| ValueChain | `intelligence/valuechain_monitor.py` | on-chain liquidation cascade |

### Perceptual Cortex (Signal Assembly)
```
IntelligenceInterpreter._build_and_publish()  [interpreter.py:724]
    ├─ SignalGenerator._last_components        [tier scores T1-T9]
    ├─ _build_tier_directions()                [interpreter.py:1036]
    ├─ SignalArbiter.resolve()                 [interpreter.py:754]  ← NEW
    └─ Enhancement Layer                       [interpreter.py:781]
```

The **SignalArbiter** (`intelligence/signal_arbiter.py`) is the new prefrontal filter. It receives:
- `tier_directions`: {"microstructure": "short", "regime": "long", ...}
- `components`: raw scores per tier
- `regime`, `calendar_regime`, `cascade_zscore`

It outputs `ArbiterResult` with:
- `direction`: winning side
- `confidence`: winning coalition strength (NOT scalar sum)
- `dominant_tier`: who decided
- `suppressed_tiers`: who was overruled
- `resolution_rule`: which rule fired

### Limbic System (Emotional State)
| Component | File | Role |
|-----------|------|------|
| XAUTThermometer | `intelligence/regime_engine.py:84` | Gold = fear compass |
| RegimeMultiplierEngine | `intelligence/regime_engine.py:46` | Regime → size mult |
| DrawdownManager | `risk/drawdown_manager.py` | Pain receptor |
| SessionDrawdownTracker | `main.py:432` | Session-level P&L tracking |

### Philosophical Stack (Decision Cortex)
```
KantEngine.assess()           [main.py:3925]
    → KantFrame (structure, size_cap, order_type)
    
NietzscheEngine.compute()     [main.py:4290]
    → NietzscheOutput (will_state, size_multiplier, adjusted_size)
    
WillEngine.compute()          [main.py:4380]  ← NEW
    → WillVerdict (will_probability, size_scale, order_type_override)
```

### Environmental Classifier (New Brain Region)
```
WorldModel.update()            [world_model_loop @ main.py:8158]
    → WorldState (risk_appetite, preferred_asset_class, volatility_regime, ...)
    
world_model_loop runs every 30s, reads:
    - regime_engine.last_state()
    - dd_tracker.session_drawdown_pct
    - _last_calendar_state.regime
    - _xaut_thermometer.last_direction
    - position_manager.get_all()  (for portfolio concentration)
```

### Memory Systems
| Store | File | Persistence | Contents |
|-------|------|-------------|----------|
| ParamStore | `memory/param_store.py` | JSON disk | stop_mults, coherence_threshold, session_weights, **ai_params (NEW with TTL)** |
| RegimeMemory | `intelligence/regime_memory.py` | JSON disk | empirical WR per (regime, tier, asset_class) |
| TradeJournal | `vault/journal.py` | SQLite | trade outcomes, P&L, hold times |
| PredictionStore | `intelligence/prediction_market.py` | In-memory + drain | cross-agent bets |

---

## 2. The Neural Pathways (How Data Flows)

### Pathway A: Regular Signal (Hot Path)
```
1. on_signal_ready(event)                       [main.py:2292]
   ├─ interpreter._build_and_publish(symbol)    [interpreter.py:639]
   │  ├─ SignalArbiter.resolve()               [interpreter.py:754]
   │  │   ├─ Rule 1: calendar BLOCK?           [signal_arbiter.py:105]
   │  │   ├─ Rule 2: cascade z>3?              [signal_arbiter.py:115]
   │  │   ├─ Rule 3: sweep + weak macro?       [signal_arbiter.py:120]
   │  │   └─ Rules 4-7: general conflict       [signal_arbiter.py:125]
   │  └─ _arb_base = _arb_res.confidence       [interpreter.py:772]
   │     (feeds Enhancement Layer base)
   │
   ├─ build_candidate(state, ...)              [main.py:8788]
   │  └─ stop_atr_mult = param_store.get_stop_mult(symbol)
   │
   ├─ kant_engine.assess(...)                  [main.py:3925]
   │  └─ KantFrame(size_cap, order_type)
   │
   ├─ nietzsche_engine.compute(...)            [main.py:4290]
   │  └─ NietzscheOutput(adjusted_size, ...)
   │
   ├─ will_engine.compute(...)                 [main.py:4380]  ← NEW
   │  ├─ inputs: KantFrame + NietzscheOutput + WorldState
   │  ├─ will_probability == 0.0 ? → veto      [main.py:4395]
   │  ├─ size_scale applied to candidate       [main.py:4408]
   │  ├─ order_type_override applied           [main.py:4418]
   │  └─ writes to param_store (audit)         [will_engine.py:audit]
   │
   └─ client.place_bracket(BracketOrder)       [main.py:4860]
```

**Latency budget:** Interpreter (~5ms) + Arbiter (~0.1ms) + Kant (~0.05ms) + Nietzsche (~0.1ms) + WillEngine (~0.1ms) + build_candidate (~1ms) = ~7ms before dispatch. Bracket execution 50-300ms.

### Pathway B: Cascade Aftermath (Cold Path — The Architectural Leak)
```
_evaluate_cascade_aftermath()                   [main.py:1387]
    └─ asyncio.create_task(_execute_cascade_aftermath(direction))
        
_execute_cascade_aftermath(direction)          [main.py:2002]
    ├─ Builds MarketState manually (hardcoded macro_bias="neutral")
    ├─ build_candidate(_state, cascade_phase="aftermath")
    ├─ Oracle fusion
    ├─ Symbol edge throttle
    ├─ Session weight
    ├─ Nietzsche win-rate cap (manual call to _win_rate_band)
    ├─ Dynamic leverage fallback
    ├─ _select_order_type()
    └─ client.place_bracket()                  [main.py:2243]
```

**Critical gap:** This path bypasses ENTIRELY:
- SignalArbiter (no tier directions to resolve)
- Enhancement Layer (no HTF bias, no cross-market boost)
- KantEngine.assess() (no structure frame)
- WillEngine (no environmental modulation)
- Direction lock

The aftermath constructs a synthetic `MarketState` with hardcoded fields:
```python
macro_bias="neutral", macro_source="cascade_aftermath", macro_confidence=1.0,
regime="risk_on" if direction == "long" else "risk_off",
market_type="expansion",
weighted_score=8.0, coherence_score=8.0,
```

This is a **phantom brain** — it pretends to have high conviction (8.0) but has none of the actual signal validation that the hot path receives.

### Pathway C: Cascade Momentum (Same Bypass)
`_execute_cascade_momentum()` at `main.py:1610` has the identical structure — manual MarketState, manual build_candidate, direct bracket placement.

---

## 3. The Feedback Loops (Cybernetics)

### Loop 1: TradeOutcome → RegimeMemory → SignalArbiter
**Status:** Built but NOT wired.

`RegimeMemory.record_trade()` exists (`intelligence/regime_memory.py:116`) but is **never called**. The trade journal closes trades, but no code reads journal outcomes and writes them to RegimeMemory.

**Fix required:** In the trade outcome recorder (where `journal.update_outcome()` is called), add:
```python
regime_memory.record_trade(
    regime=trade_regime,
    dominant_tier=trade_dominant_tier,  # from arbiter result
    asset_class=asset_class,
    pnl=trade_pnl,
    hold_min=hold_time_minutes,
)
```

**Impact:** After ~10 samples per (regime, tier), the arbiter stops using static `_STATIC_EDGE_TABLE` and switches to empirical win rates. This is the core self-learning loop for signal quality.

### Loop 2: JournalAnalytics → NietzscheEngine.adapt()
**Status:** WIRED and ACTIVE.

`nietzsche_engine.adapt(analytics)` is called in `main.py:5159` and `main.py:7976`.
`JournalAnalytics` computes Kelly-optimal multipliers per (dd_band, streak_band).
Nietzsche blends 50% old static + 50% new empirical.

**This is the only live cybernetic loop in the system today.**

### Loop 3: WorldModel → WillEngine → ParamStore
**Status:** WIRED but WRITE-ONLY.

WillEngine writes to ParamStore:
- `will_probability`
- `will_size_scale`
- `will_asset_class_boost`
- `will_confidence_override`

But **no downstream component reads these keys from ParamStore.** The application is direct (in `on_signal_ready`), not via ParamStore read. This means the ParamStore acts as an audit log, not as a control surface.

**Gap:** If we want the AI Fund Manager to adjust parameters that other agents read (e.g., `stop_mult_override`, `coherence_floor_override`), those writes happen but are never consumed.

### Loop 4: ParamStore → build_candidate
**Status:** PARTIALLY WIRED.

`build_candidate()` reads `param_store.get_stop_mult(symbol)` (`main.py:8848`).
But it does NOT read:
- `param_store.get_ai_param("leverage_override")`
- `param_store.get_ai_param("atr_min_pct_override")`
- `param_store.get_ai_param("coherence_floor_override")`

**Gap:** The AI Fund Manager can write leverage overrides, but `build_candidate` ignores them.

---

## 4. The Evolution Gaps (What's Missing for Growth)

### Gap A: Aftermath/Momentum Bypass the Philosophical Stack
**Severity:** HIGH (live capital at risk)

The cascade paths are designed as high-EV events. But they execute without:
- WorldModel risk_appetite check (could enter during BLOCK calendar)
- WillEngine veto (could enter when liquidity is "thin")
- SignalArbiter (no tier resolution — just hardcoded "expansion" regime)
- Kant structure assessment (no size_cap validation)

**Fix:** Route aftermath/momentum through `on_signal_ready` by constructing a proper event and submitting it to the event bus. Or, at minimum, call `will_engine.compute()` in the cascade coroutines before `client.place_bracket()`.

### Gap B: RegimeMemory Has No Data Pipeline
**Severity:** MEDIUM (prevents empirical learning)

`RegimeMemory` is instantiated in `interpreter.py:92` and passed to `SignalArbiter`. But `record_trade()` is never called. The static edge table is used forever.

**Fix:** Hook `record_trade()` into the trade outcome closure path. This requires tracking `dominant_tier` per position (from ArbiterResult) and passing it through to the journal.

### Gap C: ParamStore AI Params Are Write-Only
**Severity:** MEDIUM (AI Fund Manager is toothless)

The AI Fund Manager writes parameters, but the execution pipeline doesn't read them. This is like a brain sending signals to muscles that have no receptors.

**Fix:** Extend `build_candidate()` and `risk_engine.validate()` to read AI params from ParamStore:
```python
# In build_candidate()
_ai_lev = param_store.get_ai_param("leverage_override")
if _ai_lev is not None:
    candidate.leverage = int(_ai_lev)

# In risk_engine.validate()
_ai_coh = param_store.get_ai_param("coherence_floor_override")
if _ai_coh is not None:
    min_coherence = max(min_coherence, _ai_coh)
```

### Gap D: No Feedback from Execution Quality to WorldModel
**Severity:** LOW-MEDIUM (missed adaptation opportunity)

If orders are rejected (slippage, insufficient margin, invalid stop), or if stops hit within 60s, this is environmental feedback. The WorldModel should learn:
- "liquidity is thinner than expected" → `liquidity_regime = "thin"`
- "stops are too tight for this vol" → `volatility_regime = "extreme"`

Currently, execution failures are logged but do not feed back into the environmental classifier.

**Fix:** Add an `execution_feedback()` method to WorldModel that takes (symbol, order_status, fill_slippage, time_to_stop_hit) and adjusts internal state. Call it from the outcome recorder.

### Gap E: Missing Portfolio Allocator
**Severity:** MEDIUM (asset allocation is emergent, not designed)

WorldModel outputs `preferred_asset_class`, but there is no enforcement. The system can still accumulate 5 equity positions during a "crypto-preferred" regime because each signal is evaluated independently.

**Fix:** Build `intelligence/portfolio_allocator.py` with `target_weights(regime, leading_sector, dd_pct)`. Enforce in the execution router before `place_bracket`:
```python
current_crypto_weight = sum(p.notional for p in positions if p.asset_class == "crypto") / balance
if current_crypto_weight + candidate.notional / balance > target_weight + 0.05:
    candidate.size = 0  # defer or reduce
```

### Gap F: Missing Autonomous Loop
**Severity:** LOW (future capability)

Per `CLAUDE.md`, the full AI Fund Manager has three loops:
1. Fast (per signal) → DONE (WillEngine)
2. Slow (30min) → DONE (WorldModel)
3. Autonomous (self-generated) → MISSING

The autonomous loop would generate its own signals (e.g., "close 50% of NVDA because equity weight is 80% and crypto is outperforming") without an external market trigger.

---

## 5. Strategic Sync: How the AI Fund Manager Should Integrate

### Immediate (This Week)

**1. Wire RegimeMemory to trade outcomes.**
File: `main.py` (wherever `journal.update_outcome()` is called)
Action: After outcome recorded, call `interpreter._arbiter.regime_memory.record_trade(...)`.

**2. Extend build_candidate to read AI ParamStore overrides.**
File: `main.py:build_candidate()`
Action: Read `leverage_override`, `coherence_floor_override` from ParamStore and apply.

**3. Add WillEngine call to cascade aftermath/momentum.**
File: `main.py:_execute_cascade_aftermath()` and `_execute_cascade_momentum()`
Action: After building candidate, before `place_bracket`, call `will_engine.compute()` with a synthetic WorldState or the current `_last_world_state`.

### Short-Term (Next 2 Weeks)

**4. Build Portfolio Allocator.**
File: `intelligence/portfolio_allocator.py`
Action: Compute target weights from WorldState. Enforce in execution router.

**5. Add execution feedback to WorldModel.**
File: `intelligence/world_model.py`
Action: New method `execution_feedback(symbol, slippage, rejected, time_to_stop)` that adjusts `liquidity_regime` and `volatility_regime`.

**6. Build Calendar Intelligence v2.**
File: `risk_calendar/engine.py`
Action: Add `get_directional_bias(symbol, event_type)` and `get_volatility_forecast(symbol, event_type)` so earnings events produce pre-positioning signals, not just gates.

### Medium-Term (Next Month)

**7. Build Sector Rotation Detector.**
File: `intelligence/sector_rotation.py`
Action: Detect lagging sectors from SSI feed. Generate catchup trades.

**8. Build Cascade Anticipation (cascade_buildup.py).**
File: `intelligence/cascade_buildup.py`
Action: 5-signal prediction (velocity, acceleration, purity, size growth, funding) 60-120s before ValueChain detects cascade.

**9. Build autonomous loop in AI Fund Manager.**
File: `intelligence/ai_fund_manager.py`
Action: Self-generated signals for rebalancing, portfolio TP, correlation trades.

---

## 6. The Philosophy

The market is not the brain. The market is **feedback**.

The brain is the stack: Arbiter → Kant → Nietzsche → World → Will.
The organs are the engines: regime, macro, microstructure, funding, calendar.
The muscles are execution: bracket orders, stops, position tracking.
The nerves are the data feeds: Bybit, SoDEX, SSI, calendar, on-chain.

**What makes it alive is the feedback loops.**
- Trade outcomes → RegimeMemory → Arbiter ("micro was wrong in transitioning; macro was right")
- Journal analytics → Nietzsche adapt ("0.85x is better than 1.0x in 3-5% drawdown")
- Execution slippage → WorldModel ("this symbol is illiquid right now")
- WorldModel → WillEngine → ParamStore → build_candidate ("reduce size, widen stops")

**What we built today is the prefrontal cortex and the hippocampus.**
- WorldModel = prefrontal cortex (environmental awareness)
- WillEngine = anterior cingulate (conflict resolution between desire and caution)
- ParamStore AI = hippocampus (short-term adaptive memory with decay)
- RegimeMemory = basal ganglia (habit learning — what worked in this regime before)

**What is still missing is the cerebellum (fine motor control) and the autonomic nervous system (self-regulation without conscious thought).**

The cerebellum is the portfolio allocator — smooth, coordinated movement across asset classes. The autonomic system is the autonomous loop — heartbeat-level rebalancing that doesn't need a market trigger.

---

## 7. Concrete Next Actions (Ordered by Risk/Reward)

| Priority | Action | File | Test |
|----------|--------|------|------|
| P0 | Wire RegimeMemory.record_trade() to outcome closure | `main.py` | `test_regime_memory_learning` |
| P0 | Add WillEngine to cascade aftermath/momentum | `main.py` | `test_aftermath_will_veto` |
| P1 | Read AI params in build_candidate | `main.py:build_candidate()` | `test_ai_param_override` |
| P1 | Build PortfolioAllocator | `intelligence/portfolio_allocator.py` | `test_portfolio_weights` |
| P2 | Add execution_feedback to WorldModel | `intelligence/world_model.py` | `test_execution_feedback` |
| P2 | Calendar v2 directional bias | `risk_calendar/engine.py` | `test_calendar_bias` |
| P3 | Sector rotation detector | `intelligence/sector_rotation.py` | `test_sector_lag` |
| P3 | Cascade anticipation | `intelligence/cascade_buildup.py` | `test_cascade_predict` |
