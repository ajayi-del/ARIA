# ARIA Local Overrides — Extracted from Claude Code Source Patterns

## Thinking Modes
- Say "ultrathink" in any prompt to trigger deep analysis mode
- Default thinking is enabled for all model calls
- Use "adaptive" for quick checks, "enabled" for complex architecture work

## Tool Concurrency Rules (from toolOrchestration.ts)
- Read-only tools (Read, Bash with ls/grep/cat/find) run in PARALLEL
- Mutating tools (Write, Edit, Bash with kill/rm/git push) run SERIAL
- When in doubt, batch reads together, then do writes separately

## Task System (from tasks.ts)
- Use TaskCreate for multi-step work (3+ steps)
- Mark in_progress BEFORE starting, completed when done
- Use blockedBy dependencies when order matters
- Prefer TaskList to check status before claiming new work

## Error Handling Patterns (from errors.ts + api/errors.ts)
- Always check isAbortError before retrying — don't retry user-canceled ops
- Parse token counts from prompt-too-long errors to decide compact vs truncate
- Use TelemetrySafeError for logs that must not contain code/paths

## Agency / Coordinator Mode (from coordinatorMode.ts)
- Research phase: spawn parallel agents for independent angles
- Synthesis phase: YOU read findings and write specific specs
- Implementation phase: one worker at a time per file set
- Verification phase: spawn fresh agent with clean eyes
- Never write "based on your findings" — synthesize yourself
- Continue vs Spawn Fresh: high context overlap -> continue, low overlap -> fresh

## Hook System (from hooks.ts)
- Hooks run shell commands at lifecycle events
- PostToolUse on Write|Edit can auto-run tests
- PreToolUse on Bash(deploy) can block if conditions not met
- SessionStart hooks can set env vars or check state

## Compact / Summarization (from compact/prompt.ts)
- When context window is full, preserve: user requests, file paths, code snippets, errors, pending tasks
- Strip <analysis> blocks after drafting — they are scratchpads
- Always include "Optional Next Step" with direct quotes from user

## Permission Modes (from yoloClassifier.ts)
- default: ask for everything
- acceptEdits: auto-accept file edits, ask for bash
- bypassPermissions: dangerous, only for trusted ops
- Use permission rules with glob matchers for repetitive approvals

## Dual-Thinking Framework (Quant + Philosopher)
When the user says **"fix"** (or requests any bug fix, patch, or correction), apply **both** `/quant` and `/philosopher` skills before making any code change.

### Execution Order
1. **Philosopher first** — root cause vs symptom, second-order effects, safety axioms
2. **Quant second** — probabilistic impact, risk metrics, number scales, EV
3. **Fix only if both pass** — smallest change, comment the WHY, run tests
4. **Verify** — re-run scenario, check logs, confirm no regressions

### Hard Stop Conditions
- Philosopher reveals safety axiom violation → STOP, report, do not fix
- Quant reveals unacceptable risk impact → STOP, propose alternative
- Tests fail after fix → STOP, undo, investigate

### Output Format
```
🔍 Philosopher: [assessment]
📊 Quant: [numerical impact]
🔧 Fix: [what changed]
✅ Verify: [test + log result]
```
