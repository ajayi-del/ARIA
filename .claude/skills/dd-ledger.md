---
name: dd-ledger
description: Phantom-drawdown repair — reconcile exchange history vs tracker DD, reset poisoned peaks in the right order (reset BEFORE redeposit), verify recovery clears
allowed-tools: [Bash, Read, Grep, Edit]
when_to_use: When recovery mode/DD% looks wrong vs actual trading results, after operator withdrawals/deposits, or when the two DD trackers disagree
arguments:
  - name: situation
    type: string
    description: "What looks wrong (e.g. 'recovery active but we barely lost', 'operator withdrew')"
---

# DD Ledger Skill

The recurring defect class (5 instances by 2026-09-02): external cash flows
read as trading drawdown. Every instance was an ops patch until the
dd-guard-sync fix; this skill is the playbook.

## 1. Recognize the symptom
- `recovery_mode_activated reason=drawdown` re-arming at EVERY boot (the
  trigger is persisted state, not in-session P&L).
- DD% that doesn't match the journal. Two trackers disagreeing (calibrator
  status vs drawdown_state.json) is a clue, not noise.
- Phantom effects are THREE deep, not one: calibrator recovery (floor 5.6 +
  0.5x cap), DrawdownGuard size tiers, SessionDrawdownTracker TP regime
  (caution 3% / defensive 6% / halt 10% — in-memory, clears at restart).

## 2. Reconcile with exchange history (Hasbrouck: raw values, not vibes)
- Sum the exchange position-history closes (gross), subtract ~fees:
  `gap = peak − current − realized − fees ≈ external flow`.
- Live truth: SoDEX via unauthenticated curl (wallet from server .env):
  `GET https://mainnet-gw.sodex.dev/api/v1/perps/accounts/{W}/state` (av),
  `/balances` (wb), `/positions`. Aster via a server-side script (scp, never
  heredoc): `Settings()` + `AsterClient.get_account_balance()`. The VM gets
  403 on urllib with no UA — curl from local works.
- The trackers are COMBINED equity: `_cached_balance[0]` = sum of venue
  balances with phantom-trough last-good substitution, possibly MAM-
  augmented. Peak must equal SoDEX av + Aster equity within cents.

## 3. The three trackers (who reads what — main.py ~9447)
- DrawdownManager: persisted logs/drawdown_state.json; anchors
  peak/low/session_start/week_start/day_start; 20/40/70% tiers.
- DrawdownGuard: in-memory; sync_peak RATCHET-ONLY by design (lost manager
  state must never disarm it) — explicit adjust_peak/reset_peak only
  (dd-guard-sync fix). Feeds the calibrator every balance update.
- Calibrator: 3% trigger, 1.5% exit; recovery = coherence floor 5.6 + 0.5x
  size cap. Exit is automatic once fed DD < 1.5%.

## 4. Sequence: reset BEFORE redeposit (Taleb — order is the whole game)
The deposit doctrine shifts anchors UP by any detected deposit so new
capital can't mask real losses. A redeposit onto a missed-withdrawal
ledger is classified as fresh capital → peak inflates → the phantom locks
in PERMANENTLY. Reset first, redeposit after; then the doctrine works from
a clean base.

## 5. The reset path
1. `cp logs/drawdown_state.json logs/drawdown_state.json.bak-YYYYMMDD-<slug>`
2. `touch logs/reset_drawdown.flag` — consumed within one 30s balance-
   monitor cycle (no restart needed post-dd-guard-sync fix).
3. Verify: `drawdown_manager_force_reset`, state file peak == current,
   `recovery_mode_deactivated` on the next feed, sizing_chain dd_mult 1.0,
   zero `recovery_mode_coherence_skip` after.
4. The flag over-corrects by the real trading DD (sub-threshold — say so,
   don't hide it). Exchange-history math is the attestation.

## 6. Do NOT "fix" blind (proposal class, needs measurement)
- Fail-closed withdrawal vetoes (open book / close-in-window) systematically
  MISS withdrawals — that's how phantoms form. Detection robustness is a
  measured proposal, not a same-day patch.
- Open-book wb branch detects withdrawals only, never deposits (designed).
- dd_tracker (session) phantom caution: clears at restart; no persisted
  repair needed.

## Canon lens (baked into the steps — Dayo's working books)
- **Hasbrouck** (step 2): the reconciliation is exchange-history arithmetic,
  not log vibes — closes summed, fees estimated, gap attributed.
- **Taleb** (step 4): the fail-closed path is explicit; the dangerous action
  (redeposit) is sequenced AFTER the repair, never before.
- **Goldratt** (step 3): the constraint is the peak ANCHOR, never the gates
  — don't touch floors/caps to fix a ledger error.
- **Chan/Thorp** (step 1): a phantom DD taxing size and clipping TPs is a
  negative-EV state; clearing it IS the profitable trade.
- **Aronson** (step 5): the fix ships bounded — kill switch
  (DD_GUARD_SYNC_FIX_ENABLED), telemetry (drawdown_guard_peak_adjusted /
  _reset), suite pins.
- **Simon** (step 5.4): say the over-correction out loud — the next reader
  of this ledger has amnesia and deserves the honest scar.
- **VSM-Beer** (step 6): the watchdog (S3*) observes and reports the reset;
  repair authority stays with the operator node (S5) — handover note every
  time so the watchdog doesn't "fix" force_reset or recovery_mode_deactivated.
