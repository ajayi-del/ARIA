---
name: audit_and_commit
description: Audit changes, run tests, and create a clean commit following ARIA conventions
allowed-tools: [Bash, Read, Write, Edit]
when_to_use: When the user says commit, before pushing, or when wrapping up a feature/fix
arguments:
  - name: message
    type: string
    description: "Suggested commit message (imperative, lowercase after colon)"
---

# Audit and Commit Skill

## 1. Staged Changes Audit
```bash
git diff --cached --stat
git diff --cached
```
Review every line. Ensure:
- No secrets, API keys, or private keys
- No debug prints or TODOs left in production code
- Only intended files are staged

## 2. Test Gate
```bash
.venv/bin/python -m pytest tests/ -q
```
Must pass. If failing, fix or unstage broken changes.

## 3. Commit Message Rules
- Format: `type: description` (e.g. `fix: drawdown guard sync`)
- Types: feat, fix, refactor, test, docs, chore
- Imperative mood, lowercase after colon
- Max 72 chars first line

## 4. Execute
```bash
git commit -m "${message}"
```

## 5. Post-Commit
```bash
git log --oneline -3
```
Show the user the commit hash and summary.
