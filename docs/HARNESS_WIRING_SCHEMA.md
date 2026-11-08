# ARIA ↔ Prop Harness — Cybernetic Wiring Schema

Spec/docs only. ARIA stays Government. No Kant / Nietzsche / Chancellor / tier 1–6 rewrites. No live restart from this doc.

**Ground truth (Dayo 2026-11-08):** risk_ceiling LIVE on main `1252854` (+ HL ref plane `47e24da`); three harnesses up; demo keeps 3 pre-topology crypto legs; turbo silence healed (env + cursor backlog). Topology: turbo1=crypto, turbo2=tradfi, demo=all+OVERFLOW_ONLY (both turbos peers), `PROP_FAMILIES` empty.

Repo twin: `docs/HARNESS_WIRING_SCHEMA.md`. Ops summary: `docs/PROP_HARNESS_ARCHITECTURE.md` / store `prop-harness-architecture.md`.

---

## Philosophy (one screen)

| Layer | Role | Live instantiation |
|-------|------|--------------------|
| **Government** | Policy, signals, coherence, risk governance | ARIA `main.py` — emits `PropSignal`; never places propr orders |
| **Fascia** | Nervous tissue — typed envelopes + kingdom memory | Target: shared bus + `KingdomView`. Today: `signals/prop_outbox.jsonl` + per-root JSON |
| **Nations (sleeves)** | Narrative + asset class + DD budget story | `crypto` / `tradfi` / `all` via `PROP_ASSET_CLASS` |
| **Armies (accounts)** | Lifetime DD contracts; one harness process each | demo / turbo / turbo2 under `PROP_STATE_ROOT` |
| **Reference planes** | Observation that must not become risk truth | HL marks (`prop_harness/io/hl_marks.py`); ARIA HL candles (`data/hyperliquid_feed.py`) |

Second-order loop: **Governor watches sleeves watching market → outcomes → state → policy → gating.**

---

## A. Layer diagram

```
                    ┌─────────────────────────────────────┐
                    │  GOVERNMENT (ARIA)                  │
                    │  coherence · stops · Kant/Niet/Chan │
                    │  PropSignal emit (main.py:~15180)   │
                    └──────────────┬──────────────────────┘
                                   │ Fascia
                                   ▼
              ┌────────────────────────────────────────┐
              │  KINGDOM MEMORY (shared, typed)        │
              │  KingdomView · LossVeto · ArmyStates   │
              │  MarkPlaneTick (ref-only) · Outcomes   │
              └───────┬──────────────┬─────────┬───────┘
                      │              │         │
           ┌──────────▼──┐  ┌────────▼──┐  ┌───▼──────────┐
           │ Nation:     │  │ Nation:   │  │ Nation: all  │
           │ crypto      │  │ tradfi    │  │ (overflow)   │
           │ Army:turbo1 │  │ Army:turbo2│ │ Army: demo   │
           └──────┬──────┘  └─────┬─────┘  └──────┬───────┘
                  │               │               │
                  └───────┬───────┴───────┬───────┘
                          ▼               ▼
                   Propr (execution)   HL (REFERENCE only)
```

Today’s fascia is **envy-by-filesystem**: each army tails the same JSONL with its own cursor (`signal_tap.py`), filters locally (`asset_class` / `families`), peers via `prop_positions.json` (`main.py:build_positions_snapshot`, `overflow_verdict`). Target fascia is **one kingdom view, many army actuators**.

---

## B. Event & state contracts

Version every envelope: `schema: 1`. Timestamps = epoch seconds unless noted. IDs durable across restarts.

### PropSignal (Government → Fascia)
| Field | Law |
|-------|-----|
| `id`, `symbol`, `side`, `entry_ref`, `stop_price`, `ts_ms` | Required (`REQUIRED_FIELDS` in `signal_tap.py`) |
| `coherence`, `tp1_price`, `src` | Optional today; target: required `coherence` for eval ladder |
| Fail-open | Emit failure logs; never block ARIA hot path (`prop_outbox_emit_failed`) |

### RoutedIntent (Fascia → Army)
| Field | Law |
|-------|-----|
| `signal_id`, `army_id`, `sleeve`, `route` ∈ {owner, overflow, reject} | Fascia decides **who** may act |
| `risk_hint_usd` | Optional Government hint — Army still owns final risk |
| Fail-closed on route | Missing KingdomView / stale peer → `overflow` refuse (today: `overflow_verdict` fail-closed) |

### ArmyState (Army → Kingdom)
Geometry + contract only — **no HL prices in risk fields**.
| Field | Law |
|-------|-----|
| `ts`, `account_id`, `sleeve`, `max_positions`, `positions[]` | Today: `prop_positions.json` |
| `budget{day,remaining,committed,realized}`, `dd{equity,headroom,band}` | Today: heartbeat / budget_day / dd.json — target: fold into KingdomView |
| Fail-open for consumers | Stale ArmyState → overflow standby / sniper disarm (`prop_trend_bridge` SNAPSHOT_FRESH_S) |

### KingdomView (Fascia aggregate)
| Field | Law |
|-------|-----|
| `armies: {id → ArmyState}`, `loss_veto`, `slot_economy` | Single read for Governor + overflow + family harvest |
| Fail-open publish | Partial view > silent envy; mark `incomplete:true` |

### MarkPlaneTick (Reference → observers)
| Field | Law |
|-------|-----|
| `ts`, `asset→mid`, `stale` | `HLMarkPlane.snapshot` / `mark()` |
| **NON-LAW** | Must **never** enter equity, headroom, risk_usd, breach band (`hl_marks.py` DESIGN LAW) |
| Fail-open | None/stale → legacy uPnL/BE path only |

