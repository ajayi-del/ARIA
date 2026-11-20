# prop_fix_monitor (host collector)

Deterministic Vultr collector for the 4h **fix-vs-intent** monitor.

- **Arithmetic lives here** → `/root/prop_fix_monitor/report.md` + `verdict.json` + `cycles.jsonl`
- **Judgment lives in Cursor** → Project long-lived agent woken by `cursor-subscriptions.subscribe_timer`
- **Telegram** → optional `telegram.env` (chmod 600); cycle never requires keys
- **Kill switch** → `touch /root/prop_fix_monitor/DISABLED`

Full architecture, oracle contract, timer ownership, and agent prompt:
Project store `docs/prop-fix-monitor-cron.md`.

```bash
# dry-run
MONITOR_ROOT=/root/prop_fix_monitor /root/prop_fix_monitor/run_cycle.sh
cat /root/prop_fix_monitor/report.md
```

Host cron (UTC, staggered :15 — Vultr crontab was empty; no aria_watchdog on this host):

```cron
15 */4 * * * /root/prop_fix_monitor/run_cycle.sh
```
