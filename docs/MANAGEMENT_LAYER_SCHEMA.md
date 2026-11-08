# Management Layer Schema — Institutional Wiring Audit

**Date:** 2026-11-08  
**Task:** ATS-12  
**MUST-NOT:** no Kant/Nietzsche/Chancellor/tier rewrites; no restart.  
**Evidence base:** live code on workspace + store audits  
(`exit-cybernetics-audit.md`, `harness-wiring-schema.md`, `prop-efficiency-alpha.md`,  
`internal/exit-ownership-wiring-audit.md`, `internal/mgmt-layer-inventory.md`,  
`internal/prop-p0-deadwire-audit.md`).

**Repo twin (optional):** `docs/MANAGEMENT_LAYER_SCHEMA.md`  
**Sibling fascia:** [`harness-wiring-schema.md`](./harness-wiring-schema.md) (Government/Armies).  
This doc is the **management layer** above that fascia: three graphs + deterministic controller + propose-only supervisor agent.

---

## Authority law (one screen)

| Layer | Owns | May not |
|-------|------|---------|
| **Deterministic controller** | Risk clamps, stop ownership, exposure caps, route refuse | Invent signals |
| **Management agent** | Research proposals over graphs A–C; scored replay | Place orders, rewrite gates, deploy/restart |
| **ARIA Government** | PropSignal, coherence, birth stop geometry | Place propr orders |
| **Armies (prop)** | Final risk_$, DD bands, fills | Fork Kant/Nietzsche/Chancellor |

Controller owns risk. Agent proposes only. Same law as Chancellor absolute / watchdog MUST-NOT.

---

## 1. MFE × ROE ratchet × TP basket — wiring verdict

### Verdict: **PARTIAL — triad correct; residual sleeves + handshake lag**

No **P0 double-writer** inside MFE ↔ ROE ↔ treasury. Do **not** surgical-PR the triad. Soft gaps documented below.

### Ownership matrix

| Writer | Owns | Abstains when | Cite |
|--------|------|---------------|------|
| **Treasury** (`intelligence/treasury.py`) | Cluster harvests: TP1/TP2/`trail_lock`/runaway/margin-recycle via `_close_with_retry` | Never writes `stop_price` | brain `:353-533`; loop `main.py:24340+`; managed set `:24516-24518` |
| **`roe_ratchet`** | ≥3% ROE stop ladder (3/6/9/15) + native replace | Router exempt; pyramid (unless exempt knob True); `_basket_managed_syms`; Hugo; fast_cycle uses campaign coordinator | brain `intelligence/roe_ratchet.py:86+`; skips `main.py:21280-21296` |
| **`mfe_guardian`** | Sub-3% ROE floor registry (accrual always; floor mutation gated) | `treasury_managed` OR `roe_ratchet_active` → `update_floor` None | `intelligence/mfe_guardian.py:128-129`, `:277-278` |
| Software stop / native TPSL | Backstop fire | No ownership skip on guardian | `_stop_guardian_loop` `main.py:18325+` |

### Handoffs (designed)

```
open → _mfe_on_open (R1 merge arms + bracket + sync)
     → on_mark ABOVE skip-stack (R3) — MFE/MAE always accrue
     → peak ROE ≥ be_rung (3%) → ratchet_target_stop
         → compose: roe_ratchet_active=True; MFE floors stop raising
         → floor_for may still tighten target (one actuation path)
     → cluster ≥2 → _basket_managed_syms → ratchet continue; treasury harvests
     → leave cluster / age expiry → treasury_ownership_released → ratchet/MFE resume
```

Compose seam (single stop actuation for MFE+ROE):

```21668:21694:main.py
                    # ── MFE floor composition …
                    if _mfe_tracker is not None:
                        ...
                                _mfe_tk.treasury_managed = bool(
                                    _sym in _basket_managed_syms)
                                _mfe_tk.roe_ratchet_active = bool(
                                    _target is not None or _is_fc_pos)
                            _mfe_floor = _mfe_tracker.floor_for(_mfe_id)
                            if _mfe_floor is not None:
                                if _target is None:
                                    _target = _mfe_floor
                                elif _pos.side == "long":
                                    _target = max(_target, _mfe_floor)
                                else:
                                    _target = min(_target, _mfe_floor)
```

R3 hoist (accrual not gated by ownership):

