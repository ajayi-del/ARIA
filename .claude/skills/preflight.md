---
name: preflight
description: Run the ARIA pre-flight checklist before any deploy or restart
allowed-tools: [Bash, Read]
when_to_use: Before deploying to production, restarting the bot, or pushing code that affects trading logic
arguments:
  - name: mode
    type: string
    description: "check type: tests, positions, diff, or full"
---

# ARIA Pre-Flight Skill

Run these checks in order. Stop and report on first failure.

## 1. Test Gate (if mode includes tests or full)
```bash
.venv/bin/python -m pytest tests/ -q
```
All 125 tests must pass. If any fail, fix before proceeding.

## 2. Open Position Check (if mode includes positions or full)
```bash
grep "open_positions" logs/aria.log | tail -3
```
If any open positions exist, STOP. Do not deploy or restart.

## 3. Git Diff Review (if mode includes diff or full)
```bash
git diff --stat HEAD
git diff HEAD
```
Show the diff to the user. No deploy without explicit approval of changes.

## 4. Log Health (if mode includes full)
```bash
tail -20 logs/aria.log
```
Check for recent errors, exceptions, or anomalous events.

## Output Format
Report each check as PASS or FAIL with one-line evidence.
If all pass: "Pre-flight clear. Ready for deploy."
If any fail: State which check failed and why. Do not proceed.
