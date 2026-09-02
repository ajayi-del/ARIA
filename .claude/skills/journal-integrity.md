---
name: journal-integrity
description: Journal-derived stats lie when the read path is poisoned — phantom records, orphan closes, dupes; repair = read-path filter, NEVER mutate (rule #14)
allowed-tools: [Bash, Read, Grep]
when_to_use: WR/pnl/edge stats look wrong, phantom or missing closes, after restarts with cross-day positions, before trusting any journal-derived number
arguments:
  - name: target
    type: string
    description: "Stat or symbol whose journal record is suspect"
---

# Journal Integrity Skill

The journal is permanent (hard rule #14 — never delete). Every derived
organ — Skeptic base rates, personality stats, symbol_edge, churn flags,
capacity journal_evidence — eats whatever the READ PATH serves. Three
poison classes, all shipped and pinned:

## 1. Phantom records (ghost closes)
SPCX 2026-08-21/22: bimodal census — 561 real closes ALL <$5 pnl vs 4
unique ghosts ALL >$100 (+$1,576 fake AFTERMATH pnl; 807 cross-file dupes
made 4 look like 64). Detection: the |pnl|>$100 threshold separates the
bimodal clusters exactly. Repair: `is_phantom_record` generalized to
ANY-date, filtered at `get_closed()` read-path — source files untouched.
Before trusting any large-pnl record: does the balance trail show the
money? Ghosts print in journals, never in wallets.

## 2. Orphan closes (restart amnesia)
`journal.load()` reads TODAY's file; `_open_entry_ids` is memory-only → a
cross-midnight position's close vanished after every restart (08-26: 5 log
closes, 1 journaled) — Skeptic, personality, churn, capacity all ate the
bias. Repair: find_open_entry_in_files (read-only scan of previous 4
day-files), close_already_recorded (120s + pnl-matched dedup),
record_cross_day_close (tier 1 migrated copy with close_migrated_from —
margin/pnl_r/personality survive; tier 2 synthetic orphan_close). When a
close is missing from stats, check the entry's DATE against boot times
first.

## 3. Duplicate counting
restore_from_journal read rolling-window day-files overlapping → every
trade counted 2-4× (414 dupes skipped at first fixed boot). Repair: dedup
by (entry_id, closed_at_ms) last-wins. Plus the live race: exchange_close
+ external_close booking one fill twice in one second → close_is_duplicate
(30s grace, no live tracked position). A stat that doubled overnight is a
dedup question before it's a performance question.

## 4. The audit pattern (run before believing ANY derived stat)
tools/beliefs_audit.py is the template: phantom census + large-pnl
suspects, pooled-vs-split diffs, stored-vs-recomputed agent winrates →
JSON report. Corrupted beliefs throttle and veto LIVE (ETH shorts at
0.50× from phantom-pooled WR; a CL-USD veto flipped OFF after the purge).
The beliefs layer is a trading organ — audit it like one.

## 5. Provenance beats deletion, always
Repairs carry provenance tags (close_migrated_from, orphan_close,
phantom_skipped counters in performance_restored) so the next audit can
distinguish filtered-from-filtered-out. If a repair would mutate a source
file, the repair is wrong — rule #14 has no exceptions.

## Canon lens (baked into the steps — Dayo's working books)
- **Hasbrouck** (step 1): ghosts fail the wallet test — journal prints
  vs balance trail, the bimodal census is arithmetic not narrative.
- **López de Prado / Aronson** (step 4): poisoned labels → poisoned
  signals — every downstream measurement inherits the read path's bias;
  audit the data plane before the model.
- **Taleb** (step 5): permanence is the fail-closed property — a filter
  can be revised, a deletion cannot be undone.
- **Simon** (step 3): the amnesia is structural (memory-only maps, today-
  only loads) — design every read path for a process that forgets.