```21112:21120:main.py
                    # MFE guardian splice … hoisted … ABOVE the ownership skip-stack
                    # … update_floor itself abstains on
                    # treasury_managed / roe_ratchet_active tracks.
```

### Zero-MFE classes

| Class | Status | Cite |
|-------|--------|------|
| Provisional-merge fills untracked (73.4%) | **FIXED R1** — cascade/bracket merge call `_mfe_on_open` | `main.py:9403/10424/16428`; pins `tests/test_exit_coherence_r1_r3.py` |
| `trade_class` never existed → 100% unknown | **FIXED R2** — real `trade_type`; aftermath→scalp; live day_type | `main.py:3507-3527` |
| Treasury/pyramid peaks stuck at 0.0 | **FIXED R3** — `on_mark` above skips | `main.py:21112-21139` |
| Arcus scalps | **Residual** — ScalpRegistry; startup skips PM/`_mfe_on_open` | `main.py:5663-5675` |
| Hedge legs | **Residual** — never enter PositionManager | hedge registry spine |
| Restart orphans | **Residual** — unknown id → `on_mark` None | `mfe_guardian.py:206-208` |
| Boot-adopted `day_type=unknown` | Accrues; wrong rung bucket possible | `main.py:3510-3513` |

### Collisions (graded)

| Pair | Grade | Note |
|------|-------|------|
| MFE ↔ ROE | **WIRED** | One compose path; abstain on active |
| ROE ↔ Treasury | **WIRED** | Roe skips managed; treasury closes only |
| MFE ↔ Treasury | **PARTIAL** | Flag re-sync sits *below* treasury `continue` → in-memory floors may raise after join until handback; **do not become stops** while skipped |
| ATR trail ↔ Treasury | **As designed** | Trail still writes protective stops on managed (`main.py:19114`, `:20796`) |
| ATR trail ↔ ROE | **PARTIAL** | Concurrent tighten-only composers; no mutual abstention |
| Native TP cancel at treasury birth | **Gated OFF** | `treasury_native_tp_cancel_enabled` default False (exit-coherence fix b) |

**Prop sleeve:** do **not** port ARIA class×band MFE/ROE into prop — sleeve-local ladder+BE+trail (`exit-cybernetics-audit.md` §4). Boundary holds.

---

## 2. Harness weaknesses — logic + policy fixes

Grounded in `harness-wiring-schema.md`, `prop-efficiency-alpha.md`, `exit-cybernetics-audit.md`, `prop-p0-deadwire-audit.md`. No Kant fork.

| Weakness | Logic/policy | Concrete fix | Priority |
|----------|--------------|--------------|----------|
| No **KingdomView** | 3 processes envy peers via files; Governor blind | Phase 1 writer `~/kingdom/prop_kingdom_view.json` merge ArmyStates+veto+heartbeats; overflow reads view **or** legacy | P2 fascia |
| Probe **slot squat** | `$5` probes fill `max_positions=3` before coh>7 | Slot reserve: keep ≥1 owner slot for coh>7; optional probe-standdown knob default off (schema Phase 4) | P2 |
| **Outcome stamps** thin | CohortStats skips `family=None` / missing `realized_pnl_final` → loss-veto forever dark | Close contract: `family`, `side`, `realized_pnl_final`, `coherence_at_entry`, `risk_usd`; backfill `family_of(asset)` on close | P1 |
| **`be_armed` fee hole** | Trail rung-1 fee-concession stamped proven → suppresses exact-entry BE | `stop_locks_entry` only (`eval_profile.py:232-243`); companion PR shipped class | P1 sealed |
| Loss-veto richness | Shrinkage needs min_n=5; `$5` units starve | Outcome stamps + optionally weight by `risk_usd`; fail-open stays | P1 |
| Reload silent | Proven + harvest but 0 same-dir re-fire | Ops/observe; no brain fork — ensure tap delivers after BE/TP | P2 |
| Shared outbox TTL | turbo2 `tap_expired` equity death | Sharded outbox / RoutedIntent (schema Phase 2–3) | P2 |
| **10x diversifier army** | Later — schema §H only | Fourth army `majors` lev≥8 when funded; preconditions: KingdomView + slot economy + demo legs flat | Later |
| provision_leverage BTC/ETH/SOL only | Tradfi big-bet margin dead vs `_LEV_OVERRIDES` | Iterate overrides filtered by `asset_class` (`prop-p0-deadwire-audit.md`) | P0 harness (separate) |

