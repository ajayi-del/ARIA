---
name: deploy
description: Full ARIA deploy pipeline — suite, commit, push, server pull, restart, verify — with all the lessons from past deploy failures baked in
allowed-tools: [Bash, Read, Edit, Grep]
when_to_use: When shipping a fix to the live bot (user says "ship", "deploy", "push and restart")
arguments:
  - name: scope
    type: string
    description: "What is being deployed (fix summary for the commit message)"
---

# ARIA Deploy Pipeline

Every step exists because a past deploy failed without it. Do not skip steps.

## 1. Test suite (MANDATORY, with the hang workaround)
Local pytest hangs at Py_FinalizeEx on a non-daemon ThreadPoolExecutor — output
through a pipe NEVER appears. Always redirect directly to a file, run in
background, read the file:
```
.venv/bin/python -m pytest tests/ -q -p no:cacheprovider > /tmp/aria_suite_<date>_<slug>.txt 2>&1
```
Read the result file for the count line, then TaskStop the hung remainder.
Compare against the CURRENT baseline count in the latest CLAUDE.md deployment
entry (e.g. 2081P+28x+60xp) — new tests must show as +N.

## 2. Commit + push
`git add` specific files (never -A). Commit message: what the defect was, what
the fix is, kill switch name, suite count. Push origin main. If non-fast-forward:
the watchdog committed server-side — `git pull --rebase`, never force.

## 3. Server pull — DRIFT CHECK FIRST (never destroy work)
```
gcloud compute ssh aria-prod-v2 --zone=europe-west3-c --command="cd ~/ARIA && git status --short && git stash list | head -3"
```
- The watchdog may have UNCOMMITTED auto-tier fixes on the server. Read BOTH
  diffs fully before deciding. Stash with a descriptive name
  (`watchdog-auto-tier-<date>-<slug>`), pull, restore only runtime state
  (signals/aria_outbox.json local modification is expected — leave it).
- `signals/aria_outbox.json` shows as modified on every pull — runtime state, ignore.

## 4. Position check (rule #9 — exchange API is truth, never stale logs)
```
curl -s "https://mainnet-gw.sodex.dev/api/v1/perps/accounts/<WALLET>/positions"
```
(wallet from server ~/ARIA/.env, 0xdb87899...). Note open positions for the
re-adoption verify in step 6.

## 5. Restart (only if the trade path changed — standalone tools need none)
```
gcloud compute ssh aria-prod-v2 --zone=europe-west3-c --command="/tmp/aria_restart.sh"
```
Expect RESTART_OK. If the ssh drops mid-restart: reconnect and verify process
liveness — the script may have completed server-side.

## 6. Verify (the corrected protocol — 83a84f2 lesson)
The script's 35s pgrep check passes deep-boot crashers. MANDATORY:
1. Wait ≥90s after RESTART_OK.
2. `tmux capture-pane -t aria -p | grep -ci traceback` → must be 0
   (tracebacks go to the PANE, NOT aria.log — boot logs alone prove nothing).
3. Confirm a GATHER-LOOP event with a POST-BOOT timestamp:
   treasury_heartbeat or pnl_attribution — only loop events prove main()
   survived its def section. (Mind the actual UTC hour when grepping.)
4. Single process: pgrep count 2 = wrapper race — confirm with
   `ps -o pid,etime,cmd -p <pids>` that only one is python3 main.py.
5. Position re-adoption: startup_position_synced + startup_stop_placed for
   each pre-restart position from step 4.
6. The fix's own telemetry firing (its designed event name).

## 7. Bookkeeping (all three, every deploy)
- CLAUDE.md Recent Deployments entry (evidence, fix, kill switch, suite count,
  verified-live line, designed-events list) — commit + push.
- Server ~/aria_watchdog/memory/handover.md operator note.
- Server ~/aria_watchdog/proposals.jsonl status line (id, evidence=commit hash,
  status:"implemented", node:"local").

## Canon lens (baked into the steps — Dayo's working books)
- **Aronson** (before commit): every new degree of freedom ships BOUNDED —
  a kill switch whose False state reproduces the pre-module system
  bit-for-bit, and telemetry from birth. No unbounded knobs, ever.
- **Davey / López de Prado** (before commit): any new gate or relief is
  shadow-scored counterfactually FROM BIRTH — context is not signal until
  measured. If the commit message can't name the measurement, it isn't done.
- **Taleb** (verify step): verification proves the system did not become
  MORE PERMISSIVE by accident — fail-closed paths still fail closed, gates
  still armed. A fix that loosens silently is a future blowup with skin in
  the game.
- **Carver / Van Tharp / Vince** (verify step): if the change touches
  sizing, verify risk per trade is unchanged in R terms — notional follows
  stop distance, and venue sleeves size off their OWN capital.
- **Ashby / VSM-Beer** (bookkeeping): the designed-events list in the
  CLAUDE.md entry IS the variety budget — kill switch + telemetry + digest
  coverage ship WITH the module, or the watchdog will "fix" a designed
  event next cycle.
- **Goldratt** (rollback judgment): if verify fails, identify the actual
  constraint before acting — rollback (git revert + restart) is the answer
  to an execution-path regression, not to a slow telemetry warmup.
