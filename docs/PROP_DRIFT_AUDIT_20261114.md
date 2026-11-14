# Prop Harness Drift Audit — 2026-11-14 (local node, SSH-verified)

Auditor: local Claude node (Mac, SSH key to Vultr). Census: 2026-11-14 14:20–14:40 Europe/Berlin (13:20–13:40 UTC).
Baseline doc: docs/harness-agent-handoff.md (Project store, authored 2026-11-14 ~02:00 Berlin).
Method: live process/env census (3 PIDs), journal histograms (all-time + post-bounce window), file-hash diff host vs origin/main 78747dc. Rule 9 honored: positions read from harness state mirrors of Propr API; no restarts performed; no secrets read.

## 0. Headline

The handoff's own P0 (act-backoff) and its "pending" PRs #43/#44 are ALREADY MERGED and DEPLOYED. A bounce executed 2026-11-14 10:07Z (11:07 Berlin) brought all three sleeves to origin/main 78747dc, verified by md5 on 6/6 core files (main.py, config.py, executor.py, eval_profile.py, signal_tap.py, act_backoff.py). The 429 act-storm class is dead post-bounce. The cloud agent's parallel audit (HEAD 9ee7e4c, "main d4afcba") audited a stale checkout — its D1/D2/D3/D7 "UNMERGED/CRITICAL GAP" verdicts and FIX-1/2/3 build proposals are superseded; do not action them.

## 1. Drift table — handoff vs as-built 14:30 Berlin