---

## 3. Free formulas → existing ARIA brains

**Doctrine:** map into brains already present; sleeve policy optional — **not** an engine rebuild. Latency-news uses calendar/TOB/post-print already live.

| Formula | Existing brain | Status | How to use (no rewrite of tiers) |
|---------|----------------|--------|----------------------------------|
| **Hurst H (rolling R/S on log-price)** | `intelligence/hurst_regime.py` — H>0.55 trend / H<0.45 MR / vol_rank chaotic | Classify **ON**; gate **SHADOW** (`regime_gate_live_enabled=False`) | Sleeve policy: trend-day / breakout consumers *read* `RegimeState`; live gate only after shadow census. Loop `main.py:32242+` |
| Vol rank (Parkinson IV-proxy) | Same module + `data/klines_4h.py` | Live measurement | Chaotic kill already in `classify_regime` |
| Day-type ORB | `intelligence/day_type_classifier.py` | **LIVE** | MFE rung bucket + trend_direction_guard already consume |
| Breakout coherence 4-pillar | `intelligence/breakout_coherence.py` | **SHADOW** (override OFF) | Optional sleeve: elevates coherence when H trending + pillars warm — still additive, not tier rewrite |
| EMA trend | `_ema_trend_verdict` `main.py:7930+`; campaign EMA3/5 HB | **LIVE** | Sleeve policy for campaign/trend-day only |
| VWAP / session VWAP | `data/session_vwap.py`, `intelligence/vwap_reversion.py` | **BRAIN ONLY — zero main splice** | Optional sleeve: mean-revert when H MR; kill-switch splice; do not replace microstructure tiers |
| Streak → size | `win_streak_size_mult` `main.py:35390+` (ARIA); prop has no streak ruin yet | ARIA live | **Eval ruin math:** map consecutive losses → day-budget haircut / sleeve pause via OutcomeFeedback → KingdomView — **Army controller**, not Nietzsche fork |
| Eval size ladder | `prop_harness/brains/eval_profile.py` `risk_for_signal` / `risk_ceiling` | LIVE | Keep; streak-ruin plugs into remaining/headroom clamps |
| Latency / news | Calendar dwell `post_print_block_seconds`; `_tob_snapshot_loop` → `logs/tob_snapshots.jsonl` | LIVE measure + dwell | Management agent scores TOB markouts; no new news feed |

Hurst contract (do not swap estimator casually):

```161:177:intelligence/hurst_regime.py
def classify_regime(hurst: Optional[float], vol_rank: Optional[float]) -> str:
    ...
    if v >= _CHAOTIC_VOL:
        return "chaotic"
    if v < _CALM_VOL and h > _TREND_H:
        return "trending"
    if v < _CALM_VOL and h < _MR_H:
        return "mean_reverting"
    return "random"
```

---

## 4. Management layer — three graphs + controller + agent

### Architecture

```
        ┌─────────────────────────────────────────────┐
        │  MANAGEMENT AGENT (research lane)           │
        │  proposes only → proposals.jsonl            │
        └──────────────────┬──────────────────────────┘
                           │ scored proposals
        ┌──────────────────▼──────────────────────────┐
        │  DETERMINISTIC CONTROLLER                   │
        │  Chancellor · DD · family caps · risk_ceiling│
        │  MFE/ROE/treasury ownership · loss_veto     │
        └───────┬──────────────┬──────────────┬───────┘
                │              │              │
     Relationship         Exposure      Execution-dep
     graph                graph         graph
```

### A — Market relationship graph

| EXISTING | TO_ADD |
|----------|--------|
| Prop `_FAMILIES` / `family_of` (`prop_harness/families.py`) | Single RelationshipGraph API |
| `crypto_family_cap` LIVE; equity corr cap LIVE | Unify ARIA `ASSET_CATEGORIES` ↔ prop families adapter |
| Campaign `DEFAULT_FAMILY_HEDGE_MAP` LIVE | Splice unwired `market_families` / `metals_anchor` / `cross_asset_lag` |
| CorrelationEngine + kelly haircut LIVE; MAG7 pairs shadow | Dynamic ρ where evidence ≥n |

### B — Exposure graph

