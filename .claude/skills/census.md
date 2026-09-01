---
name: census
description: Server log forensics — per-symbol pipeline funnel census from aria.log to answer "why didn't X trade" with counts, not vibes
allowed-tools: [Bash, Read, Grep]
when_to_use: When the user asks why a symbol/class didn't trade, where candidates die, or to check a pipeline (stocks, alts, a venue)
arguments:
  - name: target
    type: string
    description: "Symbol list or class to census (e.g. 'single-name stocks', 'USTECH100')"
---

# Log Census Skill

Answers "why didn't X trade" with exact gate counts. Lesson from 2026-09-01:
three symbol classes had three different stories (SPCX traded, single names
gated, USTECH100 plane-bugged) — never answer from one grep.

## Step 1: Write the census script LOCALLY, scp it, run it
Never heredoc python over ssh inside double quotes — shell interpolation
mangles it (parse error near '}'). Pattern:
```
cat > /tmp/census.py <<'PYEOF' ... PYEOF
gcloud compute scp /tmp/census.py aria-prod-v2:/tmp/census.py --zone=europe-west3-c
gcloud compute ssh aria-prod-v2 --zone=europe-west3-c --command="python3 /tmp/census.py"
```

## Step 2: The aria.log parse idiom
- One JSON object per line, but the timestamp is NOT at line start — cheap
  prefilter `'"2026-09-01T' in line`, then `json.loads(line[line.index('{'):])`.
- Time windows compare on `d['timestamp']` strings (ISO sorts lexically).
- Mind the CURRENT UTC hour in window greps (a 17:xx window at 16:5x finds
  nothing — verify with `date -u` on the server first).
- Symbol normalize: `d.get('symbol','').split('-')[0]`.

## Step 3: The funnel (per symbol, split pre-US / US session at 13:30 UTC)
- `signal_ready` → `sizing_chain` → `coherence_tier_reject` (Kant, carries
  reason coherence_below_<floor>_<value>) → `execution_decision` → fills
  (`fastpath_entry_journaled` / `bracket_placed`).
- Rejection census: events starting `signal_rejected_` counted per symbol.
- Cross-check: a symbol with many signal_ready, few signal_rejected_*, and
  zero decisions is dying at Kant (`coherence_tier_reject` — NOT prefixed
  signal_rejected) or recovery (`recovery_mode_coherence_skip`).

## Step 4: Classify the finding (the discipline that matters)
- GATES WORKING: coherence rejects in a sub-floor band, turnover_reject
  clusters, counter_trend — doctrine, not plumbing. These gates are
  shadow-scored: say "measure in gate_economics before proposing", never
  propose a floor change from one session.
- PLUMBING DEFECT: the cross-scale plane signature — a metric and the entry
  price off by the venue rebase factor (SPCX ~5.4x, USTECH100 ~41x).
  Examples shipped: sentinel reference plane (6d1a7c3), ATR plane (ee47951).
  For Yahoo-owned tradfi symbols (tradfi_feed owns candles), ANY execution-
  plane metric derived from candle buffers is suspect. Get the raw values
  from the log line (atr AND entry) and compute the ratio before claiming.
- FEED DARK: signal_stale_data / insufficient_candles clusters — check feed
  ownership (Yahoo off-hours/weekends is expected dark, not a bug).

## Step 5: Report per-class, not per-feeling
State for each symbol class: signals flowed? where they die (gate + count)?
designed or defect? If defect: exact log lines with values, root cause,
file:line. The user ships on evidence.

## Canon lens (baked into the steps — Dayo's working books)
- **Hasbrouck** (step 3-4): evidence = raw values from the log lines
  themselves (the atr AND the entry, not the event name). Microstructure
  claims without printed numbers are gossip.
- **Goldratt** (step 3): the funnel exists to name THE constraint — one
  binding gate with counts, not a list of everything that fired. Control
  the chokepoint; don't fight the tape.
- **Chan / Thorp** (step 4, gates-working branch): a negative-expectancy
  class getting size ZERO is the doctrine working — "no trades because the
  gate refuses losers" is a VALID finding, not a problem to fix. Say so.
- **von Neumann / Sun Tzu** (step 4, adversarial read): before calling
  anything a defect, ask whether the "block" is actually defense — is the
  gate refusing a battle the bot should refuse? The atr_sanity gate was
  CORRECTLY preventing broken brackets even while it structurally blocked
  USTECH100; the fix repaired the plane, not the gate.
- **Steenbarger** (step 4, churn read): repeated fire/no-fill cycles on one
  symbol = churn signature — check participation (vol_ratio) before blaming
  the entry path.
- **Kuhn / Mandelbrot** (step 4, anomaly read): the anomaly IS the finding —
  a cohort dying at one gate in a narrow value band (coherence 2.2–2.9) is
  a distribution statement; report the band, not just the count.
- **Freeman-Shor** (when exits are in scope): if the census touches closes,
  read hold asymmetry — winners cut early vs losers riding is the
  disposition-effect signature, and payoff beats win-rate.
- **López de Prado / Aronson** (step 5): every "the gate is wrong" claim
  carries n and a measurement path (gate_economics cohort, shadow-scored
  from birth) — one session of refusals is an anecdote, not an estimand.
