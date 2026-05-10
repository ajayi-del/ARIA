---
name: fix
description: Apply both quantitative and philosophical thinking frameworks before fixing anything
allowed-tools: [Bash, Read, Write, Edit, Agent]
when_to_use: When the user says "fix" or requests a bug fix, patch, or correction
arguments:
  - name: issue
    type: string
    description: "What needs fixing — error message, broken behavior, or anomaly"
---

# Fix Skill

The user explicitly requires **both** quant and philosopher lenses on every fix. Do not skip either.

## Step 1: Philosopher Analysis
Apply `/philosopher` first:
- Is this a symptom or root cause?
- What breaks if we fix this naively?
- Does this violate any hard rules (CLAUDE.md safety axioms)?
- Can we undo it instantly if wrong?

**If a safety axiom is violated — STOP. Do not proceed.**

## Step 2: Quant Analysis
Apply `/quant` second:
- What is the probabilistic impact of this fix?
- Does it change any risk-adjusted metrics (drawdown, position sizing, EV)?
- Are numbers stored in the correct scale (percent vs decimal)?
- Does the math still hold after the fix?

## Step 3: Fix
Only after both analyses pass:
- Make the smallest possible change
- Add a comment explaining WHY (not what)
- Run tests: `.venv/bin/python -m pytest tests/ -x -q`
- Verify no new warnings or type errors

## Step 4: Post-Fix Verification
- Re-run the failing scenario
- Check logs for cleanliness
- Confirm no regressions in related areas
- Report: root cause, fix applied, quant impact, philosophical rationale

## Output Format
```
🔍 Philosopher: [Root cause vs symptom assessment]
📊 Quant: [Numerical impact assessment]
🔧 Fix: [What changed and why]
✅ Verify: [Test result + log check]
```
