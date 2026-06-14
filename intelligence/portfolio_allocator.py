"""
intelligence/portfolio_allocator.py — Asset-class concentration guard.

Prevents the system from accumulating lopsided exposure when WorldModel
signals a preference for a different asset class.  Evaluated per-candidate
before bracket placement.
"""

from dataclasses import dataclass
from typing import Dict, List, Optional
import structlog

logger = structlog.get_logger(__name__)


# Broader asset classes used for portfolio-level allocation.
_ASSET_CLASS_MAP = {
    "equity": "equity",
    "commodity": "commodity",
    # Everything else collapses to crypto.
}


def _symbol_asset_class(symbol: str, config) -> str:
    """Map a symbol to its broad asset class using ASSET_CONFIG."""
    _cfg = getattr(config, "ASSET_CONFIG", {})
    _cat = _cfg.get(symbol, {}).get("category", "crypto")
    return _ASSET_CLASS_MAP.get(_cat, "crypto")


class PortfolioAllocator:
    """
    Compute target vs actual asset-class weights and gate candidates
    that would breach concentration limits.
    """

    # Tolerance band around target weight (absolute, e.g. 0.05 = 5%)
    TOLERANCE = 0.05

    # Default target mix when WorldState says "mixed"
    _DEFAULT_WEIGHTS = {
        "crypto": 0.40,
        "equity": 0.35,
        "commodity": 0.25,
    }

    # Preferred-asset-class driven mixes
    _PREFERRED_MAP = {
        "crypto":    {"crypto": 0.70, "equity": 0.20, "commodity": 0.10},
        "equity":    {"crypto": 0.20, "equity": 0.70, "commodity": 0.10},
        "commodity": {"crypto": 0.25, "equity": 0.15, "commodity": 0.60},
        "mixed":     _DEFAULT_WEIGHTS,
    }

    @classmethod
    def target_weights(cls, world_state) -> Dict[str, float]:
        """Return target weight dict from WorldState."""
        _pref = getattr(world_state, "preferred_asset_class", "mixed")
        _risk = getattr(world_state, "risk_appetite", 0.5)
        _base = cls._PREFERRED_MAP.get(_pref, cls._DEFAULT_WEIGHTS).copy()

        # Risk appetite scales the *entire* mix down when defensive,
        # but never below 50% of the nominal targets so the allocator
        # doesn't completely shut off diversification.
        if _risk < 0.3:
            _scale = 0.50 + (_risk / 0.30) * 0.50   # 0.0 risk -> 0.50x, 0.3 risk -> 1.0x
        else:
            _scale = 1.0

        for k in _base:
            _base[k] = round(_base[k] * _scale, 3)
        return _base

    @classmethod
    def current_weights(
        cls,
        positions: List,
        balance: float,
        config,
    ) -> Dict[str, float]:
        """
        Compute actual asset-class weights from open positions.
        positions: list of Position-like objects (symbol, size, entry_price).
        """
        if not positions or balance <= 0:
            return {"crypto": 0.0, "equity": 0.0, "commodity": 0.0}

        _notional_by_class: Dict[str, float] = {"crypto": 0.0, "equity": 0.0, "commodity": 0.0}
        for pos in positions:
            _sym = getattr(pos, "symbol", "")
            _size = getattr(pos, "size", 0.0)
            _price = getattr(pos, "entry_price", 0.0)
            if not _sym or _size <= 0 or _price <= 0:
                continue
            _notional = _size * _price
            _aclass = _symbol_asset_class(_sym, config)
            _notional_by_class[_aclass] = _notional_by_class.get(_aclass, 0.0) + _notional

        return {
            k: round(v / balance, 4)
            for k, v in _notional_by_class.items()
        }

    @classmethod
    def check_candidate(
        cls,
        candidate,
        positions: List,
        balance: float,
        world_state,
        config,
    ):
        """
        Returns (allowed: bool, reason: str, adjusted_candidate or None).

        If the candidate would push its asset class beyond target + TOLERANCE,
        the candidate is rejected (allowed=False).
        """
        _sym = getattr(candidate, "symbol", "")
        _size = getattr(candidate, "size", 0.0)
        _entry = getattr(candidate, "entry_price", 0.0)
        if not _sym or _size <= 0 or _entry <= 0 or balance <= 0:
            return True, "invalid_candidate_skipped", candidate

        _aclass = _symbol_asset_class(_sym, config)
        _targets = cls.target_weights(world_state)
        _current = cls.current_weights(positions, balance, config)

        _candidate_notional = _size * _entry
        _candidate_weight = _candidate_notional / balance
        _new_weight = _current.get(_aclass, 0.0) + _candidate_weight
        _target = _targets.get(_aclass, cls._DEFAULT_WEIGHTS.get(_aclass, 0.33))
        _ceiling = _target + cls.TOLERANCE

        if _new_weight > _ceiling:
            logger.info(
                "portfolio_allocator_reject",
                symbol=_sym,
                asset_class=_aclass,
                current_weight=round(_current.get(_aclass, 0.0), 4),
                candidate_weight=round(_candidate_weight, 4),
                new_weight=round(_new_weight, 4),
                target=round(_target, 4),
                ceiling=round(_ceiling, 4),
                reason="concentration_limit",
            )
            return False, f"{_aclass}_weight_would_exceed_{round(_ceiling, 4)}", None

        return True, "ok", candidate