| EXISTING | TO_ADD |
|----------|--------|
| `compute_book_exposure` → `logs/exposure_snapshots.jsonl` (telemetry; velocity SHADOW) | Typed ExposureGraph authority (net/gross/family/venue/army) |
| Chancellor max_kingdom / max_symbol LIVE | KingdomView fold of 3× ArmyState + ARIA sleeve |
| Campaign / anticipator / cascade gross / venue equity clamps LIVE | Promote velocity gate only after census |

### C — Execution dependency graph

| EXISTING | TO_ADD |
|----------|--------|
| `_stop_confirmed` bool; Arcus `entry_pending`+`arcus_tpsl_armed`; latefill L1/L2; pyramid phases; treasury ownership; MFE↔ROE handshake | Unified FSM: `entry → stop_ack → tpsl_armed → owner(MFE\|ROE\|treasury\|pyramid\|arcus)` |
| Prop BE/trail/reload | Cross-venue ack timeout/retry edges |

### D — Deterministic controller (locate; do not rewrite)

- `risk/chancellor.py` — absolute capital veto  
- `execution/kant_gate.py` — structural refuse  
- `risk/drawdown_guard.py` — DD size mult  
- Prop `risk_ceiling` + `loss_veto` + Army DD brain  
- Exit ownership triad (§1)

### E — Supervisor vs management agent

| Node | Role today | Management-layer fit |
|------|------------|----------------------|
| Watchdog | Crash/typo, EV scan, proposals; MUST-NOT gates/leverage/universe | Ops supervisor — keep |
| CEO | Architecture / exit_coherence schema; no deploy | Schema steward — keep |
| **Management agent (TO_ADD)** | — | Research lane only: graphs A–C + stop-ack census → proposals; inherit MUST-NOTs; never unconstrained |

### Authority matrix

| Concern | Controller | Agent |
|---------|------------|-------|
| Stop owner / exposure refuse / DD band | **Decide** | Propose retune with replay |
| Relationship edge weights | Enforce caps | Propose map merges |
| Kant/Nietzsche/Chancellor/tiers | Unchanged | Forbidden |
| Restart / capital | Forbidden (both unless Governor) | Forbidden |

---

## 5. Biology / quant — antifragile wiring

Concrete mappings into existing fascia (no mysticism):

| Concept | Wiring |
|---------|--------|
| **Antifragile feedback** | Outcomes → state → gating: loss-veto (`CohortStats`), win_streak size decay, MFE roundtrip counters, prop reload only when proven. Strengthen: OutcomeFeedback envelope `schema:1` with risk-weighted closes. |
| **Second-order observation** | Governor/CEO/watchdog watch sleeves watching markets (exposure_snapshots, axiom_gate.jsonl, postonly_shadow, tob_snapshots, hurst shadow would-blocks). Management agent consumes these planes; never becomes a third order placer. |
| **Federal sleeves** | Nations = crypto/tradfi/all; Armies = demo/turbo/turbo2; overflow valve; future 10x diversifier. Shared KingdomView = federal memory; local risk_ceiling = state police. ARIA parasite_scalper loss_reversal stays sleeve-local — typed bus required before cross-system loss memory. |
| **Homeostasis** | DD bands + recovery caps + fee governor window + margin budgets — already homeostatic controllers. Agent may propose knob ranges; controller enforces. |

---

## 6. Build order (Dayo-locked)

| # | Build | Reuse first | Ship shape |
|---|-------|-------------|------------|
| **1** | Execution state machine + stop ack | `_stop_confirmed`, Arcus TPSL arm, latefill synth, pyramid `owns_stop`, treasury activate/release | Typed FSM + ack timeout; telemetry `stop_ack_missed`; kill switch → legacy bools |
| **2** | Shared thesis risk | `crypto_family_cap` + prop `family_of` + campaign hedge map + CorrelationEngine | One `(family,side,β)` object consulted by all entry paths; adapter not fork |
| **3** | Exposure graph | `compute_book_exposure` + Chancellor open_margins + Army snapshots | KingdomView Phase 1 + typed ExposureGraph; snapshots remain fail-open telemetry until GO |
| **4** | Relationship graph | Prefer `ASSET_CATEGORIES` ∪ prop `_FAMILIES`; splice metals/cross_asset/market_families | Single API: hedge-of / shares-factor-with |
| **5** | Management agent research lane | `proposals.jsonl` channel; watchdog MUST-NOT + CEO no-deploy | Propose-only over graphs 1–4; replay-gated; never trade path |

