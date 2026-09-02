---
name: sizing-chain
description: Sizing forensics — trace a fill's notional through the multiplier chain, name the ONE binding constraint, venue floor and leverage-invariant risk doctrine
allowed-tools: [Bash, Read, Grep]
when_to_use: A fill size looks wrong ("why so small/large"), margin utilization is off, before changing any sizing knob
arguments:
  - name: target
    type: string
    description: "Symbol/fill whose size needs explanation"
---

# Sizing Chain Skill

HYPE filled at $9 margin on a $421 signal (9aaf890). ZEC filled at 10×
intended size (79ff55d). Both were invisible until someone read the chain.
The sizing_chain event names every multiplier — use it, never guess.

## 1. Read the event, find the MINIMUM (Goldratt)
`sizing_chain` carries the notional at each stage plus the multiplier fields
(conviction stack, recovery, session, dd_mult_effective, risk_parity_ratio,
whale_mult, etf tilt, tac_rung, floor_used). The binding constraint is the
stage where notional DROPS to its minimum — name that one stage with its
value, not the whole list. A size complaint without the binding stage
named is a feeling, not a finding.

## 2. The chain order (who multiplies whom)
base → risk-parity (ref_stop/actual_stop, clamped [0.25, 3.0]) → conviction
ladder → recovery 0.5× (suppressed, never bypassed) → session →
dd_mult_effective (max of guard/manager — both measure the SAME event) →
balance/margin cap → venue floor resize → post-multiplier hard cap.
Every governor applies multiplicatively AROUND risk-parity, never instead.

## 3. Venue floor doctrine
`_venue_min_notional` = max(venue exchange min, sleeve × 2%): Aster $3
(3 bracket legs × $1 exchange min), SoDEX $80 (grows past $4k book). A
candidate below the floor dies UNSCORED at nietzsche_min_notional_fail —
that's the HYPE class: gate approved, basket cap shrank it, floor killed
it, nothing shadow-scored. Floor deaths are invisible without the
`min_notional` shadow gate — always check it in the census.

## 4. Leverage-invariant risk (Thorp/Vince)
notional = sleeve × margin_pct × leverage is a CEILING; risk per trade =
notional × stop distance and is INVARIANT under leverage. Margin pct is a
budget ceiling (0.80 aster), not a sizing signal. Sleeve, never combined:
fraction of the venue's OWN capital (Vince) — combined-equity fallbacks
are SoDEX-only, other venues fail closed to no-balance.

## 5. The .env trap (issue #17, twice bitten)
pydantic env>code binding: a stale BASE_TRADE_USD/MIN_TRADE_USD line in
server .env silently killed a 3× step-up for 5 days (2d4fd4e). Before
blaming code for a size, grep the server .env for sizing overrides. .env
is for secrets, never config.

## Canon lens (baked into the steps — Dayo's working books)
- **Goldratt** (step 1): one binding constraint per fill — control the
  chokepoint, don't tune the whole chain.
- **Carver / Van Tharp / Vince** (step 4): risk in R terms, notional
  follows stop distance, sleeves size off their OWN capital.
- **Thorp** (step 4): leverage changes margin, never risk — a leverage
  question is a stop-distance question.
- **Hasbrouck** (step 1): the sizing_chain event IS the raw tape — read
  the numbers, don't theorize about multipliers.
- **Aronson** (step 5): every sizing knob is a bounded degree of freedom
  with a kill switch — and the deadliest knob was the one hidden in .env.
