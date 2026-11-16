# Cascade signal layer — SoDEX pre-liq + HL reference → ARIA → turbo2

**Date:** 2026-11-08  
**Task:** ATS-27  
**Ship:** **schema only** (no live splice; no Arcus; no HL risk math)  
**Driver:** turbo2 tradfi silence = empty fascia (prop-tp-turbo2 audit); need equity/commodity PropSignals that are **cascade-aware references**, not new risk truth.

**Store canonical:** Project `docs/cascade-signal-layer.md`  
**Cites:** [`HARNESS_WIRING_SCHEMA.md`](./HARNESS_WIRING_SCHEMA.md) MarkPlaneTick · HL marks DESIGN LAW · `intelligence/sodex_liq_engine_watch.py` · `data/whale_positions.py` · cascade_tracker / orchestrator · sibling [`KINGDOM_CRISIS_VALVE.md`](./KINGDOM_CRISIS_VALVE.md)

---

## Doctrine bullets (final)

1. **HL observes; propr/SoDEX account** — marks, `liquidationPx`, cascade phase never mint equity, headroom, DD band, or `risk_usd`.
2. **SoDEX pre-liq public address + HL cascade are REFERENCE inputs** into ARIA’s signal layer — they annotate / unlock PropSignal emission; they do not size prop brackets.
3. **Path:** `CascadeSignal` → ARIA (equities/commodities only) → `prop_outbox` PropSignal → turbo2 sleeve — **exclude Arcus** (dust / dual-venue fights; Arcus stays scalp-local).
4. **Fail-open everywhere:** missing address, stale HL, dark liq-watch → no emit (never block ARIA hot path; never freeze turbo2).
5. **Kill switches before live:** `CASCADE_SIGNAL_LAYER=0`, `CASCADE_OUTBOX_EMIT=0`; census shadow JSONL before default-ON.

---

## 1. Problem

| Symptom | Cause |
|---------|-------|
| turbo2 `tap_signal=0` | Outbox 100% crypto `execution_decision`; 0 xyz / 0 `arcus_fill` |
| Arcus broadcast dark | Scalp dust / `below_min_notional` — **do not couple cascade layer to Arcus** |
| Existing cascade brains | SoDEX/Bybit/ValueChain/Aster crypto-centric; tradfi xyz underserved for prop fascia |

Need a **typed reference bus** that turns public pre-liq / HL cascade stress on equities & commodities into Government PropSignals turbo2 can legally consume.

---

## 2. Sources (reference only)

### A. SoDEX pre-liq public address

| Plane | Live precedent | Role in this layer |
|-------|----------------|--------------------|
| Address-scoped position / liq proximity | `data/whale_positions.py` (Aster RPC + HL `clearinghouseState`) | Watch **Governor-listed public addresses** for equity/commodity names approaching liquidation / forced reduce |
| SoDEX liq-engine slice regularity | `intelligence/sodex_liq_engine_watch.py` (advisory verdicts) | Confirm cascade-in-progress on SoDEX-routed symbols — **verdict only** |
| ValueChain liquidated address topics | `data/valuechain_monitor.py` | Optional crypto confirm; **not** the tradfi emit path |

**Law:** address plane is **observe → CascadeSignal**. Never place from the address wallet. Never treat foreign equity as propr fill truth.

### B. Hyperliquid cascade / marks

| Plane | Use | Forbidden |
|-------|-----|-----------|
| `allMids` (main + `xyz`) | Stress / z-score **trigger** for emit eligibility; harness trail/telemetry | equity / headroom / breach |
| `clearinghouseState.liquidationPx` | Distance-to-liq feature on watched HL users | risk_usd |
| HL liq / funding stress (future WS) | Phase tag on CascadeSignal | Kant/Nietzsche fork |

Same DESIGN LAW as `prop_harness/io/hl_marks.py` and external-review §3.

### C. Explicitly excluded

| Source | Why |
|--------|-----|
| **Arcus** fills / scalp intents | Dust-bound; dual-venue; turbo2 feed must not wait on Arcus ops |
| Aster `!forceOrder` alone as prop size | Crypto cascade path already owned by CascadeTracker — do not double-emit into tradfi outbox |
| Prop harness reading HL for DD | HARD NON-LAW |

---

## 3. Envelope — `CascadeSignal` (schema:1)