Do **not** reorder: stop ack before graphs (protection truth); thesis risk before exposure (know what shares); exposure before relationship dynamics; agent last (needs inputs).

---

## Exit / fascia boundary (prop ↔ ARIA)

| Plane | ARIA | Prop |
|-------|------|------|
| MFE / ROE / treasury rows | Owns | **Do not import** |
| Ladder + BE + HL trail | — | Owns (eval) |
| Loss memory | parasite local | demo→turbo veto file → KingdomView target |
| Signals | PropSignal emit | Tap + Army Kant belt |

---

## 7. Surplus routing — ARIA capital-constrained, prop funded

**Problem.** ARIA sleeve can refuse a *sound* thesis solely because SoDEX/Arcus/Aster cannot post margin (low funds, venue equity clamp, notional floor after crush, anticipator margin budget). Prop armies may still have eval/funded headroom. Today the outbox is **not** a clean surplus valve: it emits only on `approved=True` after axiom (`main.py:15147-15204`), **before** Chancellor (`:15686-15690`) and **before** bracket-site `venue_equity` / `notional_floor` (`:16216-16272`). Capital deaths that flip `approved=False` earlier never emit; post-emit capital deaths leave prop holding an unlabeled “would-execute” row while ARIA quietly returns.

**Non-goals.** Prop must **not** become a shadow of ARIA’s rejected book. Do **not** require a SoDEX/Arcus fill for prop to trade. Do **not** enlarge ARIA limits from prop headroom. Eval ≠ funded sizing doctrine.

### Law

| Class | Emit? | Harness `RoutedIntent.route` | Fail mode |
|-------|-------|------------------------------|-----------|
| **Thesis + geometry OK, ARIA capital cannot post** → `aria_capital_block` | YES (surplus envelope) | May `owner` / `overflow` if Army Kant belt + KingdomView thesis slot free | Fail-**open** to prop (alternate path) |
| **True risk / structure refuse** → `aria_risk_block` | Optional telemetry only; **never** actionable route | Always `reject` | Fail-**closed** |
| **ARIA places (or clamps and places)** → `aria_exec` (today’s unlabeled emit) | YES | Normal owner/overflow | Unchanged |
| ARIA + prop both want same `(family,side)` | — | Second actor needs ExposureGraph / thesis reservation | Fail-closed without shared graph |

ARIA remains Government for coherence + birth stop geometry. Armies still own final `risk_$` / DD / fills. Prop headroom **never** feeds back into ARIA `base_trade_usd`, Chancellor caps, or venue clamps.

### Capital-class standdowns (surplus-eligible) — existing cites

| Event / reason | Where | Notes |
|----------------|-------|-------|
| `signal_rejected_venue_equity` / `venue_equity_insufficient` | `main.py:9269-9278`, `:10293-10302`, `:16225-16232` | Combined-book size vs executing-venue collateral |
| `venue_equity_unknown` | `_venue_equity_clamp` `:33462-33481` | Fail-closed on ARIA; surplus may still carry thesis if geometry intact |
| `signal_rejected_notional_floor` / `notional_floor` | `:9309-9326`, `:10329-10346`, `:16257-16272`; in `_KANT_PRE_VENUE_REJECTIONS` `:1162-1171` | Post-crush dust refuse — thesis ok, size dead on ARIA min |
| `campaign_terminal_floor_unaffordable` | `:15677-15684` | Campaign path can’t afford terminal floor |
| `anticipator_place_standdown` `reason=margin_budget` \| `margin_preflight` \| `margin_unknown` | `:28762-28803`, `:29319-29360` | Fleet/book margin ceiling — capital, not Kant structure |
| Aster `margin_insufficient_resized` / notional clamp | Phase-2 sizing (docs) | Resize-down on ARIA is **not** surplus by itself; only hard refuse after floor → capital_block |

Sizing half-blocks that log `*_blocked_notional_floor` (`:11886-12161`) are **capital-adjacent** only when the *final* reject is the floor — if an upstream risk gate already refused, class = risk_block.

### Risk-class refuses (never surplus-route) — fail-closed

