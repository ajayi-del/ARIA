"""
intelligence/kant_engine.py — Market Structure Interpreter.

Kant asks: "What does this signal MEAN given the structure of the market?"
It does NOT replace any risk gate — it adjusts the thresholds those gates
compare against, based on what regime the market is actually in.

Three outputs that flow into risk_engine.validate(kant_overrides=...):
  atr_baseline_min    — raise/lower ATR ratio floor
  coherence_min       — raise/lower required coherence
  basis_stress_weight — amplify/dampen basis stress penalty

One output that flows directly into the Nietzsche engine:
  size_cap  — hard ceiling on size multiplier

Hysteresis: 3-period (same as personality engine) to prevent thrashing.
Latency: O(1) dict lookups + float comparisons, ~0.05ms.
"""

from __future__ import annotations

import collections
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class MarketStructure(Enum):
    ACCUMULATION = "accumulation"
    # Low vol, coiling, pre-breakout.
    # ATR below baseline; order book building; volume declining into support.

    TREND        = "trend"
    # Directional, sustained, HTF-aligned.
    # Cascade or strong momentum active.

    DISTRIBUTION = "distribution"
    # High vol, erratic, post-peak.
    # Funding elevated, basis stressed.

    CHAOS        = "chaos"
    # Extreme: z_score > 4, basis stress ≥ 3, or RPC degraded below 0.40.


@dataclass(frozen=True)
class KantFrame:
    structure:            MarketStructure
    confidence:           float   # 0.0–1.0

    # Threshold overrides for risk_engine — only applied when confidence > 0.50
    atr_baseline_min:     float   # replaces COIL_THRESHOLD in on_signal_ready
    coherence_min:        float   # floor added on top of adaptive calibrator
    basis_stress_weight:  float   # multiplier on basis_stress_count gate

    order_type:           str     # "limit" | "market" | "probe"
    size_cap:             float   # hard ceiling applied by Nietzsche engine
    min_notional_adjust:  bool    # True = auto-bump to meet venue minimum


# Per-structure parameter tables — frozen constants, loaded once at import.
_FRAMES: dict[MarketStructure, dict] = {
    MarketStructure.ACCUMULATION: {
        "atr_baseline_min":    0.50,   # lower — don't block early coil entries
        "coherence_min":       4.0,    # probe earlier (lower floor)
        "basis_stress_weight": 0.30,   # basis less meaningful pre-breakout
        "order_type":          "limit",
        "size_cap":            0.75,   # not full conviction yet
    },
    MarketStructure.TREND: {
        "atr_baseline_min":    0.70,   # normal
        "coherence_min":       4.5,    # normal
        "basis_stress_weight": 1.00,   # normal
        "order_type":          "market",
        "size_cap":            1.25,   # expand with momentum
    },
    MarketStructure.DISTRIBUTION: {
        "atr_baseline_min":    0.90,   # higher — be selective
        "coherence_min":       5.5,    # only strong signals
        "basis_stress_weight": 2.00,   # amplify warning
        "order_type":          "limit",
        "size_cap":            0.50,   # reduce exposure
    },
    MarketStructure.CHAOS: {
        "atr_baseline_min":    1.30,   # very selective
        "coherence_min":       6.5,    # elite only
        "basis_stress_weight": 9999,   # hard block on any basis stress
        "order_type":          "probe",
        "size_cap":            0.25,   # minimal
    },
}

_CONFIDENCE_BASE: dict[MarketStructure, float] = {
    MarketStructure.ACCUMULATION: 0.65,
    MarketStructure.TREND:        0.80,
    MarketStructure.DISTRIBUTION: 0.70,
    MarketStructure.CHAOS:        0.90,
}

# 3-period hysteresis before committing to a new structure.
# Prevents thrashing between TREND and DISTRIBUTION on noisy ticks.
_HYSTERESIS = 3


