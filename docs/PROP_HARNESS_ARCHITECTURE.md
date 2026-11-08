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

## Stop-ack FSM

Typed protection truth (management §6 build #1): `ARMED → ACKED`, else `MISSED` → halt new risk + heal (`resize_protection`) then reduce. Kill `PROP_STOP_ACK_FSM=0`. See [`docs/STOP_ACK_FSM.md`](./STOP_ACK_FSM.md).

## Ops

- State + `prop.env` **outside** repo (`PROP_STATE_ROOT`). Secrets never in git.
- Do not restart with open book unless Governor-ordered; verify exchange positions first.
- Tests: `.venv/bin/python -m pytest tests/prop_harness/ -q`
