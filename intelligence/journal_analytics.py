"""
intelligence/journal_analytics.py — Pattern miner for the cybernetic feedback loop.

Reads closed trades from the trade journal and computes per-feature performance
metrics. Emits CyberneticAdjustments that Kant and Nietzsche consume to retune
their thresholds.

Latency: O(n) where n = closed trades in journal (max 500 in-memory).
Called every 6 hours or after every 10th trade close.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class StructurePerformance:
    """Empirical performance for a single Kant market structure."""
    n_trades: int = 0
    wins: int = 0
    losses: int = 0
    wr: float = 0.0          # win rate
    avg_r: float = 0.0       # mean R-multiple
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0
    median_hold_ms: float = 0.0
    limit_slippage_pct: float = 0.0   # avg slippage for limit entries
    market_slippage_pct: float = 0.0  # avg slippage for market entries


@dataclass
class WillPerformance:
    """Empirical performance for a (drawdown_band, streak_band) cell."""
    n_trades: int = 0
    wins: int = 0
    losses: int = 0
    wr: float = 0.0
    avg_r: float = 0.0
    avg_win_r: float = 0.0
    avg_loss_r: float = 0.0


@dataclass
class CyberneticAdjustments:
    """Output of the analytics engine — consumed by Kant and Nietzsche."""
    # Per-structure threshold offsets (Kant)
    structure_offsets: Dict[str, Dict[str, float]] = field(default_factory=dict)
    # Per-structure order-type override recommendation
    structure_order_type: Dict[str, Optional[str]] = field(default_factory=dict)
    # Kelly-optimal multiplier per Will Table cell (Nietzsche)
    kelly_multipliers: Dict[str, Dict[str, float]] = field(default_factory=dict)
    # Recommended WillState per cell (Nietzsche)
    kelly_states: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # Global hold-time recommendation per edge profile
    hold_time_recommendations: Dict[str, int] = field(default_factory=dict)


class JournalAnalytics:
    """
    Pattern miner for trade journal outcomes.

    Instantiate once; call `analyze(journal_entries)` periodically.
    """

    # Minimum sample size before trusting empirical metrics
    _MIN_SAMPLE: int = 5

    def analyze(self, entries: List[dict]) -> CyberneticAdjustments:
        """
        Compute all cybernetic adjustments from a list of journal entries.

        entries: list of dicts from TradeJournal.get_closed()
        """
        adj = CyberneticAdjustments()

        if not entries:
            return adj

        closed = [e for e in entries if e.get("outcome") in ("win", "loss")]
        if len(closed) < self._MIN_SAMPLE:
            return adj

        adj.structure_offsets = self._structure_offsets(closed)
        adj.structure_order_type = self._structure_order_types(closed)
        adj.kelly_multipliers, adj.kelly_states = self._kelly_will_table(closed)
        adj.hold_time_recommendations = self._hold_time_analysis(closed)

        return adj

    # ── Kant structure analysis ───────────────────────────────────────────────

    def _structure_offsets(self, closed: List[dict]) -> Dict[str, Dict[str, float]]:
        """Compute coherence_min and size_cap offsets per structure."""
        per_struct: Dict[str, List[dict]] = {}
        for e in closed:
            s = e.get("kant_structure", "normal") or "normal"
            per_struct.setdefault(s, []).append(e)

        offsets: Dict[str, Dict[str, float]] = {}
        for struct, trades in per_struct.items():
            if len(trades) < self._MIN_SAMPLE:
                continue
            perf = self._calc_structure_perf(trades)
            off: Dict[str, float] = {}

            # Coherence tuning
            if perf.wr < 0.40:
                off["coherence_min"] = +0.3
            elif perf.wr > 0.65 and perf.avg_r > 1.5:
                off["coherence_min"] = -0.15

            # Size cap tuning
            if perf.avg_r > 2.0:
                off["size_cap"] = +0.05
            elif perf.avg_r < 0.5:
                off["size_cap"] = -0.10

            # ATR baseline: if low-ATR entries underperform, tighten
            low_atr_trades = [t for t in trades if t.get("atr_vs_baseline", 1.0) < 0.8]
            if len(low_atr_trades) >= self._MIN_SAMPLE:
                low_perf = self._calc_structure_perf(low_atr_trades)
                if low_perf.wr < 0.40:
                    off["atr_baseline_min"] = +0.15

            if off:
                offsets[struct] = off

        return offsets

    def _structure_order_types(self, closed: List[dict]) -> Dict[str, Optional[str]]:
        """Recommend order-type overrides if slippage data shows an edge."""
        per_struct_ot: Dict[str, Dict[str, List[dict]]] = {}
        for e in closed:
            s = e.get("kant_structure", "normal") or "normal"
            ot = e.get("order_type_used", "limit") or "limit"
            per_struct_ot.setdefault(s, {}).setdefault(ot, []).append(e)

        recommendations: Dict[str, Optional[str]] = {}
        for struct, ot_map in per_struct_ot.items():
            if len(ot_map) < 2:
                continue
            # Compare win rates per order type
            ot_wr: Dict[str, float] = {}
            for ot, trades in ot_map.items():
                if len(trades) < self._MIN_SAMPLE:
                    continue
                perf = self._calc_structure_perf(trades)
                ot_wr[ot] = perf.wr

            if not ot_wr:
                continue

            best_ot = max(ot_wr, key=ot_wr.get)
            worst_ot = min(ot_wr, key=ot_wr.get)
            if ot_wr[best_ot] - ot_wr[worst_ot] > 0.20:
                recommendations[struct] = best_ot

        return recommendations

    # ── Nietzsche Kelly analysis ──────────────────────────────────────────────

    def _kelly_will_table(
        self, closed: List[dict]
    ) -> tuple[Dict[str, Dict[str, float]], Dict[str, Dict[str, str]]]:
        """Compute Kelly-optimal multipliers per (dd_band, streak_band)."""
        per_cell: Dict[str, Dict[str, List[dict]]] = {}
        for e in closed:
            dd = e.get("drawdown_band", "5-10%") or "5-10%"
            sb = e.get("streak_band", "0-2") or "0-2"
            per_cell.setdefault(dd, {}).setdefault(sb, []).append(e)

        mults: Dict[str, Dict[str, float]] = {}
        states: Dict[str, Dict[str, str]] = {}

        for dd_band, sb_map in per_cell.items():
            mults[dd_band] = {}
            states[dd_band] = {}
            for sb, trades in sb_map.items():
                if len(trades) < self._MIN_SAMPLE:
                    continue
                perf = self._calc_structure_perf(trades)
                p = perf.wr
                q = 1.0 - p
                b = (
                    abs(perf.avg_win_r / perf.avg_loss_r)
                    if perf.avg_loss_r != 0 else 1.0
                )
                kelly = (p * b - q) / b if b > 0 else 0.0
                kelly = max(0.0, min(1.5, kelly * 0.5))  # half-Kelly, cap at 1.5

                mults[dd_band][sb] = round(kelly, 3)
                state = (
                    "aggressive" if kelly > 0.50 else
                    "neutral" if kelly > 0.25 else
                    "conservative" if kelly > 0.10 else
                    "defensive"
                )
                states[dd_band][sb] = state

        return mults, states

    # ── Hold time analysis ────────────────────────────────────────────────────

    def _hold_time_analysis(self, closed: List[dict]) -> Dict[str, int]:
        """Recommend time-stop durations per edge profile."""
        by_profile: Dict[str, List[dict]] = {}
        for e in closed:
            prof = e.get("trade_edge_profile", "directional") or "directional"
            by_profile.setdefault(prof, []).append(e)

        recs: Dict[str, int] = {}
        for prof, trades in by_profile.items():
            if len(trades) < self._MIN_SAMPLE:
                continue
            winners = [t for t in trades if t.get("outcome") == "win" and t.get("hold_time_ms")]
            if len(winners) < self._MIN_SAMPLE:
                continue
            # 80th percentile hold time for winners
            hold_times = sorted(t["hold_time_ms"] for t in winners)
            idx_80 = int(len(hold_times) * 0.80)
            p80 = hold_times[idx_80]
            recs[prof] = max(300_000, int(p80))  # minimum 5 min

        return recs

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _calc_structure_perf(trades: List[dict]) -> StructurePerformance:
        """Compute aggregate stats for a list of trades."""
        wins = [t for t in trades if t.get("outcome") == "win"]
        losses = [t for t in trades if t.get("outcome") == "loss"]
        n = len(trades)

        win_r = [t.get("pnl_r", 0.0) or 0.0 for t in wins if t.get("pnl_r") is not None]
        loss_r = [t.get("pnl_r", 0.0) or 0.0 for t in losses if t.get("pnl_r") is not None]
        all_r = win_r + loss_r

        avg_r = statistics.mean(all_r) if all_r else 0.0
        avg_win_r = statistics.mean(win_r) if win_r else 0.0
        avg_loss_r = statistics.mean(loss_r) if loss_r else 0.0

        hold_times = [t.get("hold_time_ms", 0) for t in trades if t.get("hold_time_ms")]
        median_hold = statistics.median(hold_times) if hold_times else 0.0

        # Slippage analysis
        limit_slips = []
        market_slips = []
        for t in trades:
            slip = t.get("slippage_pct", 0.0) or 0.0
            ot = t.get("order_type_used", "limit")
            if ot == "limit":
                limit_slips.append(slip)
            elif ot == "market":
                market_slips.append(slip)

        return StructurePerformance(
            n_trades=n,
            wins=len(wins),
            losses=len(losses),
            wr=len(wins) / n if n > 0 else 0.0,
            avg_r=avg_r,
            avg_win_r=avg_win_r,
            avg_loss_r=avg_loss_r,
            median_hold_ms=median_hold,
            limit_slippage_pct=statistics.mean(limit_slips) if limit_slips else 0.0,
            market_slippage_pct=statistics.mean(market_slips) if market_slips else 0.0,
        )