| Class | Examples (live) |
|-------|-----------------|
| Kant structure / axiom structural reject | `signal_rejected_axiom`, Kant frame not approved |
| Coherence / personality / base-rate | coherence floor, `signal_rejected_base_rate`, personality blacklist |
| Calendar / latency-news | `signal_rejected_calendar_block`, settle, post-print dwell |
| DD / recovery halt | recovery “capital preservation outranks offense” (`:8112`, `:8156`) — **risk policy**, not surplus |
| Chancellor **VETO** | `chancellor_veto` (`:6405`, plane rejects `:9222`) — absolute |
| Direction / fakeout / family hard reject | `post_stop_flip`, `direction_gate`, `crypto_family_cap` **reject** (not 0.5× haircut alone), `tp_invariant` |
| Cross-sleeve / portfolio allocator veto | in `_KANT_PRE_VENUE_REJECTIONS` but **risk/portfolio** — do **not** surplus |

**Chancellor nuance:** size *clamp* that still leaves `approved` and a placeable bracket → emit `aria_exec` as today. Exposure-full VETO / kingdom halt → `aria_risk_block` (routing to prop would double-count thesis without an ExposureGraph grant).

### Envelope (schema:1)

Extend `PropSignal` **or** sibling shadow JSONL (prefer field on same outbox for one tap):

| Field | Law |
|-------|-----|
| `block_class` | ∈ {`aria_exec`, `aria_capital_block`, `aria_risk_block`} — default omit = legacy `aria_exec` |
| `capital_reason` | Opaque string from table above when capital_block (`venue_equity_insufficient`, `notional_floor`, `margin_budget`, …) |
| `aria_venue` | Intended ARIA venue that could not post (sodex/aster/arcus) — observational |
| `coherence`, `entry_ref`, `stop_price` | Still required for actionable rows; naked geometry never emits |
| `thesis_id` | Stable `{symbol}:{side}:{utc_day}` or family-key — KingdomView reservation key |
| **NON-LAW** | No ARIA notional, no prop equity, no “ARIA would have sized $X” as Army risk truth |

`aria_risk_block` rows: either omit from outbox or emit with `actionable:false` so taps can census without routing.

### RoutedIntent acceptance

```
PropSignal
  → if block_class == aria_risk_block → route=reject
  → if block_class == aria_capital_block:
        require stop geometry + coherence bar (eval ladder unchanged)
        KingdomView.thesis_reserve(thesis_id) — fail-closed if SoDEX/ARIA already open same (family,side)
        Army risk_ceiling / loss_veto / slots — Army owns size (eval vs funded doctrine separate)
        route ∈ {owner, overflow} — never “force demo because ARIA poor”
  → if block_class == aria_exec (or missing):
        today path; if ARIA fill later confirmed, KingdomView marks thesis spent
        (optional: cancel resting prop twin on same thesis_id — kill-switched)
```

**No SoDEX fill required** for capital_block path — Government already certified thesis+stop; Army executes propr contract only.

### Double-count guard (ExposureGraph / thesis risk)

Until KingdomView + shared thesis risk (§6 builds 2–3) ship:

1. **Default:** capital_block actionable only when ARIA has **no** open/pending same `(symbol,side)` and family cap free on the *kingdom* read (ARIA open book ∪ ArmyStates).  
2. **Never** open prop leg solely because ARIA rejected for Chancellor exposure — that reject means the kingdom already holds the factor.  
3. When both sleeves green: prefer **one** owner of thesis risk (ARIA if it can post after clamp; else prop surplus). No dual fire without explicit hedge doctrine.

### Eval vs funded

| Profile | Surplus capital_block |
|---------|----------------------|
| **evaluation** | Allowed — option account may spend day budget on Government thesis ARIA can’t host; still `risk_ceiling` + probe/big-bet ladder |
| **funded** | Allowed only with stricter thesis reserve + family harvest; payout doctrine outranks “fill every surplus”; optional coh floor raise |

### Anti-shadow checklist (must all hold)

1. Risk rejects never become `aria_capital_block` by renaming.  
2. Prop does not invent stops/coherence — only consumes Government geometry.  
3. Prop equity never widens ARIA gates.  
4. Surplus is labeled; unlabeled outbox ≠ capital_block.  
5. Census: `capital_block_routed` / `capital_block_thesis_busy` / `risk_block_dropped` events before any default-ON.

### Splice order (schema only — no restart from this doc)

