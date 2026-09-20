"""intelligence/exposure_snapshot.py — Book-exposure math (pure brain).

2026-09-19: the snapshot loop (main.py side) needs one honest number set for
the whole book — primary legs and hedge legs — per pass. This module is the
pure computation: signed/unsigned notional, primary vs hedge split, per-venue
net. Longs are +, shorts are −. Legs missing mark or size are skipped (a leg
we cannot price carries no measurable exposure). Zero I/O.
"""
from __future__ import annotations

from typing import Dict, List


def _signed_notional(leg: dict):
    """Signed USD notional of one leg, or None when unpriceable."""
    try:
        size = float(leg.get("size"))
        mark = float(leg.get("mark"))
    except (TypeError, ValueError):
        return None
    if leg.get("side") == "short":
        return -(size * mark)
    return size * mark          # unknown side reads as long — fail-open


def compute_book_exposure(positions: List[dict],
                          hedge_legs: List[dict]) -> dict:
    """Net/gross/primary/hedge/per-venue exposure across the book."""
    primary_net = 0.0
    hedge_net = 0.0
    gross = 0.0
    per_venue: Dict[str, float] = {}

    for legs, is_hedge in ((positions, False), (hedge_legs, True)):
        for leg in (legs or []):
            signed = _signed_notional(leg)
            if signed is None:
                continue
            gross += abs(signed)
            venue = str(leg.get("venue") or "unknown")
            per_venue[venue] = per_venue.get(venue, 0.0) + signed
            if is_hedge:
                hedge_net += signed
            else:
                primary_net += signed

    return {
        "net_notional": primary_net + hedge_net,
        "gross_notional": gross,
        "primary_net": primary_net,
        "hedge_net": hedge_net,
        "per_venue": per_venue,
    }
