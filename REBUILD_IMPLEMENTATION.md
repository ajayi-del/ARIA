# ARIA Rebuild Implementation List
# Generated: 2026-04-25
# State: HALTED — 71% drawdown, balance $58.22, zero open positions

## Quant Diagnosis

### The $100 Cliff (15:04 UTC, Apr 25)
- Balance dropped from $159.35 → $59.34 in ~67 seconds.
- **Zero corresponding trade logs** in that window (no position_closed, no order_filled, no liquidation).
- Direct SoDEX API confirms perps `av` = $58.22 (real).
- Three stale **unfilled limit orders** consuming $58.19 margin (GOOGL, META, AMZN).
- `at = -29.06` in SoDEX balance state suggests ~$29 in accumulated funding/realized losses.
- **Unexplained gap: ~$107** missing from perps account with no logged trading activity.
- Most likely causes (ranked):
  1. External withdrawal or auto-rebalance out of perps account (unlogged)
  2. SoDEX socialized loss / ADL / settlement event (unlogged)
  3. Balance fetch seeded incorrectly at startup (staked MAG7 $201 conflated with perps balance)
  4. Large position liquidated between pnl_attribution cycles (logging gap)

### Drawdown Math
- DrawdownManager peak: $201.31 (suspicious — matches staked MAG7 value, not first perps balance of $194.50)
- DrawdownGuard peak: $171.27 (different tracker, different state — **dual-system bug**)
- Actual loss from first logged perps balance ($194.50 → $58.22) = 70.1%
- Daily PnL from tracked positions ≈ -$5. Daily drawdown from day_start ($185.53) = 68.6%.
- **Conclusion: the 71% is numerically correct if peak was $201, but the peak may be wrong.**

### Why It Cannot Trade Now
- Balance floor halt: balance $58.22 < floor $58.0 (intermittent, but real)
- Drawdown manager halt: 71% > 50% max
- Min notional gate: $80 > $58.22 balance → every candidate rejected
- Size multiplier: 0.0

---

## Phase 1: Emergency Stabilization (Do First)

- [ ] Cancel 3 stale limit orders (GOOGL, META, AMZN) freeing $58.19 margin
- [ ] Reset drawdown state to realistic anchors (peak = $60, current = $58.22)
- [ ] Lower min_notional from $80 to $50 (or balance-aware dynamic floor)
- [ ] Fix dual drawdown tracker divergence (DrawdownGuard vs DrawdownManager)
- [ ] Restart in tmux session `aria`

## Phase 2: Forensic & Logging Hardening

- [ ] Add `order_history` and `funding_history` fetch to startup diagnostic
- [ ] Log every balance change > $1 with delta attribution (funding, trade, transfer, fee)
- [ ] Ensure position_closed always fires BEFORE pnl_attribution
- [ ] Add SoDEX `at` (accumulated) field to balance telemetry
- [ ] DrawdownManager peak must be seeded exclusively from perps `av`, never from portfolio/staked value

## Phase 3: Sizing & Gate Fixes for Low Balance

- [ ] Min notional = `max(10, balance * 0.15)` instead of hard $80
- [ ] Risk per trade floor: $1.00 minimum (so 1.7% at $58, not 3%)
- [ ] Drawdown thresholds scale with balance: $58 account cannot afford 50% DD cap same as $500 account
- [ ] Add "micro_mode" when balance < $100: lower leverage cap, tighter stops, smaller sizing

## Phase 4: Relaunch Protocol

1. Confirm no open positions (`P = null` already confirmed)
2. Run tests locally: `python3 -m pytest tests/ -q`
3. Push fixes to main, pull on server
4. Cancel stale orders (via API or bot startup cleanup)
5. Reset drawdown_state.json anchors
6. Start in tmux: `tmux new -s aria -d 'python3 main.py'`
7. Watch logs for 60s: grep `account_balance|drawdown|halt`

---

## Absolute Blockers Before Relaunch

1. **Must explain the missing $107.** If it was a withdrawal, note it. If it was a platform event, document it.
2. **Must fix min_notional > balance** or the bot will spin evaluating signals it can never execute.
3. **Must reconcile DrawdownGuard and DrawdownManager** to one source of truth.
