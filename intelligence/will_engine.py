"""
intelligence/will_engine.py — Kant x Nietzsche x World = Will Probability.

The Will Engine is the synthesis layer of the AI Fund Manager.
It does not predict prices. It answers: "Given what Kant knows about structure,
what Nietzsche knows about our psychological state, and what the World Model
knows about the environment — how strongly do we want to act, and at what scale?"

Output is a WillVerdict. The engine writes the verdict to ParamStore
for audit and downstream consumption, but the primary consumer is
the execution pipeline which applies size_scale directly to the candidate.

Hard rule: will_probability = 0.0 is a full veto (DORMANT, BLOCK, or
extreme adverse conditions). The pipeline may still override for cascade
personalities (APEX/AFTERMATH) — that logic lives in main.py, not here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Optional

import structlog

if TYPE_CHECKING:
    from intelligence.kant_engine import KantFrame
    from intelligence.nietzsche_engine import NietzscheOutput
    from intelligence.world_model import WorldState
    from memory.param_store import ParamStore

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class WillVerdict:
    """Immutable decision from the Will Engine."""
    will_probability: float      # 0.0–1.0  (0.0 = veto)
    size_scale: float            # 0.0–2.0  (applied to candidate.size)
    confidence_override: Optional[float] = None   # raised coherence floor
    asset_class_boost: Dict[str, float] = None    # per-class multipliers
    order_type_override: Optional[str] = None     # "limit" | "market" | "probe"
    reason: str = ""


class WillEngine:
    """
    Synthesis engine: structure + psychology + environment = will.

    Pure function except for optional ParamStore audit logging.
    Thread-safe: no mutable state.
    """

    # Normalization: Nietzsche max size_multiplier is 1.50 (AGGRESSIVE at 0-1% dd, 6+ streak)
    _NIETZSCHE_MAX_MULT = 1.50

    def __init__(self, param_store: Optional["ParamStore"] = None) -> None:
        self._param_store = param_store

    def compute(
        self,
        kant_frame: "KantFrame",
        nietzsche_output: "NietzscheOutput",
        world_state: "WorldState",
        signal_asset_class: str = "crypto",
        signal_coherence: float = 0.0,
    ) -> WillVerdict:
        """
        Compute will probability and size scale from the philosophical stack.

        Args:
            kant_frame: Market structure frame (size_cap, order_type)
            nietzsche_output: Psychological sizing output
            world_state: Environmental classification
            signal_asset_class: "crypto" | "equity" | "commodity"
            signal_coherence: raw coherence score of the signal
        """
        # ── 1. Base will probability ────────────────────────────────────────
        # Normalize Nietzsche size_multiplier to 0-1 range
        _n_mult = nietzsche_output.size_multiplier
        _base_prob = min(1.0, _n_mult / self._NIETZSCHE_MAX_MULT)

        # Modulate by world risk appetite
        # If world says 0 risk, prob is halved; if world says full risk, prob is boosted
        _base_prob *= (0.5 + 0.5 * world_state.risk_appetite)

        # Hard vetos ────────────────────────────────────────────────────────
        if nietzsche_output.will_state.value == "dormant":
            _base_prob = 0.0
        if world_state.time_quality <= 0.0:
            _base_prob = 0.0

        # Volatility discount ───────────────────────────────────────────────
        if world_state.volatility_regime == "extreme":
            _base_prob *= 0.60
        elif world_state.volatility_regime == "elevated":
            _base_prob *= 0.80

        # Time quality discount ─────────────────────────────────────────────
        if world_state.time_quality < 0.3:
            _base_prob *= 0.50
        elif world_state.time_quality < 0.6:
            _base_prob *= 0.85

        # Asset class alignment boost ───────────────────────────────────────
        _aligned = signal_asset_class == world_state.preferred_asset_class
        _mixed_pref = world_state.preferred_asset_class == "mixed"
        if _aligned or _mixed_pref:
            _base_prob *= 1.15
        else:
            _base_prob *= 0.80

        will_prob = round(max(0.0, min(1.0, _base_prob)), 4)

        # ── 2. Size scale ───────────────────────────────────────────────────
        _size = _n_mult

        # World risk appetite modulates size directly
        _size *= world_state.risk_appetite

        # Volatility scaling
        if world_state.volatility_regime == "extreme":
            _size *= 0.60
        elif world_state.volatility_regime == "elevated":
            _size *= 0.85
        elif world_state.volatility_regime == "low":
            _size *= 1.10  # low vol = size up

        # Asset class preference scaling
        if _aligned:
            _size *= 1.10
        elif not _mixed_pref:
            _size *= 0.80

        # Correlation regime scaling
        if world_state.correlation_regime == "convergent":
            _size *= 1.05  # aligned markets = more confidence
        elif world_state.correlation_regime == "divergent":
            _size *= 0.90  # divergent markets = less confidence

        # Kant hard cap
        _size = min(_size, kant_frame.size_cap)
        _size = max(0.10, _size)  # absolute floor

        size_scale = round(_size, 4)

        # ── 3. Confidence override ──────────────────────────────────────────
        _conf_override = None
        if world_state.risk_appetite < 0.3:
            _conf_override = 4.5  # raise floor when defensive
        elif world_state.volatility_regime == "extreme":
            _conf_override = 4.0
        elif world_state.risk_appetite > 0.8 and signal_coherence >= 6.0:
            _conf_override = 2.5  # lower floor for aggressive high-conviction

        # ── 4. Order type override ──────────────────────────────────────────
        _order_override = None
        if world_state.volatility_regime == "extreme" and kant_frame.order_type == "limit":
            _order_override = "market"  # need speed in extreme vol
        elif world_state.liquidity_regime == "thin" and kant_frame.order_type == "market":
            _order_override = "limit"  # avoid slippage in thin books

        # ── 5. Asset class boost map ────────────────────────────────────────
        _boosts: Dict[str, float] = {}
        if world_state.preferred_asset_class == "mixed":
            _boosts = {"crypto": 1.0, "equity": 1.0, "commodity": 1.0}
        else:
            _boosts = {
                "crypto": 1.10 if world_state.preferred_asset_class == "crypto" else 0.90,
                "equity": 1.10 if world_state.preferred_asset_class == "equity" else 0.90,
                "commodity": 1.10 if world_state.preferred_asset_class == "commodity" else 0.90,
            }

        # ── 6. Narrative reason ─────────────────────────────────────────────
        _reason_parts = [
            f"will={will_prob:.2f}",
            f"size={size_scale:.2f}",
            f"world={world_state.preferred_asset_class}",
            f"vol={world_state.volatility_regime}",
            f"risk={world_state.risk_appetite:.2f}",
            f"kant_cap={kant_frame.size_cap:.2f}",
            f"nietzsche={nietzsche_output.will_state.value}",
        ]
        if _conf_override:
            _reason_parts.append(f"coh_override={_conf_override:.1f}")
        if _order_override:
            _reason_parts.append(f"order={_order_override}")

        reason = " | ".join(_reason_parts)

        verdict = WillVerdict(
            will_probability=will_prob,
            size_scale=size_scale,
            confidence_override=_conf_override,
            asset_class_boost=_boosts,
            order_type_override=_order_override,
            reason=reason,
        )

        # ── 7. Audit to ParamStore ──────────────────────────────────────────
        self._audit(verdict, signal_asset_class)

        return verdict

    def _audit(self, verdict: WillVerdict, asset_class: str) -> None:
        """Write key verdict fields to ParamStore for audit and downstream use."""
        if self._param_store is None:
            return
        try:
            self._param_store.set_ai_param(
                "will_probability", verdict.will_probability, ttl_seconds=3600
            )
            self._param_store.set_ai_param(
                "will_size_scale", verdict.size_scale, ttl_seconds=3600
            )
            self._param_store.set_ai_param(
                "will_asset_class_boost",
                verdict.asset_class_boost.get(asset_class, 1.0),
                ttl_seconds=3600,
            )
            if verdict.confidence_override is not None:
                self._param_store.set_ai_param(
                    "will_confidence_override", verdict.confidence_override, ttl_seconds=3600
                )
        except Exception as e:
            logger.debug("will_audit_failed", error=str(e))
