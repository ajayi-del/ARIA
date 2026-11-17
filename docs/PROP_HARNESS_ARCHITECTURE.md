# Prop Harness — Operational Summary

ARIA = signal engine. `prop_harness/` = graph that sizes/routes/protects prop sleeves. Never rewrite Kant / Nietzsche / Chancellor / tiers 1–6 from here.

**Wiring schema (cybernetic target):** [`docs/HARNESS_WIRING_SCHEMA.md`](./HARNESS_WIRING_SCHEMA.md) — Government / Fascia / Nations / Armies / reference planes, typed envelopes, migration phases.

## Live topology (2026-11-08, ~$5k evals)

| Account | State root | Sleeve | Role |
|---------|------------|--------|------|
| demo | `~/prop_harness_state` | `all` | Overflow — fires only when owning peer(s) at `max_positions` |
| turbo | `~/prop_harness_state_turbo` | `crypto` | Owns crypto |
| turbo2 | `~/prop_harness_state_turbo2` | `tradfi` | Owns equities / commodities / FX / indices |

- `max_positions` binds **per account process** (live default **3** via `PROP_MAX_POSITIONS` / `from_env`; dataclass default 5 is unused when env-loaded). Not per sleeve inside one process.
- Equities/commodities: **turbo2** (owner) + **demo** only when turbo2 is full. **Not** turbo (`PROP_ASSET_CLASS=crypto`).
- `PROP_FAMILIES` currently **empty** on all three — family partition not active; rotation family-cap still binds one representative per (family, side) **within** an account.

## Eval doctrine (`e9cc7f3` + risk-ceiling fix)

- Profile: `PROP_PROFILE=evaluation` on all three.
- Sizing: coh ≤7 → `$5` probe; coh >7 → 70% remaining day budget; coh ≥8.5 → 100%.
- Day budget `$40` / trend `$50` (trend = day realized PnL > 0). Single-trade ceiling = trend budget.
- Kant belt on `place_bracket`: eval uses `risk_ceiling` (remaining / headroom−hard_zero / trend max). **Must not** re-apply legacy `BudgetBrain.risk_cap` `r_max=$7.5` (that dead-wired every big bet).
- Exit ladder 1R / 2.5R / 5R (40/30/30), fee-net. BE at +0.5R via venue `unrealizedPnl`. Reload: proven-only, harvested risk, tighten-only.
- Loss veto: demo publishes `program/loss_veto.json`; turbos consume via `PROP_LOSS_VETO_PATH` (fail-open on stale).
- Leverage: margin-efficiency only (30% equity cap + provision BTC/ETH/SOL 10x).

## HL marks

Reference-only (`prop_harness/io/hl_marks.py`, `47e24da`). Never feed equity/headroom/risk math. Triggers/telemetry only; fail-open when stale.

## Ops

- State + `prop.env` **outside** repo (`PROP_STATE_ROOT`). Secrets never in git.
- Do not restart with open book unless Governor-ordered; verify exchange positions first.
- Tests: `.venv/bin/python -m pytest tests/prop_harness/ -q`

### Ship-manifest verification (watchdog mandate class, 2026-11-17 — the false-ship lesson)

After ANY prop deploy, the deploy is not done until the host proves it runs the intended bytes. A green local suite + a commit proves NOTHING about the host (2026-11-17: server ran Nov-16 code while fixes sat local).

Registers, all machine-checkable:
1. `/root/ARIA/DEPLOYED_SHA` == the intended commit (written by the deploy step, never by hand).
2. Knob presence: `grep` the shipped knobs in `/root/ARIA/prop_harness/config.py` (the deploy names its manifest of knobs).
3. One PID per seat: `pgrep -f prop_harness.main` matched against `PROP_STATE_ROOT` per state root — never trust `harness.pid` (staleness caused a 4-min double-run 2026-11-17; kill/restart by environ match).
4. Heartbeat age ≤ 90s per seat + zero new tracebacks in `harness.out`.
5. New-code fingerprint: the deploy names one observable that only the new bytes produce (e.g. `vetoed_ts` key in `program/loss_veto.json`).

Any failure = the deploy did not happen; alert, do not self-patch.
