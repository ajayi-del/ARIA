---
name: watchdog-sync
description: Inter-node protocol — amend watchdog prompt.md (with backup), append handover.md operator notes, and write proposals.jsonl status lines correctly
allowed-tools: [Bash, Read]
when_to_use: After any deploy or watchdog-affecting change; when giving the watchdog a new mandate; when marking proposals implemented
arguments:
  - name: change
    type: string
    description: "What the watchdog needs to know or check"
---

# Watchdog Sync Skill

The watchdog is a separate LLM node (server cron, kimi-k3). It inherits ONLY
what is written to its files. Every local deploy closes the loop here.

## 1. proposals.jsonl (append-only, last-wins)
`~/aria_watchdog/proposals.jsonl` — NEVER edit existing lines. Status
transitions are NEW lines; last line wins. Schema:
```json
{"ts": "...", "id": "<slug>", "title": "...", "rationale": "...",
 "evidence": "<commit hash + live verify + suite count>",
 "risk": "<low/med/high + kill switch>", "status": "implemented", "node": "local"}
```
Effect-claiming proposals (when WE propose TO the watchdog) also carry:
estimand, identification, n, effect_size, comparisons — the causal bar.

## 2. handover.md (the watchdog's guaranteed read)
`~/aria_watchdog/memory/handover.md` — appended per deploy via a LOCAL
python file scp'd to the server (never heredoc-over-ssh, quoting trap).
Include: what shipped + hash, what the watchdog must VERIFY next cycle
(telemetry event names, expected counts), corrections to its memory (it
self-corrects when told with evidence — e.g. the SoDEX-only vs combined AUM
error), and designed events it must not "fix".

## 3. prompt.md amends (standing mandates)
- ALWAYS backup first: `cp prompt.md prompt.md.bak-YYYYMMDD-<slug>`.
- Splice with a python script (scp + run) using a unique anchor string and
  `assert src.count(anchor) == 1` — never sed multi-line.
- Respect the contract: the watchdog MAY diagnose/propose/apply bounded
  auto-tier fixes; it MUST-NOT touch Kant/Nietzsche/Chancellor, leverage,
  universe lists, or treasury/trend_day/campaign knobs. New mandates must
  say which side of that line they live on.
- Observer-class mandates always carry the MUST-NOT-trade line and the
  proposal-only path for live wiring.

## 4. Verify the watchdog absorbed it
Next cycle's report.md (or `tail ~/aria_watchdog/cycles.log`) should show
the new section executed. The 2026-09-01 proof: stock-census mandate
amended 16:40 → cycle-12 report carried STOCK CENSUS + independent deploy
verification the same evening.

## Canon lens (baked into the steps — Dayo's working books)
- **López de Prado / Aronson** (proposals.jsonl): the causal bar is
  non-negotiable — estimand, identification, n, effect_size, comparisons.
  Context is not signal until measured; a proposal without n is a story.
- **VSM-Beer / Ashby** (prompt.md amends): one VSM function per mandate —
  the watchdog is S3* audit, never S1 operations. New mandates name their
  system function and their variety budget (what it may touch, what it
  must not, how it's observed).
- **Taleb** (MUST-NOT lines): every observer-class mandate carries the
  fail-closed path explicitly — the watchdog's default on ambiguity must
  be no-action, never improvisation with live capital.
- **Grinold-Kahn** (evidence lines): breadth over hero signals — when the
  handover cites verification, cite multiple independent confirmations
  (exchange API + log telemetry + watchdog cross-check), not one channel.
- **Simon** (handover notes): the watchdog is a fresh process each cycle —
  write handovers as if to a smart colleague with amnesia: what shipped,
  what to verify, what not to touch, in that order.