| # | Handoff claim | As-built (evidence) | Class | Severity |
|---|---|---|---|---|
| R1 | PR #43 deny "committed on feature branches; deny not live until bounce" | MERGED 4d358b4 on origin/main; live default-on (crypto_deny_enabled=True); 101 crypto_deny tap_filtered events since 10:07Z (journal turbo) | RESOLVED | — |
| R2 | PR #44 "pending": turbo2 0.5% risk, crypto coh 7.5 | MERGED 2f734e5; live env PID 157769 PROP_EVAL_TRADFI_ACCOUNT_RISK_FRAC=0.005, PID 157768 PROP_CRYPTO_MIN_COH_FIRE=7.5; turbo2 max_positions=4 | RESOLVED | — |
| R3 | P0 proposal: PROP_ACT_BACKOFF | MERGED #45 (6906e19/86aca79/42a8693); config defaults ON (30s→300s, latch 3); brains/act_backoff.py on host; protection_resize_failed 68,971 all-time → 0 since 10:07Z | RESOLVED | — |
| R4 | PR #41 coh size bump "effectively silent" | MERGED 78747dc (tip of main); on host | RESOLVED | — |
| R5 | "Open books blocked bounce at last census" (turbo BTC/TRX/kBONK; turbo2 6) | Bounce executed 10:07Z. Turbo FLAT: 3 reconcile_closed_external ~10:07–10:16Z (pnl stamp 0 = known fee gap). turbo2 still 6 opens: GOLD/CBRS/XYZ100/MU/DRAM/LITE (CL/SNDK rotated out since census) | SUPERSEDED | note: open-book bounce happened; books re-adopted cleanly |
| R6 | "loop_error gcloud missing ~600+ — leftover GCP outbox path" | All 3 PIDs PROP_OUTBOX_TRANSPORT=local, PATH=/root/ARIA/signals/prop_outbox.jsonl; 0 loop_error any sleeve since 10:07Z. Residual was pre-migration history | RESOLVED | — |
| R7 | "Repo on host often without .git" | /root/ARIA/.git EXISTS as 81-byte broken worktree pointer → gitdir:/Users/dayodapper/CascadeProjects/ARIA/.git/worktrees/ARIA-arcus-hl-7852 (host was rsynced from a Mac worktree). Hash-verify mandatory; done | CONFIRMED+ | P3 hygiene |
| R8 | Basket brains (basket_tp.py/basket_graph.py) "confirm present on Vultr" | ABSENT from main and host (peak-push 79ef2fd, capital-scale 77a1ff0, HOLD-law e08c840 live only on branches). basket_tp_close_failed 2,139 all-time = pre-migration code. Basket conquest stack currently DARK — consistent with #38/#39 HOLD doctrine, but basket_tp.json state is now unread by any live brain | EXPECTED-HOLD | P2 when HOLD lifts |
| R9 | Stepped profit lock (PR #42) "flag on trail_failed — act fails" | PROP_STEPPED_PROFIT_LOCK referenced in host config.py + main.py; zero stepped events post-bounce (turbo flat; turbo2 n/a correct) | MERGED, unexercised | observe |
| R10 | Fee/exit_reason gap on reconcile | PERSISTS: post-bounce reconcile_closed_external stamps pnl 0, no fee, exit_reason null | KNOWN GAP | P3 |
| R11 | Day budget latch healthy | CONFIRMED: day_latch.json={} all sleeves; turbo committed $0/$80; turbo2 committed $39.74/$100 (realized +$2.85 today) | CORRECT | — |
| R12 | ARIA balance_floor_halt intentional; turbo2 comove silence designed | CONFIRMED both; not defects. ARIA main.py PID 24245 up since Nov 9 (Government untouched by bounce — correct) | CORRECT | — |
| R13 | — (not in handoff) | NEW: runner_bank_harvest zombie loop — harvest kept firing every ~15s for 9 min (10:07–10:16Z) against the externally-closed BTC short until reconcile absence×3 removed the bracket. 34 futile act calls, each 429-eligible | NEW DEFECT | P2 |

## 2. Cybernetic rescore (post-bounce live, per sleeve)

| Phase | turbo (crypto) | turbo2 (stocks) | demo |
|---|---|---|---|
| SENSE | A — HL marks age 1s, tap local, offsets advancing | A | A |
| DECIDE | A — deny live (101), coh floor 7.5 env, slot reserve 1, comove gate on | A — 0.5% equity frac live | A (overflow standby) |
| ACT | B+ — backoff deployed, 0 act-fails post-bounce; residual zombie-harvest class (R13) | A− — 0 act-fails with 6 opens; qty≤0 class absent post-bounce | A |
| JOURNAL | A− — dense; fee/exit_reason null on external closes (R10) | A− same | A− |
| LEARN | B — budget roll/latch honest; no storm telemetry aggregator (429 class now moot) | B | B |

Act vs designed-silence call-outs: post-bounce act failures ≈ zero everywhere EXCEPT the 34-event zombie-harvest tail (R13). runner_time_decay_shadow (39k turbo all-time) = shadow by design. mfe_sharpen_failed 15.6k all-time = pre-backoff 429 class, now silent. turbo2 comove=0 / stepped=0 = designed. Nothing in the current silence set is a defect.

## 3. Ranked proposals (surgical; ceo_review lane, no deploys from this audit)

| Rank | Fix | Class | Files | Evidence | Confidence |
|---|---|---|---|---|---|
| P1 | Harvest/act paths skip when bracket absent from live positions map (or honor act-backoff latch key per asset) — kills R13 zombie retry window | CONTAINED (~5 lines + pin) | executor.py (harvest_basket_tp / runner bank path) | turbo journal 10:07–10:16Z, 34 events | HIGH |
| P2 | Stamp fee + exit_reason on reconcile_closed_external via close_pnl_from_trades extension | MODULE (journal/loss_stats) | main.py reconcile, io/journal.py | R10; census-quality blocker | HIGH |
| P3 | Deploy hygiene: drop a DEPLOYED_SHA file on rsync (or repair/remove broken .git pointer) so hash-verify is one command | CONTAINED (ops) | deploy script | R7 | HIGH |
| P4 | When Dayo lifts #38/#39 HOLD: confirm basket_tp.json epoch compatibility before enabling brains against stale state | MODULE gated | basket branches | R8 | MEDIUM |
| HOLD | System-level exit rewrites, stepped-lock rung tuning, comove flip actuation, cost-move actuation | SYSTEM | — | per doctrine, Dayo-locked | — |

Superseded (do NOT action from the cloud audit): "merge #43/#44/#45", "build 429 backoff from scratch", "deploy requires restart-now" — all landed already. Its FIX-5 qty≤0 guard is worth one grep before adopting; post-bounce journals show zero qty≤0 resize events, so the class may already be dead.

## 4. Deploy-verify checklist (per handoff restart rules)

- [x] ≥60–90s heartbeats: all 3 sleeves fresh (≤8s age at census)
- [x] Single PID per sleeve: 157767 demo / 157768 turbo / 157769 turbo2, started 10:07Z
- [x] No traceback storm: 0 loop_error post-bounce
- [x] turbo flat after external closes; turbo2 6 opens re-adopted (entry_rebased_from_venue path exists; positions.json mirrors venue)
- [x] No secrets in this report

Report path (local): /Users/dayodapper/CascadeProjects/ARIA/docs/PROP_DRIFT_AUDIT_20261114.md
