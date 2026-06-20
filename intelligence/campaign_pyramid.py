"""
intelligence/campaign_pyramid.py — Campaign Pyramid Engine (SpaceX Tournament)

MFE-based anti-martingale pyramid for high-volatility campaign symbols.
Unlike the standard pyramid (which requires TP1 hit + coherence >= 8.0),
the campaign pyramid layers in based on proven price action:

  Layer 1 (25%): MFE > 0.5% in favor
  Layer 2 (35%): MFE > 1.0% in favor
  Layer 3 (40%): MFE > 1.5% in favor

Each layer must pass day-type and volatility gates:
  - chop day → no pyramid
  - atr_vs_baseline > 1.5 → no pyramid (too volatile)
  - min 3 min between layers

Stops are asymmetric:
  L1: 0.6% buffer (wider than normal — tournament noise)
  L2/L3: breakeven + 0.2% noise buffer on combined position

Philosophy: The scout proves the terrain. The general commits the battalion
only after the scout reports back alive.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import structlog

log = structlog.get_logger(__name__)


@dataclass
class CampaignPyramidState:
    """Per-symbol pyramid state."""
    symbol: str
    layers: int = 0           # 0 = base only, 1 = base+L1, 2 = base+L1+L2
    base_size: float = 0.0
    base_entry: float = 0.0
    last_layer_ms: int = 0    # timestamp of most recent layer add
    total_size: float = 0.0   # cumulative size across all layers


class CampaignPyramidEngine:
    """
    Tournament-mode pyramid engine.
    Thread-safe: all state is per-symbol dict lookups.
    """

    # MFE confirmation thresholds per layer (as fraction of entry price)
    _MFE_THRESHOLDS = [0.005, 0.010, 0.015]   # 0.5%, 1.0%, 1.5%

    # Size fractions per layer (anti-martingale)
    _SIZE_FRACTIONS = [0.25, 0.35, 0.40]

    def __init__(self, config=None) -> None:
        self._config = config
        self._states: Dict[str, CampaignPyramidState] = {}

    # ── Public API ────────────────────────────────────────────────────────────

    def register_base(self, symbol: str, base_size: float, base_entry: float) -> None:
        """Call when the base campaign position is opened."""
        self._states[symbol] = CampaignPyramidState(
            symbol=symbol,
            layers=0,
            base_size=base_size,
            base_entry=base_entry,
            last_layer_ms=int(time.time() * 1000),
            total_size=base_size,
        )
        log.info("campaign_pyramid_base_registered",
                 symbol=symbol, base_size=round(base_size, 6),
                 entry=round(base_entry, 4))

    def can_add_layer(
        self,
        symbol: str,
        mark_price: float,
        day_type: str,
        atr_ratio: float,
    ) -> Tuple[bool, str, int]:
        """
        Check if a new pyramid layer can be added.
        Returns (allowed, reason, next_layer_index).
        """
        state = self._states.get(symbol)
        if state is None:
            return False, "no_base_position", 0

        max_layers = getattr(self._config, "campaign_pyramid_max_layers", 3)
        if state.layers >= max_layers - 1:
            return False, "max_layers_reached", state.layers

        # Day-type gate
        if day_type == "chop":
            return False, "chop_day_no_pyramid", state.layers

        # Volatility gate
        _vol_cap = getattr(self._config, "campaign_pyramid_volatility_cap", 1.5)
        if atr_ratio > _vol_cap:
            return False, f"atr_ratio_too_high_{atr_ratio:.2f}", state.layers

        # Min time between layers (3 min)
        _min_gap_ms = getattr(self._config, "campaign_pyramid_min_layer_gap_s", 180) * 1000
        _elapsed = int(time.time() * 1000) - state.last_layer_ms
        if _elapsed < _min_gap_ms:
            return False, f"layer_gap_too_short_{_elapsed // 1000}s", state.layers

        # MFE confirmation
        _mfe = self._compute_mfe(state, mark_price)
        _next_layer = state.layers
        _threshold = self._MFE_THRESHOLDS[_next_layer]
        if _mfe < _threshold:
            return False, f"mfe_below_threshold_{_mfe:.4f}_needs_{_threshold:.4f}", state.layers

        return True, "", _next_layer

    def compute_layer_size(self, symbol: str, coherence: float) -> float:
        """Return the size for the next pyramid layer."""
        state = self._states.get(symbol)
        if state is None:
            return 0.0

        layer_idx = state.layers
        if layer_idx >= len(self._SIZE_FRACTIONS):
            return 0.0

        frac = self._SIZE_FRACTIONS[layer_idx]
        # Coherence boost: 5.0 → 1.0, 8.0 → 1.24, 10.0 → 1.4
        _coh_mult = min(1.4, 1.0 + max(0.0, coherence - 5.0) / 12.5)
        return state.base_size * frac * _coh_mult

    def get_stop_price(
        self,
        symbol: str,
        layer_idx: int,
        entry_price: float,
    ) -> Optional[float]:
        """
        Compute stop price for the new layer.
        L1: entry - 0.6% (long) / entry + 0.6% (short)
        L2/L3: breakeven of combined position + 0.2% buffer
        """
        state = self._states.get(symbol)
        if state is None:
            return None

        _side = "long" if entry_price >= state.base_entry else "short"

        if layer_idx == 0:
            # L1 gets a wide stop — 0.6% buffer
            _buffer = getattr(self._config, "campaign_pyramid_l1_stop_buffer", 0.006)
            if _side == "long":
                return entry_price * (1.0 - _buffer)
            return entry_price * (1.0 + _buffer)

        # L2/L3: breakeven of combined position + 0.2% buffer
        _comb_sz = state.total_size
        _new_sz = self.compute_layer_size(symbol, 0.0)
        _total = _comb_sz + _new_sz
        if _total <= 0:
            return None

        if _side == "long":
            _breakeven = (
                state.base_entry * state.base_size
                + entry_price * _new_sz
            ) / _total
            _buffer = getattr(self._config, "campaign_pyramid_breakeven_buffer", 0.002)
            return _breakeven * (1.0 - _buffer)
        else:
            _breakeven = (
                state.base_entry * state.base_size
                + entry_price * _new_sz
            ) / _total
            _buffer = getattr(self._config, "campaign_pyramid_breakeven_buffer", 0.002)
            return _breakeven * (1.0 + _buffer)

    def record_layer(self, symbol: str, layer_size: float, layer_entry: float) -> None:
        """Call after a pyramid layer order is filled."""
        state = self._states.get(symbol)
        if state is None:
            return
        state.layers += 1
        state.last_layer_ms = int(time.time() * 1000)
        state.total_size += layer_size
        log.info("campaign_pyramid_layer_added",
                 symbol=symbol,
                 layer=state.layers,
                 layer_size=round(layer_size, 6),
                 total_size=round(state.total_size, 6),
                 entry=round(layer_entry, 4))

    def reset(self, symbol: str) -> None:
        """Call when position is closed."""
        if symbol in self._states:
            del self._states[symbol]
            log.info("campaign_pyramid_reset", symbol=symbol)

    def get_state(self, symbol: str) -> Optional[CampaignPyramidState]:
        return self._states.get(symbol)

    def is_active(self, symbol: str) -> bool:
        return symbol in self._states

    # ── Internal ──────────────────────────────────────────────────────────────

    @staticmethod
    def _compute_mfe(state: CampaignPyramidState, mark_price: float) -> float:
        """Max favorable excursion as fraction of base entry."""
        if state.base_entry <= 0 or mark_price <= 0:
            return 0.0
        # We don't track MFE history — use current mark vs entry
        if mark_price > state.base_entry:
            return (mark_price - state.base_entry) / state.base_entry
        return (state.base_entry - mark_price) / state.base_entry
