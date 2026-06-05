import time
from pathlib import Path
from typing import Dict, List, Tuple

from core.state_persistence import atomic_load, atomic_save

_BASELINE_PATH = Path("data/cascade_baselines.json")


class CascadeBasketIntelligence:
    """
    L4 book-driven intelligence for cascade basket entry + exit.

    Three functions:
      1. get_harvest_decision()  — should basket harvest now? At what threshold?
      2. rank_entry_symbols()    — which symbols have L4 confirmation for cascade entry?
      3. is_exit_safe()          — is L4 spread safe enough to close without slippage?
    """

    def __init__(self, orderbook_stores: dict, mark_price_stores: dict):
        self._obs = orderbook_stores
        self._marks = mark_price_stores
        # Baseline snapshots: captured once per symbol when cascade is NOT active.
        self._depth_baselines: dict[str, float] = {}   # sym -> avg depth USD
        self._spread_baselines: dict[str, float] = {}  # sym -> avg spread bps
        self._last_baseline_ts: float = 0.0
        # Restore baselines from disk so restarts don't create a blind window
        _loaded = atomic_load(_BASELINE_PATH, max_age_s=86400)
        if _loaded:
            self._depth_baselines = _loaded.get("depth", {})
            self._spread_baselines = _loaded.get("spread", {})

    def update_baselines(self, symbols: list[str]) -> None:
        """Called every 60s when NOT in cascade — captures normal-regime L4 depth/spread."""
        for sym in symbols:
            ob = self._obs.get(sym)
            if not ob or ob.age_ms() > 10_000:
                continue
            try:
                bid, ask, spread = ob.top_of_book()
                mid = (bid + ask) / 2
                if mid <= 0:
                    continue
                spread_bps = (spread / mid) * 10_000
                bid_depth = ob.depth_usd(side="bid", levels=5)
                ask_depth = ob.depth_usd(side="ask", levels=5)
                total_depth = bid_depth + ask_depth
                # EMA smooth: 0.1 weight to new sample
                old_depth = self._depth_baselines.get(sym, total_depth)
                self._depth_baselines[sym] = old_depth * 0.9 + total_depth * 0.1
                old_spread = self._spread_baselines.get(sym, spread_bps)
                self._spread_baselines[sym] = old_spread * 0.9 + spread_bps * 0.1
            except Exception:
                pass
        self._last_baseline_ts = time.time()
        # Persist baselines so restarts don't cold-start
        atomic_save(_BASELINE_PATH, {
            "depth": self._depth_baselines,
            "spread": self._spread_baselines,
        })

    def get_depth_ratio(self, symbol: str, side: str) -> float:
        """
        Current L4 depth on exit side / baseline depth.

        side="long"  -> exit via sell -> check BID depth
        side="short" -> exit via buy  -> check ASK depth

        Returns:
          < 0.3  -> depth depleted (cascade in progress, reversal imminent)
          0.3-0.7 -> depth recovering (safe to harvest with care)
          > 0.7  -> depth normal (safe to harvest at any threshold)
        """
        ob = self._obs.get(symbol)
        if not ob or ob.age_ms() > 10_000:
            return 1.0  # no data -> assume normal

        baseline = self._depth_baselines.get(symbol, 0)
        if baseline <= 0:
            return 1.0

        current_depth = ob.depth_usd(
            side="bid" if side == "long" else "ask", levels=5
        )
        return min(2.0, current_depth / baseline)

    def is_exit_safe(self, symbol: str, size_usd: float) -> tuple[bool, float]:
        """
        L4 spread gate: should we execute this close order?

        Returns (safe, spread_cost_pct):
          safe=True   -> spread is <= 2x baseline, close will not eat the profit
          safe=False  -> spread blown out, wait for normalization
          spread_cost_pct -> estimated slippage cost as % of position notional
        """
        ob = self._obs.get(symbol)
        if not ob or ob.age_ms() > 10_000:
            return True, 0.0  # no data -> proceed (conservative)

        try:
            bid, ask, spread = ob.top_of_book()
            mid = (bid + ask) / 2
            current_bps = (spread / mid) * 10_000 if mid > 0 else 0
        except Exception:
            return True, 0.0

        baseline_bps = self._spread_baselines.get(symbol, current_bps)
        if baseline_bps <= 0:
            baseline_bps = current_bps

        spread_ratio = current_bps / max(baseline_bps, 0.1)
        spread_cost_pct = (current_bps / 10_000) * 100  # half-spread as cost

        return spread_ratio <= 2.0, spread_cost_pct

    def rank_entry_symbols(
        self, candidates: list[str], cascade_direction: str
    ) -> list[tuple[str, float]]:
        """
        Rank symbols by L4 imbalance confirmation for cascade entry.

        Cascade bearish + L4 imbalance > +0.4 (bid-heavy) -> contradiction, skip
        Cascade bearish + L4 imbalance < -0.3 (ask-heavy) -> confirmation, rank high
        Cascade bullish + L4 imbalance > +0.4 (bid-heavy) -> confirmation, rank high
        Cascade bullish + L4 imbalance < -0.3 (ask-heavy) -> contradiction, skip

        Returns [(symbol, l4_score)] sorted by confirmation strength.
        """
        scored: list[tuple[str, float]] = []
        for sym in candidates:
            ob = self._obs.get(sym)
            if not ob or ob.age_ms() > 10_000:
                scored.append((sym, 0.0))  # no L4 data -> neutral
                continue

            imb = ob.imbalance(depth=5)
            spread_ratio = self._get_spread_ratio(sym)

            if cascade_direction == "short":  # bearish cascade -> we want to short
                # Confirmation: asks being rebuilt, bids pulled -> imbalance goes negative
                # imbalance < -0.3 means ask_vol >> bid_vol -> bearish confirmed
                l4_score = -imb * 2.0  # negative imbalance = bearish confirmation
            elif cascade_direction == "long":  # bullish cascade -> we want to long
                # Confirmation: bids being consumed -> imbalance goes negative
                l4_score = -imb * 2.0
            else:
                l4_score = 0.0

            # Penalize blown spreads — entering into a 5x spread eats the edge
            if spread_ratio > 3.0:
                l4_score *= 0.3  # severe penalty
            elif spread_ratio > 2.0:
                l4_score *= 0.6  # moderate penalty

            scored.append((sym, round(l4_score, 4)))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    def _get_spread_ratio(self, symbol: str) -> float:
        ob = self._obs.get(symbol)
        if not ob or ob.age_ms() > 10_000:
            return 1.0
        try:
            bid, ask, spread = ob.top_of_book()
            mid = (bid + ask) / 2
            current_bps = (spread / mid) * 10_000 if mid > 0 else 0
        except Exception:
            return 1.0
        baseline = self._spread_baselines.get(symbol, current_bps)
        return current_bps / max(baseline, 0.1) if baseline > 0 else 1.0
