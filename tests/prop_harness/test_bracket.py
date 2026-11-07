"""Bracket builder shape tests — offline, no venue."""

from decimal import Decimal

import pytest

from prop_harness.bracket import (
    DEFAULT_LADDER, build_bracket, round_qty, spec_for, split_ladder, tp_price,
)
from prop_harness.ids import intent_ulid, new_ulid, semantic_id


def test_ulid_format_and_determinism():
    a = intent_ulid("acct1", "BTC-entry", 1794000000000)
    b = intent_ulid("acct1", "BTC-entry", 1794000000000)
    c = intent_ulid("acct1", "BTC-entry", 1794000000001)
    assert a == b, "same seed must produce same ULID (idempotent retry)"
    assert a != c
    assert len(a) == 26
    assert len(new_ulid()) == 26
    assert semantic_id("acct1", "BTC", 123) == "prop-acct1-BTC-123"


def test_tp_price_sides():
    assert tp_price(Decimal("100"), Decimal("95"), Decimal("2"), "buy") == Decimal("110")
    assert tp_price(Decimal("100"), Decimal("105"), Decimal("2"), "sell") == Decimal("90")
    with pytest.raises(ValueError):
        tp_price(Decimal("100"), Decimal("100"), Decimal("2"), "buy")


def test_ladder_sums_to_full_quantity():
    legs = split_ladder(Decimal("0.001"), 3, Decimal("0.001"), DEFAULT_LADDER)
    # 0.001 min qty: only the runner survives; runner absorbs everything
    assert sum(q for _, q in legs) == Decimal("0.001")
    legs = split_ladder(Decimal("0.010"), 3, Decimal("0.001"), DEFAULT_LADDER)
    assert sum(q for _, q in legs) == Decimal("0.010")
    assert [str(r) for r, _ in legs] == ["2.0", "3.5", "6.0"]


def test_bracket_shape_long():
    body = build_bracket(
        account_id="urn:prp-account:x", asset="BTC", side="buy",
        quantity="0.010", entry_ref="100000", stop_price="99500",
        ts_ms=1794000000000,
    )
    assert body["orderGroupId"]
    orders = body["orders"]
    assert len(orders) == 5  # entry + SL + 3 TP
    entry, sl = orders[0], orders[1]
    tps = orders[2:]

    assert entry["type"] == "market" and entry["side"] == "buy"
    assert entry["reduceOnly"] is False
    assert entry["timeInForce"] == "IOC"

    assert sl["type"] == "stop_market" and sl["side"] == "sell"
    assert sl["reduceOnly"] is True and sl["triggerPrice"] == "99500"
    assert sl["quantity"] == "0.01"  # full-size stop (normalized)

    expected_tps = ["101000", "101750", "103000"]  # 2R, 3.5R, 6R off 500 risk
    for tp_order, expected in zip(tps, expected_tps):
        assert tp_order["type"] == "take_profit_limit"
        assert tp_order["reduceOnly"] is True
        assert tp_order["triggerPrice"] == expected
        assert tp_order["price"] == expected
    tp_qty = sum(Decimal(o["quantity"]) for o in tps)
    assert tp_qty == Decimal("0.010")

    for order in orders:
        assert order["exchange"] == "hyperliquid"
        assert order["positionSide"] == "long"
        assert len(order["intentId"]) == 26


def test_bracket_shape_short():
    body = build_bracket(
        account_id="x", asset="ETH", side="sell",
        quantity="0.10", entry_ref="3000", stop_price="3030",
        ts_ms=1794000000000,
    )
    entry, sl = body["orders"][0], body["orders"][1]
    assert entry["side"] == "sell" and entry["positionSide"] == "short"
    assert sl["side"] == "buy" and sl["triggerPrice"] == "3030"
    tp = body["orders"][2]
    assert tp["triggerPrice"] == "2940"  # 2R below entry


def test_bracket_rejects_bad_geometry():
    with pytest.raises(ValueError):
        build_bracket(account_id="x", asset="BTC", side="buy",
                      quantity="0.01", entry_ref="100000", stop_price="100500",
                      ts_ms=1)
    with pytest.raises(ValueError):
        build_bracket(account_id="x", asset="BTC", side="buy",
                      quantity="0.0001", entry_ref="100000", stop_price="99500",
                      ts_ms=1)
    with pytest.raises(KeyError):
        build_bracket(account_id="x", asset="NOSUCH", side="buy",
                      quantity="1", entry_ref="100", stop_price="99", ts_ms=1)
    # xyz: HIP-3 assets fall back to the default spec
    body = build_bracket(account_id="x", asset="xyz:GOLD", side="buy",
                         quantity="0.05", entry_ref="4000", stop_price="3980",
                         ts_ms=1)
    assert body["orders"][0]["asset"] == "xyz:GOLD"


def test_spec_rounding():
    assert round_qty(Decimal("0.01234"), 3) == Decimal("0.012")
    assert spec_for("BTC") == (Decimal("0.001"), 3)
