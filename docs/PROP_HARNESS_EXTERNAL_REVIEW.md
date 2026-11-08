# Prop harness — external review spec

Reviewer brief for today's prop-harness evaluation-doctrine surface.
**Spec only.** Nothing in the proposed Hyperliquid section is approved work.

---

## 0. Scope

**Repo (local):** `/Users/dayodapper/CascadeProjects/ARIA`

**Surface:** commits `3fd4555..HEAD` touching `prop_harness/` and `tests/prop_harness/`. Load-bearing doctrine is pinned in:

- `tests/prop_harness/test_eval_profile.py` (42 pins)
- docstring of `prop_harness/brains/eval_profile.py`

**Suite:** `.venv/bin/python -m pytest tests/prop_harness/ -q` (venv required — bare `pytest` breaks on the repo conftest).

**Live evidence (server):** `gcloud compute ssh aria-prod-v2 --project=aria-trading-prod-2026 --zone=europe-west3-c --tunnel-through-iap`, then per-account `~/prop_harness_state{,_turbo,_turbo2}/journal/events.jsonl` + `heartbeat.json`.

Recent prop commits of note:

```
2174211 prop: overflow topology — demo fires only when the owning turbo is at cap
e9cc7f3 prop: evaluation-phase doctrine — big-bet sizing, breakeven arm, reload graph, loss veto
5d7d736 prop: wave-3 sleeve map — fx, pre-IPO, wider equities (68 → 160)
178edec prop: PROP_FAMILIES family-partition knob for the two-turbo split
c82afdd prop: rotation policy gates — family cap + cost-vs-risk
```

---

## 1. Paste-ready audit prompt (evaluation doctrine)

> Audit today's prop-harness evaluation-doctrine build in `/Users/dayodapper/CascadeProjects/ARIA`. Surface: commits `3fd4555..HEAD` touching `prop_harness/` and `tests/prop_harness/`. Doctrine intent is pinned in `tests/prop_harness/test_eval_profile.py` (42 pins) and the docstring of `prop_harness/brains/eval_profile.py` — the eval account is an OPTION: probe $5 at coherence ≤7, 70% of remaining budget at >7, 100% at ≥8.5, $40/day floor ($50 trend days), breakeven arm at +0.5R, reload graph spends harvested risk only (max 1, tighten-only stops), margin cap 30% equity over a leverage ladder, loss-veto = family|side cohort veto (n≥5, k=20 shrunk expectancy <0, 7d window, fail-open >6h stale). Hunt for: verdict-math drift from the pins, budget commit/release leaks (every commit must have exactly one release path), registry-field regressions on old brackets, fail-open paths that should be fail-closed, and any place the standard (funded-phase) profile can accidentally inherit eval behavior. Verify claims against the live journals on the server, not just the code.

---

## 2. Topology the reviewer should assume

Three harnesses, one outbox (pull-only, cursor + dedup, TTL 300s):

| Account | tmux | Sleeve | Role |
|---|---|---|---|
| demo `RUr6EmkJiFhB` | `prop` | crypto | overflow-only + loss-veto publisher |
| turbo1 `dvcMBJ2ACrq4` | `prop_turbo` | crypto | primary |
| turbo2 `gtEjJUtE4cmg` | `prop_turbo2` | tradfi (`xyz:`) | primary |

Demo fires a fresh signal only when every peer covering its class is at `max_positions` (reads both turbos' `prop_positions.json`; fail-closed on stale/missing). Turbos consume demo `loss_veto.json`.

Inside each harness: signal tap → class/family filter → reload graph (if held) or `risk_for_signal` → place_bracket preflight (budget / margin / loss veto / family cap / max positions) → 5-leg venue bracket. Equity poll owns drawdown bands; reconciler owns absence×3 closes, breakeven arm, protection resize.

---

## 3. Hyperliquid reference plane (proposed — review before build)

### 3.1 Problem

The harness has **no price plane**. Propr's API exposes equity / positions / orders but **no REST ticker**. Prices arrive via WS or the ARIA outbox; the harness today works around the missing mark everywhere.

### 3.2 Proposal

Evaluate adding a **keyless Hyperliquid public-info poller** as a **REFERENCE-ONLY** price plane:

- Endpoints: `info` — `allMids` / `metaAndAssetCtxs` / `l2Book` / `candles`
- HL is propr's underlying venue; markets include HIP-3 `xyz:` names
- Zero new keys, zero cost
- Precedent: ARIA's existing HL whale-mirror leg (`clearinghouseState`, unsigned) already trusts this plane for observation

### 3.3 Design law (non-negotiable)

**HL is a reference plane, never execution truth.**

Propr B-books internal fills and smooths HIP-3 marks off-hours. Therefore:

- Equity, uPnL, headroom, band ladder, and breach math stay **propr-authoritative**
- HL marks may drive **triggers and telemetry only**
- HL marks must **NEVER** feed risk math

### 3.4 What free HL data buys vs today's workarounds

| Gap today | Workaround now | HL mark plane would unlock |
|---|---|---|
| No venue mark | Breakeven arm reads account-level `unrealizedPnl` (poll-lagged) | Mark-based breakeven arm |
| No venue truth for reloads | Reload "better price" compares signal `entry_ref` vs bracket ref (both ARIA-timestamped) | Better-price verdicts vs venue-side mark |
| Trailing stops unbuilt | — (blocked on missing mark plane) | Trailing stops |
| No path MFE | — | MFE / give-back telemetry per bracket |
| No stale-signal price guard | — | Stale-signal price guard at the tap |

### 3.5 Five unlocks the reviewer must score

1. **Trailing stops on prop** — stop replace = cancel registry `order_id` + new `stop_market` reduceOnly with `positionId`. Rungs: breakeven+fees @1R, lock 1R @2R, tighten-only above.
2. **Mark-based breakeven arm** — replace the uPnL poll trigger.
3. **Reload better-price verdicts** — against a venue-side mark instead of signal refs.
4. **MFE / give-back telemetry** per bracket.
5. **Stale-signal price guard** at the tap.

### 3.6 Hard constraints to check

- HL marks **never** enter equity / headroom / band-ladder / breach paths (B-book fills and off-hours HIP-3 EWMA smoothing diverge from HL raw).
- Poller = supervised loop with **staleness abstain**. Fail-open: no HL data ⇒ today's behavior **bit-for-bit**.
- Zero new keys / cost.
- Whale-mirror (`clearinghouseState`, unsigned) is the trust precedent — not a license to elevate HL into risk truth.

### 3.7 Reviewer deliverable for this section

Verdict **GO / NO-GO**, plus:

1. **Failure matrix** covering at least: HL down, HL↔propr divergence, off-hours HIP-3 smoothing, rate limits.
2. Which of the **five unlocks survive** contact with the measured divergence numbers (and which die or must stay shadow).

### 3.8 Recommended build order *if* GO (not approved)

If the reviewer returns GO, first build should be:

1. mark plane
2. trailing stops

That pair is pure offense with the least doctrine risk. Unlocks 2–5 wait on divergence evidence. **Not approved work** — sequencing note only.

---

## 4. Reviewer checklist (whole packet)

- [ ] Eval doctrine: pins match live verdict math; no standard-profile bleed
- [ ] Budget: every commit has exactly one release path; no leaks under absence×3 / flatten / reload
- [ ] Overflow topology: demo refuses while turbos have room; fail-closed on stale snapshots
- [ ] Loss veto: publisher/consumer contract; fail-open >6h stale as designed
- [ ] **HL reference plane (§3): GO / NO-GO + failure matrix + unlock survival**
- [ ] Claims checked against server journals, not code alone