### OutcomeFeedback (Army → Kingdom → Government)
| Field | Law |
|-------|-----|
| `family`, `side`, `realized_pnl`, `closed_ts`, `risk_usd`, `coherence_at_entry` | Feeds loss-veto + future policy |
| Loss veto | `CohortStats` k=20, min_n=5; consumers **fail-open** on stale (`executor._loss_veto`) |

---

## C. Authority matrix

| Concern | Authoritative | Forbidden |
|---------|---------------|-----------|
| Signal / coherence / stop geometry at birth | **ARIA** | Harness inventing signals |
| Route (which army) | **Fascia / KingdomView** (today: per-process filters + overflow) | Double-fire without overflow law |
| Lifetime DD / equity / headroom / band | **Propr** via Army drawdown brain | HL mid → headroom |
| Risk_$ at place | **Army** (`risk_for_signal` + `risk_ceiling`, `1252854`) | HL, ARIA notional caps |
| Fill truth / positions | **Propr** | HL B-book as fill oracle |
| Marks for trail / markout / sniper band | **ARIA candles** (exec scale) or HL **ref** after GO | HL into `mark_price_stores` without scale gate |
| Loss veto artifact | **Probe army (demo)** publishes; others consume | Dead probe freezing fleet |
| Kant/Nietzsche/Chancellor | **ARIA only** | Forking gates into 3 harness processes |

---

## D. Cybernetic loops

### 1. Entry loop
`PropSignal` → route (owner sleeve else overflow) → Army Kant belt (`enabled`, DD band, `risk_ceiling`, slots, family cap, loss veto, margin, cost/risk) → bracket batch → `ArmyState` publish → KingdomView.

### 2. Exit / BE / reload
Reconcile @15s: sync size → harvest `risk_released` → BE at +0.5R uPnL (`should_arm_breakeven`) → reload only if proven + fresh same-dir signal (`reload_verdict`). HL MarkPlaneTick may later **trigger** trail tighten — still not risk math.

### 3. Cross-sleeve harmony
Owner full → overflow army may fire same class. Family cap = one rep/(family,side) **per army**. Target: KingdomView family harvest (replace-worse across owner before overflow spends a slot).

### 4. Loss-veto as kingdom memory
Demo closes → `loss_veto.json` → turbos refuse toxic cohorts. Second-order: Governor reads veto + ArmyStates → policy (size floor, sleeve pause) without rewriting ARIA tiers.

### 5. Prop→ARIA sniper (existing)
`ArmyState` snapshots → `prop_trend_bridge` (ARIA-owned marks/bars) → sniper intents. Prop holds ground; ARIA rides — fascia already points the right way.

---

## E. Migration (no big-bang)

| Phase | Splice | Kill / rollback |
|-------|--------|-----------------|
| **0** | LIVE: outbox JSONL, env sleeve, overflow peers, risk_ceiling, HL ref poll | — |
| **1** | `KingdomView` writer (merge 3× ArmyState + veto + heartbeats) under `~/kingdom/prop_kingdom_view.json`; overflow reads view **or** legacy peers | `PROP_KINGDOM_VIEW=0` → peer files |
| **2** | Fascia router: ARIA or thin router process emits `RoutedIntent` per army (owner first); armies drop local `asset_class` filter when intents addressed | Dual-run: intents + outbox; prefer intent |
| **3** | Cursor death fix: per-sleeve outbox shards **or** intent push; boot hot-cursor within TTL | Keep TTL quarantine |
| **4** | Slot economy: probes don’t consume last owner slot when coh>7 pending; optional probe-standdown | Knob default off |
| **5** | Family harvest replace-worse (eval-only) | Kill switch |
| **6** | HL triggers (trail/reload price) behind GO — still fail-open to uPnL | `HL_MARKS` already; don’t wire risk |

Leave demo’s 3 legacy crypto legs; topology self-heals for **new** signals.

---

## F. Excellence levers (top 7)

1. **Shared KingdomView** — end silent multi-process envy; one overflow/slot/family truth.
2. **RoutedIntent / sharded outbox** — kill cursor/TTL equity death (turbo2 `tap_expired` class).
3. **Slot economy vs probe pre-spend** — reserve ≥1 owner slot for coh>7; probes are options, not squatters.
4. **Family harvest** — replace weaker same-dir rep before overflow burns a second army slot.
5. **HL mark plane (triggers only)** — trail/BE confirm/markout; never equity (`hl_marks.py` law; ARIA `HL_MARKS_ENABLED` scale doctrine).
6. **OutcomeFeedback richness** — stamp coherence_at_entry + risk_usd on closes so veto/policy learn at eval scale.
7. **Variational / extra planes** — `execution/variational_ledger.py` stays manual observatory; wire into KingdomView as **unmanaged** ArmyState (never harness-managed) so Governor sees full kingdom exposure.

---

## G. NON-goals

- Don’t fork Kant/Nietzsche/Chancellor into three harness processes.
- Don’t B-book risk, equity, or DD off HL marks.
- Don’t big-bang rewrite the outbox on a green eval book.
- Don’t delete demo’s legacy legs without Governor order.
- Don’t feed HL mids into ARIA `mark_price_stores` without explicit scale GO (phantom-PnL class).
- Don’t touch tiers 1–6 signal logic from harness work.

---

## Executive wiring law (5)

1. ARIA governs policy; armies execute contracts; fascia routes — never the reverse.
2. One kingdom memory; many actuators — stop discovering peers by accident.
3. HL observes; propr accounts — marks never mint headroom.
4. Overflow is a valve, not a second government.
5. Outcomes rewrite gating through state, not through forked brains.
