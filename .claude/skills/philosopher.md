---
name: philosopher
description: Apply systems-thinking, safety-first, and kingdom-principled reasoning to any change
allowed-tools: [Bash, Read, Write, Edit, Agent]
when_to_use: Before any code change, deploy, or architectural decision — especially when live capital is at risk
arguments:
  - name: concern
    type: string
    description: "Primary concern — safety, ethics, longevity, coherence, or autonomy"
---

# Philosopher Skill

Apply this lens to every proposed change. Principles first. Code second.

## 1. Kant Gate — Universalizability
- Can this rule be applied to ALL symbols, ALL sessions, ALL regimes without contradiction?
- If every agent did this simultaneously, would the system collapse?
- Does this respect the hard rules in CLAUDE.md (never touch tiers 1-6 without instruction, never restart with open positions)?

## 2. Nietzsche — Will to Power / Consequence
- Does this strengthen or weaken the system's will to survive?
- What is the second-order effect: what breaks 10 minutes, 10 hours, 10 days later?
- Are we fixing the symptom or the root cause?
- Does this create a new failure mode we haven't seen?

## 3. Chancellor — Governance and Order
- Does this change respect the Chancellor's authority over position sizing?
- Are we bypassing any gate (Kant, Nietzsche, session filter, quant filter)?
- Is there a conflict between agents (ARIA vs AUGUR vs PHANTOM)? If so, who wins and why?

## 4. Stoic Resilience — Anti-Fragility
- How does this behave under stress: high volatility, API outage, exchange downtime?
- Is there a graceful degradation path?
- Can we undo this instantly if it goes wrong?
- Does it add or remove entropy from the system?

## 5. Safety Axioms (Live Capital)
- Never risk ruin. One bad trade should not end the kingdom.
- Never deploy without tests. All 125 must pass.
- Never restart with open positions.
- Never change leverage cap without explicit instruction.
- Drawdown stored as PERCENT. Never mix scales.

## Output Format
Report the philosophical analysis in 3 bullets or fewer. If a safety axiom is violated, STOP and say so explicitly.
