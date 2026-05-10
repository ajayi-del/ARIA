---
name: emergency_stop
description: Emergency halt for ARIA trading bot when risk thresholds are breached
allowed-tools: [Bash, Read, Write]
when_to_use: When drawdown exceeds limits, open positions show anomalous behavior, user explicitly says stop, or before risky operations
arguments:
  - name: reason
    type: string
    description: Why the stop is being initiated
---

# ARIA Emergency Stop Skill

## Immediate Actions (in order)

1. **Log the stop reason**
   ```bash
   echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) EMERGENCY_STOP: ${reason}" >> logs/aria.log
   ```

2. **Check if process is running**
   ```bash
   ps aux | grep "python.*main.py" | grep -v grep
   ```

3. **Kill the process if found**
   ```bash
   pkill -f "python.*main.py" || pkill -f "python3.*main.py"
   ```

4. **Verify shutdown**
   ```bash
   sleep 2 && ps aux | grep "python.*main.py" | grep -v grep || echo "Process terminated"
   ```

5. **Capture final state**
   ```bash
   tail -50 logs/aria.log > logs/emergency_stop_$(date +%s).log
   ```

## Post-Stop Protocol

- Report to user: process status, last known positions, and any open orders
- Do NOT restart without explicit user approval
- If positions remain open on exchange, flag this immediately
