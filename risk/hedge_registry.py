"""
Hedge Registry — P0 hedge spine (2026-09-19 Governor-approved hedge venue).

A separate registry for hedge legs: intentional SHORTs on the hedge venue
(config.hedge_venue, default "bybit") opened against Aster/SoDEX primary
longs. The primary PositionManager NETS opposite sides by symbol
(risk/position_manager.py add(), one-way mode) — a hedge short added there
would annihilate the long's record. Hedge legs are therefore NEVER added to
the PositionManager; they live here, keyed by plan/pair id, with plain
lookup semantics and NO netting.

P0 scope: pure data structure + lookups (by_symbol / by_pair / all).
Nothing populates it yet — config.hedge_enabled=False keeps the whole
subsystem inert; P2 wires the live hedge lifecycle.
"""

import threading
from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class HedgeLeg:
    """One hedge leg. `pair_id` is the plan/pair key joining this hedge to
    its primary leg; `hedge_of` mirrors the primary-leg plan id for
    join-by-primary lookups."""
    pair_id: str
    symbol: str
    venue: str                 # execution venue of the hedge leg ("bybit")
    side: str                  # "long" | "short" — hedges are shorts in v1
    qty: float
    entry_price: float
    hedge_of: str = ""         # primary-leg plan id this hedge offsets
    state: str = "open"        # "open" | "closing" | "closed"


class HedgeRegistry:
    """Thread-safe store of hedge legs. No netting, no PnL, no side effects.

    Thread idiom: a plain threading.Lock per mutation/read (same pattern as
    the stdlib-guarded stores elsewhere in the repo); all work is short and
    non-blocking so the asyncio loop never stalls on it.
    """

    def __init__(self):
        self._legs: Dict[str, HedgeLeg] = {}
        self._lock = threading.Lock()

    def upsert(self, pair_id: str, *, symbol: str, venue: str, side: str,
               qty: float, entry_price: float, hedge_of: str = "",
               state: str = "open") -> HedgeLeg:
        """Insert or replace the leg for a pair id. One leg per pair id —
        re-hedging the same plan REPLACES the record, never nets it (the
        deliberate contrast with PositionManager.add)."""
        leg = HedgeLeg(pair_id=str(pair_id), symbol=symbol, venue=venue,
                       side=side, qty=float(qty),
                       entry_price=float(entry_price),
                       hedge_of=hedge_of, state=state)
        with self._lock:
            self._legs[leg.pair_id] = leg
        return leg

    def update(self, _pair_id: str, **fields) -> Optional[HedgeLeg]:
        """Patch fields on an existing leg (qty/entry_price/state/...).
        Returns None when the pair id is unknown. pair_id itself is
        immutable (a `pair_id` field in the patch is ignored)."""
        with self._lock:
            leg = self._legs.get(str(_pair_id))
            if leg is None:
                return None
            for k, v in fields.items():
                if k != "pair_id" and hasattr(leg, k):
                    setattr(leg, k, v)
            return leg

    def remove(self, pair_id: str) -> Optional[HedgeLeg]:
        """Drop a leg (hedge closed). Returns the removed leg or None."""
        with self._lock:
            return self._legs.pop(str(pair_id), None)

    def get(self, pair_id: str) -> Optional[HedgeLeg]:
        with self._lock:
            return self._legs.get(str(pair_id))

    def by_pair(self, pair_id: str) -> Optional[HedgeLeg]:
        """Alias of get() — reads naturally at call sites keyed by plan id."""
        return self.get(pair_id)

    def by_symbol(self, symbol: str) -> List[HedgeLeg]:
        with self._lock:
            return [leg for leg in self._legs.values() if leg.symbol == symbol]

    def all(self) -> List[HedgeLeg]:
        with self._lock:
            return list(self._legs.values())

    def __len__(self) -> int:
        with self._lock:
            return len(self._legs)
