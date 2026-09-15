---
name: build-ship-swarm
description: The Governor's build/ship doctrine — when he says "build" or "ship", run the swarm pipeline: parallel specialist agents, polymath cross-review (no agent grades its own work), integrate, suite, commit, deploy at flat-book window, 60s verify. Cato uses the same law with its agents.
when_to_use: Governor says "build", "ship", "build these into aria", "ultrathink and ship", or any multi-module strategy/tool build directive. Cato invokes the same pipeline for its commissioned builds (debate-won / work-order whitelist).
---

# BUILD/SHIP SWARM — the house pipeline (Governor 2026-09-15, codified)

"Trading gates, not edges" (September census): gate-tightening ≠ alpha. Every build
ships with its measurement plane or it does not ship.

## The pipeline (in order — never skip 3)

1. **SPEC** — the coordinating node (local Claude or Cato) writes the spec
   itself: file paths, function signatures, thresholds, dark-leg semantics,
   kill switches, Polemarch stamps. Never delegate understanding; agents get
   self-contained briefs (context, doctrine, exact deliverable, test pins).
2. **SWARM** — one specialist agent per module, run in parallel, background.
   Every brain: zero-I/O, None=dark, entries fail closed, kills fail open,
   shadow-from-birth. Every tool: observer-class (stdlib+lazy httpx, atomic
   tmp+replace, one-bad-line JSONL, exit-0). Suite runner is
   `.venv/bin/python -m pytest` — bare python3 dies at conftest (eth_account).
3. **CROSS-REVIEW (Strategos/Polemarch law)** — clean-eyes reviewer agents
   attack each other's diffs in polymath frame (bugs AND what's missing).
   **No agent grades its own work.** Reviewer verdicts: SHIP /
   SHIP-WITH-FIXES / REJECT. Fixes applied before integration.
4. **INTEGRATE** — one splice point per module (department template), kill
   switch whose False = pre-module bit-for-bit, own telemetry namespace,
   designed-events list. Inert brains commit separately from live splices.
5. **VERIFY** — full suite green; restart-bundle deploys wait for a FLAT
   book verified via EXCHANGE APIs (rule 9 — never trust logs); restart via
   /tmp/aria_restart.sh pattern; post-boot verify = wait ≥90s, grep tmux
   pane for traceback, confirm a gather-loop event (treasury_heartbeat /
   pnl_attribution) with a post-boot timestamp. Rollback if unexpected.
6. **LEDGER** — proposals.jsonl status transitions, Recent Deployments entry,
   memory updated. Journal is permanent; deployed code carries its estimand
   or it rides as shadow until n.

## Deploy classes
- **Restart-bundle** (main.py/config/execution touched): flat-book window only.
- **No-restart** (tools/cron/docs/prompts): any time; install is the node's job.
- **SHADOW_ONLY modules**: require explicit Governor override to arm live.

## Cato binding
Cato's agents follow this same pipeline; Cato's cross-review reviewers must
not be the builders. Live trade-path changes stay local-node/Governor lane —
Cato ships staged diffs at ceo_review (CEO-review gate memory).
