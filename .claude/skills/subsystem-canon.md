# Subsystem Canon — every ARIA subsystem wires to a governing book/concept

Origin: Governor directive 2026-09-08 ("find books that critically show how to wire each
subsystem surgically... no hallucinations stay grounded"). Standing reference for the
LOCAL-TOUCH PROTOCOL: every staged diff names its governing text; Cato's watch_register
line for that subsystem cites it. If a change has no governing concept, it is folklore —
do not ship it (Aronson's rule).

## The map

### Entries / signal quality (the #1 leak — D22: exits fine, entries lose)
- **Grinold & Kahn, *Active Portfolio Management*** — Fundamental Law: IR ≈ IC × √breadth.
  ARIA's breadth is fine (many symbols, many polls); IC is near zero on SoDEX. Entry work
  = raise IC per signal, not more signals.
- **López de Prado, *Advances in Financial Machine Learning*** — meta-labeling: a primary
  model sides, a secondary model decides size/veto. Triple-barrier labeling is how entry
  candidates should be graded (already echo'd in exit_autopsy MFE join design).
- **Chan, *Algorithmic Trading*** — a strategy class with measured negative expectancy
  gets sized to ZERO until re-derived. Applies to: SoDEX churn sleeve, Mon/Wed long
  cohort (shadow-gated proposal filed).

### Evidence discipline (governs ALL of the above and my own analyses)
- **Aronson, *Evidence-Based Technical Analysis*** — no threshold without derivation,
  n, and CI; multiple-comparison control. This is the axiom that convicted Filter 5
  (quiet_market_pause bar 40 vs 45-day max 24, no derivation, commit 8617f92) and it
  applies to my own weekday finding (n=46, pattern-grade not policy-grade).
- **Davey, *Building Winning Algorithmic Trading Systems*** — shadow/incubation first:
  every gate change runs as a shadow cohort with a pre-registered estimand before it
  can touch live flow. (weekday-session-cohort-gate proposal follows this.)

### Exits / ratchet (#37 class)
- **Freeman-Shor, *The Art of Execution*** — disposition effect: cutting winners early,
  riding losers. Measured in ARIA's own ledger: TPs held 52min, stops cut 17min — the
  exit side is actually healthy; do NOT "fix" exits to compensate for bad entries.
- **Van Tharp, *Trade Your Way to Financial Freedom*** — R-multiples: every exit graded
  in units of initial risk, which is what #37's fired-telemetry must record to make the
  D11 ratchet arm gradeable.

### Sizing
- **Thorp** (Kelly criterion papers) / **Vince, *The Mathematics of Money Management***
  (optimal f) — fraction of equity as a function of edge/odds; ARIA's ladder + aster_margin_pct
  are Kelly-fraction governors, not knobs to turn on a hot streak.
- **Carver, *Systematic Trading*** — risk parity across sleeves: scale by vol, not by
  conviction. Venue margin split is a Carver problem.

### Fill path / execution costs (#27 binding edge)
- **Hasbrouck, *Empirical Market Microstructure*** / **O'Hara, *Market Microstructure
  Theory*** — spread, impact, adverse selection: the entry-path spec the Chief Quant
  owes from D22 lives here.

### Threshold reconstruction (Filter 5 / quiet_market_pause)
- **Kaufman, *Trading Systems and Methods*** — adaptive/percentile-based thresholds
  derived from rolling history (the repair already exists in valuechain_monitor.py:531-549;
  the wire is GATED on n≥30 signed closes, #36).

### Regime / calendar effects
- **Ilmanen, *Expected Returns*** — calendar/seasonal anomalies exist but are the most
  data-mined class; demand out-of-sample and economic mechanism.
- **Clenow, *Stocks on the Move*** / **Murphy, *Intermarket Analysis*** — regime filters
  as explicit state machines, cross-asset confirmation (the SoSoValue flow-map doctrine).

### Behavior / churn signature
- **Steenbarger, *The Psychology of Trading*** — re-entry churn (SOL ×11) as a measured
  behavior pattern, treated as a system bug not noise.
- **Lefèvre, *Reminiscences of a Stock Operator*** — the hold gap: "the big money is in
  the sitting." Multi-hour Aster holds vs sub-hour SoDEX churn is this, measured.

### One-system philosophy (Cato's audit lens)
- **Beer, *The Brain of the Firm*** (Viable System Model) — each subsystem: one function,
  clear recursion level, own variety attenuation.
- **Simon, *The Sciences of the Artificial*** — nearly-decomposable systems: subsystems
  interact through narrow interfaces (the "one splice point" rule).
- **Ashby, *An Introduction to Cybernetics*** — requisite variety: a controller (Cato)
  must observe at least as much state as it governs → the census mandate.
- Enforced mechanically via **docs/DEPARTMENT_TEMPLATE.md**: zero-I/O brain, one splice
  point, kill switch, own telemetry namespace, own tests.

### Ledger integrity (non-negotiable floor)
- Firm doctrine, not a trading book: accounting identity — every flow classified once,
  no book inflated, phantom classes quarantined (SPCX ghost cluster, intervenue transit).
  Withdrawal attestations repair the DD ledger before any recovery-arm sizing.

## How to apply
1. Before staging any diff: name the subsystem's row above; cite it in proposals.jsonl.
2. Cato's watch_register line for the touched subsystem carries the same citation.
3. If the fix contradicts its governing text (e.g., loosening a gate to stop a bleed —
   Aronson forbids curve-fitting to recent pain), the fix is wrong, not the text.
4. New subsystem? It enters DEPARTMENT_TEMPLATE.md review AND this canon before deploy.