class KantEngine:
    """
    Interprets market structure from existing signal fields.

    Inputs: atr_vs_baseline, cascade_phase/zscore, basis_stress_count,
            rpc_health_score (all already computed in on_signal_ready).
    Output: KantFrame — frozen threshold overrides.

    Instance is shared across all symbols — call assess() per tick.
    """

    def __init__(self, config) -> None:
        self._config = config
        self._last_frames: dict[str, Optional[KantFrame]] = {}
        self._pending:     dict[str, Optional[tuple[MarketStructure, int]]] = {}
        # Bayesian outcome tracking — per-structure circular buffer (maxlen=50)
        # on_outcome() appends 1.0/0.0; _compute_confidence() blends empirical WR
        self._outcomes: dict[MarketStructure, collections.deque] = {
            s: collections.deque(maxlen=50) for s in MarketStructure
        }

    # ── Public API ────────────────────────────────────────────────────────────

    def assess(
        self,
        symbol:             str,
        atr_vs_baseline:    float,
        cascade_phase:      str,
        cascade_zscore:     float,
        basis_stress_count: int,
        rpc_health:         float,
        regime:             str,
        liq_60s:            int,
    ) -> KantFrame:
        """
        Classify market structure for `symbol` and return threshold overrides.

        All inputs come from fields already computed on the hot path — zero
        extra I/O. Called once per signal after personality assessment.
        """
        raw    = self._detect_structure(
            atr_vs_baseline, cascade_phase, cascade_zscore,
            basis_stress_count, rpc_health, liq_60s,
        )
        stable = self._apply_hysteresis(symbol, raw)
        frame  = self._build_frame(stable, atr_vs_baseline, cascade_zscore, basis_stress_count)
        self._last_frames[symbol] = frame
        return frame

    def on_outcome(self, symbol: str, won: bool, pnl_r: float) -> None:
        """
        Bayes feedback — called after every trade close.

        Updates the per-structure outcome deque for the structure that was active
        at trade entry (last assessed frame for this symbol).  _compute_confidence()
        blends this empirical win-rate into the signal-based confidence at 20% weight
        once ≥5 outcomes are available.

        pnl_r: realized R-multiple (pnl_usd / initial_margin).  Not used for the WR
               blend but stored implicitly via the won flag.
        """
        frame = self._last_frames.get(symbol)
        if frame is None:
            return
        self._outcomes[frame.structure].append(1.0 if won else 0.0)

    @property
    def last_frame(self) -> Optional[KantFrame]:
        """Last frame emitted across all symbols (display/debug use)."""
        frames = [f for f in self._last_frames.values() if f is not None]
        return frames[-1] if frames else None

    # ── Private ───────────────────────────────────────────────────────────────

    def _detect_structure(
        self,
        atr_vs_baseline:    float,
        cascade_phase:      str,
        cascade_zscore:     float,
        basis_stress_count: int,
        rpc_health:         float,
        liq_60s:            int,
    ) -> MarketStructure:

        # CHAOS — extreme conditions first (most restrictive)
        if cascade_zscore > 4.0 and basis_stress_count >= 3:
            return MarketStructure.CHAOS
        if rpc_health < 0.40:
            return MarketStructure.CHAOS

        # TREND — directional momentum confirmed
        if cascade_phase in ("building", "expansion", "peak",
                             "PHASE_BUILDING", "PHASE_EXPANSION"):
            return MarketStructure.TREND
        if cascade_zscore > 2.0 and liq_60s > 30 and atr_vs_baseline > 1.2:
            return MarketStructure.TREND

        # DISTRIBUTION — post-peak or basis stressed
        if cascade_phase in ("exhaustion", "aftermath",
                             "PHASE_EXHAUSTION", "PHASE_AFTERMATH"):
            return MarketStructure.DISTRIBUTION
        if basis_stress_count >= 2 and atr_vs_baseline > 1.0:
            return MarketStructure.DISTRIBUTION

        # ACCUMULATION — coiling, low vol
        if atr_vs_baseline < 0.75:
            return MarketStructure.ACCUMULATION

        # Default — normal directional
        return MarketStructure.TREND

    def _apply_hysteresis(
        self, symbol: str, raw: MarketStructure
    ) -> MarketStructure:
        """3-period hysteresis per symbol — prevents thrashing."""
        last_frame = self._last_frames.get(symbol)
        if last_frame is None:
            # First tick: commit immediately, no hysteresis needed
            return raw

        current = last_frame.structure
        if raw == current:
            self._pending[symbol] = None
            return current

        pending = self._pending.get(symbol)
        if pending and pending[0] == raw:
            count = pending[1] + 1
            if count >= _HYSTERESIS:
                self._pending[symbol] = None
                return raw
            self._pending[symbol] = (raw, count)
        else:
            self._pending[symbol] = (raw, 1)

        return current  # hold previous until hysteresis satisfied

    def _build_frame(
        self,
        structure:          MarketStructure,
        atr_vs_baseline:    float,
        cascade_zscore:     float,
        basis_stress_count: int,
    ) -> KantFrame:
        params = _FRAMES[structure]
        conf   = self._compute_confidence(
            structure, atr_vs_baseline, cascade_zscore, basis_stress_count
        )
        return KantFrame(
            structure           = structure,
            confidence          = conf,
            atr_baseline_min    = params["atr_baseline_min"],
            coherence_min       = params["coherence_min"],
            basis_stress_weight = params["basis_stress_weight"],
            order_type          = params["order_type"],
            size_cap            = params["size_cap"],
            min_notional_adjust = True,
        )

    def _compute_confidence(
        self,
        structure:          MarketStructure,
        atr_vs_baseline:    float,
        cascade_zscore:     float,
        basis_stress_count: int,
    ) -> float:
        base = _CONFIDENCE_BASE[structure]
        if cascade_zscore > 3.0:
            base = min(1.0, base + 0.10)
        if basis_stress_count >= 2:
            base = max(0.0, base - 0.10)
        # Bayesian blend: once ≥5 outcomes observed for this structure, fold empirical
        # win-rate in at 20% weight.  Prevents signal-only base from drifting too far
        # from realized performance without swamping it on small samples.
        outcomes = self._outcomes.get(structure)
        if outcomes and len(outcomes) >= 5:
            empirical_wr = sum(outcomes) / len(outcomes)
            base = base * 0.80 + empirical_wr * 0.20
        return round(base, 2)