| Step | Change | Kill switch |
|------|--------|-------------|
| S0 | Document classes (this section); census existing post-emit Chancellor/venue returns vs outbox | — |
| S1 | Tag new outbox field `block_class=aria_exec` on current emit (bit-for-bit route) | `PROP_BLOCK_CLASS=0` omit field |
| S2 | On capital standdown sites (venue_equity / notional_floor / margin_budget / terminal_floor), emit surplus row **without** placing ARIA bracket | `PROP_CAPITAL_SURPLUS=0` |
| S3 | Tap: actionable iff `aria_exec` OR (`aria_capital_block` ∧ surplus enabled); risk_block drop | Army env |
| S4 | KingdomView `thesis_reserve` before place; release on close/reject | `PROP_THESIS_RESERVE=0` → legacy envy |

Fits build order after **shared thesis risk + exposure graph** (§6.2–3); S1–S2 may land earlier as labeled telemetry with route still ignoring capital_block.

---

## P0 wiring defect check (this audit)

| Candidate | Verdict |
|-----------|---------|
| MFE/ROE/treasury double-writer | **None** — no surgical exit PR |
| Trail `be_armed` fee hole | Already sealed class (`stop_locks_entry`) |
| Harness `provision_leverage` tradfi | True P0 elsewhere — out of management-schema scope; track in harness audit |

---

## 10-bullet executive

1. **MFE ↔ ROE ↔ treasury are correctly one-writer on the ARIA triad** — compose in `_roe_ratchet_loop`; treasury harvests; MFE abstains when flags set (`mfe_guardian.py:128-129`).  
2. **Residual zero-MFE = Arcus + hedge + restart orphans** — R1–R3 fixed the PositionManager class; do not call that “unwired triad.”  
3. **Soft defect only:** treasury flag re-sync below skip → memory floors can raise after cluster join without becoming stops — observe, don’t panic-PR.  
4. **Harness fascia still envy-by-file** — KingdomView + slot reserve + outcome stamps are the concrete P1/P2 fixes.  
5. **Hurst H already lives** as shadow regime plane — use as sleeve policy (trend vs MR), not a new engine; VWAP brains exist but are unspliced.  
6. **Streak/ruin math** plugs into eval `risk_ceiling` / day remaining via OutcomeFeedback — Army controller, not Nietzsche.  
7. **Surplus routing:** label `aria_capital_block` vs `aria_risk_block`; prop may take capital-only deaths without a SoDEX fill; never shadow risk rejects; never enlarge ARIA from prop headroom (§7).  
8. **Today’s outbox is pre-Chancellor / pre-venue-clamp** (`main.py:15173` before `:15686` / `:16225`) — unlabeled; capital surplus needs explicit emit at standdown sites.  
9. **Controller owns risk; agent proposes only** — watchdog/CEO stay ops/schema; new management lane is research → `proposals.jsonl`.  
10. **Build order locked:** stop-ack FSM → shared thesis risk → exposure graph → relationship graph → management agent (+ surplus S1–S4 after thesis/exposure). No Kant/Chancellor rewrite; no restart from this doc.

---

## Paths

| Artifact | Absolute path |
|----------|---------------|
| **This deliverable** | `/Users/dayodapper/Library/Application Support/Cursor/AgentStores/cursor_agent_stores/bc-a6e9e4c7-4f20-4521-97ca-1ab1a0bfab27/files/docs/management-layer-schema.md` |
| Harness fascia (sibling) | `…/files/docs/harness-wiring-schema.md` |
| Exit cybernetics | `…/files/docs/exit-cybernetics-audit.md` |
| Exit ownership evidence | `…/files/internal/exit-ownership-wiring-audit.md` |
| Mgmt inventory evidence | `…/files/internal/mgmt-layer-inventory.md` |
| Prop P0 harness | `…/files/internal/prop-p0-deadwire-audit.md` |
| Repo twin (if PR lands) | `/Users/dayodapper/CascadeProjects/ARIA/docs/MANAGEMENT_LAYER_SCHEMA.md` |
| Repo harness twin | `/Users/dayodapper/CascadeProjects/ARIA/docs/HARNESS_WIRING_SCHEMA.md` |
| Key brains | `…/ARIA/intelligence/{mfe_guardian,roe_ratchet,treasury,hurst_regime,day_type_classifier,breakout_coherence,crypto_family_cap,campaign_book,exposure_snapshot}.py` |
| Prop eval | `…/ARIA/prop_harness/brains/eval_profile.py` |
| Controller | `…/ARIA/risk/chancellor.py`, `…/ARIA/execution/kant_gate.py`, `…/ARIA/risk/drawdown_guard.py` |
