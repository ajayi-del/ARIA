#!/usr/bin/env python3
"""Prop harness boot check — run before every session start.

Verifies: API health, key auth, active challenge -> accountId, equity read,
leverage limits, margin config, bracket-builder sanity (offline).

Usage: python3 tools/prop_boot_check.py
Exit 0 only if every step passes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prop_harness.bracket import DEFAULT_LADDER, build_bracket  # noqa: E402
from prop_harness.config import PropConfig  # noqa: E402
from prop_harness.io.propr_client import ProprClient, ProprAPIError  # noqa: E402

PASS, FAIL = "PASS", "FAIL"


def main() -> int:
    config = PropConfig.from_env()
    failures = 0

    def step(name: str, fn):
        nonlocal failures
        try:
            result = fn()
            print(f"[{PASS}] {name}: {result}")
            return result
        except Exception as exc:
            failures += 1
            print(f"[{FAIL}] {name}: {exc}")
            return None

    step("config", lambda: f"state_root={config.state_root} "
                           f"api={config.api_url} enabled={config.enabled} "
                           f"floor=${config.internal_floor_usd}")

    if not config.api_key:
        print(f"[{FAIL}] PROP_API_KEY missing — put it in "
              f"{config.state_root}/prop.env")
        return 1

    client = ProprClient(api_key=config.api_key, base_url=config.api_url)

    step("health", lambda: client.health())
    user = step("auth users/me", lambda: client.users_me())

    def _setup():
        acct = client.setup(config.account_id or None)
        return acct
    account_id = step("challenge-attempts -> accountId", _setup)

    if account_id:
        acct = step("account equity", lambda: client.get_account())
        if acct:
            bal = float(acct.get("balance", 0))
            upnl = float(acct.get("totalUnrealizedPnl", 0))
            iso = float(acct.get("isolatedPositionMargin", 0))
            print(f"       balance=${bal:.2f} uPnL=${upnl:.2f} "
                  f"iso_margin=${iso:.2f} equity=${bal+upnl+iso:.2f} "
                  f"HWM=${float(acct.get('highWaterMark', 0)):.2f}")
        step("open positions", lambda: len(client.get_positions(status="open")))
        step("open orders", lambda: len(client.get_open_orders()))

    limits = step("leverage limits", lambda: client.leverage_limits())
    if limits:
        print(f"       defaultMax={limits.get('defaultMax')} "
              f"overrides={limits.get('overrides')}")

    if account_id:
        step("margin config BTC", lambda: client.get_margin_config("BTC"))

    def _bracket():
        body = build_bracket(
            account_id="urn:prp-account:offline", asset="BTC", side="buy",
            quantity="0.010", entry_ref="100000", stop_price="99500",
            ts_ms=1794000000000, ladder=DEFAULT_LADDER,
        )
        n = len(body["orders"])
        assert n == 5, f"expected 5 orders, got {n}"
        assert body["orderGroupId"], "group id missing"
        # min-size bracket (0.001 BTC) correctly degenerates to entry+SL+runner
        mini = build_bracket(
            account_id="urn:prp-account:offline", asset="BTC", side="buy",
            quantity="0.001", entry_ref="100000", stop_price="99500",
            ts_ms=1794000000000,
        )
        assert len(mini["orders"]) == 3, f"min bracket: expected 3, got {len(mini['orders'])}"
        return f"{n} orders (entry+SL+3TP), group ok, min-size degenerate ok"
    step("offline bracket shape", _bracket)

    print()
    if failures:
        print(f"boot check FAILED ({failures} step(s))")
        return 1
    print("boot check PASS — harness may start")
    return 0


if __name__ == "__main__":
    sys.exit(main())
