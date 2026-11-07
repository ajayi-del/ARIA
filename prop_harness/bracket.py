"""Pure bracket-order construction for the Propr batch API. No I/O.

Venue rules encoded here (docs/api.md, verified 2026-11-06):
- conditional orders (stop/take_profit) need a positionId OR must ride in the
  same batch as an entry order -> brackets are ALWAYS one batch: naked
  positions are unrepresentable.
- one entry order per request; >1 order requires a top-level orderGroupId ULID.
- intentId must be a ULID; identical intentId is deduped (idempotent retry).
- reduceOnly on every exit leg: selling without it opens a fresh short.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_DOWN
from typing import Any

from .ids import intent_ulid, new_ulid

# asset -> (min_quantity, quantity_decimals). xyz: HIP-3 assets use the default.
SPECS: dict[str, tuple[str, int]] = {
    "BTC": ("0.001", 3),
    "ETH": ("0.01", 2),
    "SOL": ("0.1", 1),
    "DOGE": ("1", 0),
    "XRP": ("1", 0),
    "AVAX": ("0.1", 1),
    "LINK": ("0.1", 1),
    "AAVE": ("0.01", 2),
    "ZEC": ("0.01", 2),
    "BCH": ("0.01", 2),
}
XYZ_DEFAULT_SPEC = ("0.01", 2)

# quant TP ladder: (R multiple, fraction of position)
DEFAULT_LADDER: tuple[tuple[str, str], ...] = (
    ("2.0", "0.5"),
    ("3.5", "0.3"),
    ("6.0", "0.2"),
)


def spec_for(asset: str) -> tuple[Decimal, int]:
    if asset in SPECS:
        mn, dec = SPECS[asset]
    elif asset.startswith("xyz:"):
        mn, dec = XYZ_DEFAULT_SPEC
    else:
        raise KeyError(f"no symbol spec for {asset!r} — sync before trading")
    return Decimal(mn), dec


def round_qty(qty: Decimal, decimals: int) -> Decimal:
    return qty.quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_DOWN)


def tp_price(entry: Decimal, stop: Decimal, r_mult: Decimal, side: str,
             fee_rate: Decimal = Decimal(0)) -> Decimal:
    """Price where the leg banks r_mult × gross risk NET of round-trip fees.

    Net per unit = |p − e| − f(e + p); solve net = k·|e − s|:
      long:  p = [e(1+f) + k·risk] / (1−f)
      short: p = [e(1−f) − k·risk] / (1+f)
    fee_rate=0 -> legacy pure-geometry ladder bit-for-bit. Governor 2026-11-07:
    measured fills showed 0.045%/side taker on the demo account — a nominal
    2R target was netting ~1.8R before this.
    """
    risk = abs(entry - stop)
    if risk <= 0:
        raise ValueError("entry and stop must differ")
    f = Decimal(str(fee_rate))
    if side == "buy":
        return (entry * (1 + f) + risk * r_mult) / (1 - f)
    return (entry * (1 - f) - risk * r_mult) / (1 + f)


def fmt(d: Decimal) -> str:
    """Venue-bound numeric string: no trailing zeros, no exponent notation."""
    return format(d.normalize(), "f")


def split_ladder(
    quantity: Decimal, decimals: int, min_qty: Decimal,
    ladder: tuple[tuple[str, str], ...],
) -> list[tuple[Decimal, Decimal]]:
    """(r_mult, leg_qty) per rung; runner absorbs rounding remainder."""
    legs: list[tuple[Decimal, Decimal]] = []
    allocated = Decimal(0)
    for r_mult, frac in ladder[:-1]:
        leg = round_qty(quantity * Decimal(frac), decimals)
        if leg >= min_qty:
            legs.append((Decimal(r_mult), leg))
            allocated += leg
    runner = round_qty(quantity - allocated, decimals)
    if runner < min_qty:
        raise ValueError(f"runner leg {runner} below min qty {min_qty}")
    legs.append((Decimal(ladder[-1][0]), runner))
    return legs


def build_bracket(
    account_id: str,
    asset: str,
    side: str,
    quantity: str,
    entry_ref: str,
    stop_price: str,
    ts_ms: int,
    quote: str = "USDC",
    entry_type: str = "market",
    ladder: tuple[tuple[str, str], ...] = DEFAULT_LADDER,
    fee_rate: str = "0",
) -> dict[str, Any]:
    """One batch: entry + full-size stop + ladder TP legs. Returns request body."""
    if side not in ("buy", "sell"):
        raise ValueError(f"bad side {side!r}")
    entry = Decimal(str(entry_ref))
    stop = Decimal(str(stop_price))
    qty = Decimal(str(quantity))
    min_qty, decimals = spec_for(asset)
    qty = round_qty(qty, decimals)
    if qty < min_qty:
        raise ValueError(f"qty {qty} below min {min_qty} for {asset}")
    if side == "buy" and not stop < entry:
        raise ValueError("long stop must be below entry_ref")
    if side == "sell" and not stop > entry:
        raise ValueError("short stop must be above entry_ref")

    position_side = "long" if side == "buy" else "short"
    exit_side = "sell" if side == "buy" else "buy"
    base_common: dict[str, Any] = {
        "accountId": account_id,
        "exchange": "hyperliquid",
        "positionSide": position_side,
        "productType": "perp",
        "asset": asset,
        "base": asset,
        "quote": quote,
    }

    entry_order = {
        **base_common,
        "intentId": intent_ulid(account_id, f"{asset}-entry", ts_ms),
        "type": entry_type,
        "side": side,
        "timeInForce": "IOC" if entry_type == "market" else "GTC",
        "quantity": fmt(qty),
        "reduceOnly": False,
        "closePosition": False,
    }
    if entry_type == "limit":
        entry_order["price"] = fmt(entry)

    sl_order = {
        **base_common,
        "intentId": intent_ulid(account_id, f"{asset}-sl", ts_ms),
        "type": "stop_market",
        "side": exit_side,
        "timeInForce": "GTC",
        "quantity": fmt(qty),
        "triggerPrice": fmt(stop),
        "reduceOnly": True,
        "closePosition": False,
    }

    orders = [entry_order, sl_order]
    for i, (r_mult, leg_qty) in enumerate(split_ladder(qty, decimals, min_qty, ladder)):
        tp = tp_price(entry, stop, r_mult, side, fee_rate=Decimal(fee_rate))
        orders.append({
            **base_common,
            "intentId": intent_ulid(account_id, f"{asset}-tp{i}", ts_ms),
            "type": "take_profit_limit",
            "side": exit_side,
            "timeInForce": "GTC",
            "quantity": fmt(leg_qty),
            "price": fmt(tp),
            "triggerPrice": fmt(tp),
            "reduceOnly": True,
            "closePosition": False,
        })

    return {"orders": orders, "orderGroupId": new_ulid(ts_ms)}
