#!/usr/bin/env python3
"""Prop harness smoke test — one real bracket on the demo account.

Places a minimum-size BTC bracket with native TPSL (entry + stop + 3 TP
legs in one batch), verifies the exit legs exist venue-side, then flattens
and reports round-trip fees. This is the M3 executor-vs-demo proof.

Usage:
  python3 tools/prop_smoke.py --ref-price 100000 [--asset BTC] [--keep]

--ref-price is required: Propr REST has no ticker endpoint (prices arrive
via ARIA outbox in M4 or the WS feed).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prop_harness.brains.budget import BudgetBrain  # noqa: E402
from prop_harness.brains.drawdown import DrawdownBrain  # noqa: E402
from prop_harness.config import PropConfig  # noqa: E402
from prop_harness.executor import ProprExecutor  # noqa: E402
from prop_harness.io.propr_client import ProprClient  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ref-price", type=float, required=True,
                        help="current mark price (REST has no ticker)")
    parser.add_argument("--asset", default="BTC")
    parser.add_argument("--risk", type=float, default=5.0)
    parser.add_argument("--stop-pct", type=float, default=0.005)
    parser.add_argument("--keep", action="store_true",
                        help="leave position open (default: flatten)")
    args = parser.parse_args()

    config = PropConfig.from_env()
    if not config.enabled:
        print("PROP_ENABLED=false — refusing to trade. Set it in prop.env.")
        return 2

    client = ProprClient(api_key=config.api_key, base_url=config.api_url)
    client.setup(config.account_id or None)
    root = config.state_root
    (root / "accounts" / "acct1").mkdir(parents=True, exist_ok=True)

    drawdown = DrawdownBrain(root / "accounts" / "acct1" / "dd.json",
                             floor_line=config.floor_line,
                             floor_usd=config.internal_floor_usd)
    equity = float(client.get_equity())
    state, _ = drawdown.update(equity)
    print(f"equity ${equity:.2f} | band {state.band} | headroom ${state.headroom:.2f}")

    budget = BudgetBrain(root / "accounts" / "acct1" / "budget_day.json",
                         hard_zero=config.hard_zero_headroom_usd,
                         r_base=config.r_base_usd, r_max=config.r_max_usd)
    budget.roll(state.headroom)
    print(f"budget today ${budget.state.budget:.2f} | "
          f"risk cap ${budget.risk_cap(drawdown.mult):.2f}")

    executor = ProprExecutor(config, client, budget, drawdown)

    ref = args.ref_price
    stop = ref * (1 - args.stop_pct)
    print(f"\nplacing bracket: {args.asset} long, ref={ref}, stop={stop:.1f}, "
          f"risk=${args.risk}")
    result = executor.place_bracket(
        asset=args.asset, side="buy", risk_usd=args.risk,
        entry_ref=ref, stop_price=stop,
    )
    if not result.ok:
        print(f"REJECTED: {result.reason}")
        return 1
    print(f"placed: {result.semantic_id} group={result.group_id}")
    for order in result.orders or []:
        print(f"  {order.get('type'):>20} {order.get('status'):>10} "
              f"qty={order.get('quantity')} id={order.get('orderId')}")

    time.sleep(3)
    open_orders = client.get_open_orders(base=args.asset)
    types = sorted(o["type"] for o in open_orders)
    print(f"\nopen orders venue-side: {types}")
    exits = [t for t in types if t in ("stop_market", "take_profit_limit")]
    if not exits:
        print("SMOKE FAIL: no native TPSL legs resting on venue")
        if not args.keep:
            executor.flatten_all("smoke-fail-cleanup")
        return 1
    print(f"native TPSL confirmed: {len(exits)} exit legs")

    positions = client.get_positions(status="open", base=args.asset)
    for p in positions:
        print(f"position: {p['positionSide']} {p['quantity']} {p['asset']} "
              f"@ {p.get('entryPrice')} uPnL={p.get('unrealizedPnl')}")

    if args.keep:
        print("--keep: leaving bracket open")
        return 0

    print("\nflattening...")
    executor.flatten_all("smoke-complete")
    time.sleep(2)
    remaining = client.get_positions(status="open", base=args.asset)
    trades = client.get_trades(base=args.asset, limit=10)
    fees = sum(float(t.get("fee", 0)) for t in trades[:8])
    print(f"remaining positions: {len(remaining)} | fees this round-trip: "
          f"~${fees:.4f}")
    if remaining:
        print("SMOKE FAIL: position still open after flatten")
        return 1
    print("SMOKE PASS — executor, native TPSL, flatten all verified on demo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