Typed fascia row (ARIA-internal bus or `logs/cascade_signals.jsonl` shadow). **Not** a PropSignal until Government promotes it.

| Field | Law |
|-------|-----|
| `schema` | `1` |
| `id` | Durable `{src}:{symbol}:{side}:{ts_ms}` |
| `ts_ms` | Event time |
| `symbol` | ARIA symbol (`NVDA-USD`, `CL-USD`, `USTECH100-USD`, …) |
| `asset_class` | `equity` \| `commodity` \| `index` \| `crypto` — **emit-to-prop only for equity/commodity/index** in v1 |
| `side` | Cascade pressure direction (`long_liq`→prefer fade short bias encoded as PropSignal `side`, or momentum — **policy chooses once**; default = **fade** for pre-liq, **stand down** if ambiguous) |
| `phase` | `pre_liq` \| `slice_confirm` \| `hl_stress` \| `exhaustion` |
| `src` | ∈ {`sodex_preliq_addr`, `sodex_liq_watch`, `hl_mark_stress`, `hl_addr_liq`} |
| `ref` | Opaque reference blob: `{address?, liq_distance_bps?, hl_mid?, zscore?, slice_n?}` — **telemetry** |
| `confidence` | 0..1 feature; **not** coherence (Government still scores) |
| `actionable` | bool — false = census only |
| **NON-LAW** | No `risk_usd`, no prop equity, no ARIA notional as Army truth |

### Promotion → `PropSignal` (Government only)

When `actionable` ∧ asset_class ∈ tradfi ∧ geometry available (campaign stop / interpreter stop / SYMBOL_MAP target):

| PropSignal field | Source |
|------------------|--------|
| `symbol` / mapped `asset` | SYMBOL_MAP (xyz:) |
| `side`, `entry_ref`, `stop_price` | ARIA geometry — **never invent from HL mid alone** |
| `coherence` | Government score (cascade may **bonus** within existing detector cap — no tier rewrite) |
| `src` | `cascade_signal` |
| `block_class` | `aria_exec` (or omit) |
| `thesis_id` | `{asset}:{side}:{utc_day}` |

Fail-open: no stop geometry → **do not emit**. Journal `cascade_signal_no_geometry`.

---

## 4. Authority matrix

| Concern | Authoritative | Forbidden |
|---------|---------------|-----------|
| Pre-liq / HL stress observation | Cascade signal layer | Sizing |
| Coherence / stop / emit | **ARIA Government** | Harness inventing cascade signals |
| Route to turbo2 | Fascia / sleeve filter (`PROP_ASSET_CLASS=tradfi`) | Demo overflow stealing cascade without owner full |
| Fill / DD / risk_$ | **Propr army** | HL mid → headroom |
| Sniper add-on | Existing integrator + ρ gate | Cascade layer arming sniper directly |

---

## 5. Kill switches + migration

| Step | Change | Kill |
|------|--------|------|
| C0 | This schema; shadow `logs/cascade_signals.jsonl` | — |
| C1 | Wire SoDEX liq-watch + address registry → CascadeSignal (actionable=false) | `CASCADE_SIGNAL_LAYER=0` |
| C2 | HL xyz mark-stress → CascadeSignal (actionable=false) | `CASCADE_HL_STRESS=0` |
| C3 | Promote actionable→PropSignal outbox for SYMBOL_MAP tradfi | `CASCADE_OUTBOX_EMIT=0` |
| C4 | Optional: arm `PROP_CRISIS_STRESS_ACTIVE` from kingdom-wide cascade phase | separate; crisis valve already latches |

**Code this PR:** none (schema P0 satisfied by docs). Implement C1+ only under explicit build order after Mon RTH fascia census.

---

## 6. Accept (when built)

- Shadow rows before any outbox emit
- turbo2 first `tap_signal` with `src=cascade_signal` ∧ xyz asset
- `PROP_CRISIS_VALVE` / HL marks / EVAL_LADDER unchanged by cascade emit
- Zero Arcus imports in the cascade→outbox path
- HL outage → silence (fail-open), not blocks

---

## 7. Tie to ATS-26

Cascade stress may **latch** `crisis_stress_active` for the kingdom valve (REDUCE/BLOCK new factors). That latch is boolean policy — still **not** HL-in-risk-math. Crisis valve docs: [`KINGDOM_CRISIS_VALVE.md`](./KINGDOM_CRISIS_VALVE.md).
