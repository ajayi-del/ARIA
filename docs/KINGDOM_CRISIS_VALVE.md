# KingdomView crisis valve — joint n_eff thesis budget

**Date:** 2026-11-08  
**Task:** ATS-26  
**Branch:** `cursor/kingdom-crisis-cascade-556b`  
**Base:** `main` (KingdomView Phase 1 already on main; PR #10 still open as duplicate stack — **not** stacked)  
**MUST-NOT:** no Kant / Nietzsche / Chancellor / tier 1–6 rewrite; not treasury basket TP; no HL in risk math.

**Store canonical:** Project `docs/kingdom-crisis-valve.md`  
**Cites:** graph-synthesis Law 1 · prop-tp-turbo2 audit §4 · [`HARNESS_WIRING_SCHEMA.md`](./HARNESS_WIRING_SCHEMA.md) KingdomView · sibling [`CASCADE_SIGNAL_LAYER.md`](./CASCADE_SIGNAL_LAYER.md)

---

## Verdict (one screen)

| Item | Law |
|------|-----|
| **What** | Joint **exposure** `n_eff` = count of distinct `(family\|side)` factors open across demo+turbo+turbo2 |
| **Acts** | `CLEAR` / `REDUCE` (haircut risk) / `BLOCK` (refuse new factor) on **entry path only** |
| **Not** | CohortStats learning merge (still REFUSED) · EVAL_LADDER mutation · treasury ROE% basket TP · algedonic writer · stop dual-write |
| **Anti-lock** | Kill off / stale / incomplete / missing view → **CLEAR** (fail-OPEN) |
| **Reserve** | `coh≥8.5` or already-held / `be_armed` factor → exempt from **BLOCK** (REDUCE still binds under stress) |

---

## 1. Why (war doctrine, not TP rewrite)

Per-army `EVAL_LADDER` owns birth TP. ARIA treasury owns cluster ROE harvest. Neither is a cross-army crash valve.

Live class: demo BCH+XRP shorts + turbo LTC short = **one** `large_alt|sell` factor (`n_eff=1`), not three independent bets. Family cap is **per-army**; overflow can still stack β across accounts. Crisis valve is the kingdom-level budget on that β.

```
Per-army EVAL_LADDER     → birth TP / BE / trail (unchanged)
KingdomView crisis valve → joint n_eff → REDUCE / BLOCK new risk
Kingdom stress harvest   → later actuator (tighten/flatten) — NOT this PR
```

---

## 2. Contract

### Factor key

```
factor_key = f"{family_of(asset)}|{side}"
```

Same thesis across SoDEX+prop remains one risk factor (graph Law 1); sniper clamp still bounds the SoDEX add-on separately.

### Envelope (`crisis` plane on KingdomView)

| Field | Law |
|-------|-----|
| `schema` | `1` |
| `n_eff` | `len(open_factors)` — observe always |
| `n_eff_budget` / `reduce_at` / `block_at` | defaults 4 / 3 / 4 |
| `mode` | `CLEAR` \| `REDUCE` \| `BLOCK` — actuation only when `usable` |
| `usable` | schema-ok ∧ ¬incomplete ∧ fresh ≤ `crisis_stale_s` |
| `stress_active` | optional latch (`PROP_CRISIS_STRESS_ACTIVE`); never HL→equity |
| `owns_tp` | **always false** |
| `hl_in_risk_math` | **always false** |

### Entry verdict (`crisis_verdict`)

| Action | When | Effect |
|--------|------|--------|
| CLEAR | off / unusable / below reduce_at | planned `risk_usd` |
| REDUCE | `n_eff≥reduce_at` or stress or prospective≥reduce_at | `risk_usd × reduce_mult` (default 0.5) |
| BLOCK | would **raise** n_eff past budget/block_at (or stress∧n_eff≥reduce_at) **and** not reserved | refuse place; journal `tap_crisis_block` |

Same-factor entries (`would_raise=false`) never BLOCK here — per-army `family_cap` still owns doubles.

---

## 3. Anti-lock + kill switches

| Switch | Default | Off behavior |
|--------|---------|--------------|
| `PROP_CRISIS_VALVE` | on | bit-for-bit absent (always CLEAR) |
| `PROP_CRISIS_STRESS` | on | stress latch ignored |
| `PROP_CRISIS_STRESS_ACTIVE` | **off** | no stress until Governor/cascade bus arms |
| `PROP_KINGDOM_VIEW=0` | — | no view → CLEAR (fail-open) |

Never: write `algedonic.json`, erase ArmyState, widen TP, dual-own stops, raise ARIA limits from prop headroom.

---

## 4. Code map (this ship)

| Piece | Path |
|-------|------|
| Brain | `prop_harness/brains/crisis_valve.py` |
| Fold into view | `kingdom_view.build_kingdom_view(..., crisis=)` |
| Tap splice | `prop_harness/main.py` `_execute_signal` |
| Knobs | `prop_harness/config.py` |
| Pins | `tests/prop_harness/test_crisis_valve.py` (16) + kingdom suite |

**v1 does not** auto-flatten, tighten stops, or harvest sleeves. Stress basket actuation stays schema in the prop-tp-turbo2 audit §4 until a stop-owner bit ships (ATS-22 adjacency).

---

## 5. Distinction from refused cross-account n_eff

| Plane | Merge across armies? | Why |
|-------|----------------------|-----|
| **Learning** `CohortStats` / loss_veto | **REFUSE** | Independence + probe/consumer separation (learning-loops audit) |
| **Crisis** exposure `n_eff` | **REQUIRED** | Correlated crash defense; one factor = one budget unit |

---

## 6. Accept

- Pins green: `pytest tests/prop_harness/test_crisis_valve.py --noconftest`
- Live: `crisis.n_eff` on `~/kingdom/prop_kingdom_view.json` ≤ heartbeat age
- `PROP_CRISIS_VALVE=0` → zero `tap_crisis_*` events
- Incomplete/stale view → no blocks (armies trade)
- EVAL_LADDER / treasury untouched in diff
