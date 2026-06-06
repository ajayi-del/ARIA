"""
RegimeMemory — Empirical win-rate tracker per (regime, tier, asset_class).

Updated nightly from the trade journal. Falls back to static edge table
when insufficient samples exist.

This is the cybernetic feedback loop for the SignalArbiter: it learns
which tiers have edge in which regimes, and the arbiter uses that
knowledge to resolve conflicts.
"""

import json
import structlog
from pathlib import Path
from typing import Dict, Optional, Tuple
from dataclasses import dataclass, asdict

logger = structlog.get_logger(__name__)

# Minimum samples before empirical WR is trusted over static table
MIN_SAMPLES = 10

# Static fallback: tier preference per (regime, conflict_type)
# Same table as signal_arbiter.py — kept here as authoritative source.
STATIC_PREFERENCE: Dict[Tuple[str, str], str] = {
    ("transitioning", "macro_vs_micro"): "microstructure",
    ("confused",      "macro_vs_micro"): "microstructure",
    ("chop",          "macro_vs_micro"): "microstructure",
    ("risk_on",       "macro_vs_micro"): "regime",
    ("risk_off",      "macro_vs_micro"): "regime",
    ("alt_season",    "macro_vs_micro"): "microstructure",
    ("btc_dominance", "macro_vs_micro"): "regime",
    ("transitioning", "sweep_vs_macro"):  "microstructure",
    ("risk_on",       "sweep_vs_macro"):  "microstructure",
    ("risk_off",      "sweep_vs_macro"):  "microstructure",
    ("transitioning", "funding_vs_macro"): "funding",
    ("risk_on",       "funding_vs_macro"): "regime",
    ("risk_off",      "funding_vs_macro"): "regime",
}


@dataclass
class RegimeTierStats:
    """Empirical stats for a single (regime, tier, asset_class) bucket."""
    wins: int = 0
    losses: int = 0
    pnl_total: float = 0.0
    avg_hold_min: float = 0.0
    sample_count: int = 0

    @property
    def win_rate(self) -> float:
        if self.sample_count == 0:
            return 0.0
        return self.wins / self.sample_count

    def to_dict(self) -> dict:
        return {
            "wins": self.wins,
            "losses": self.losses,
            "pnl_total": round(self.pnl_total, 4),
            "avg_hold_min": round(self.avg_hold_min, 4),
            "sample_count": self.sample_count,
            "win_rate": round(self.win_rate, 4),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RegimeTierStats":
        return cls(
            wins=d.get("wins", 0),
            losses=d.get("losses", 0),
            pnl_total=d.get("pnl_total", 0.0),
            avg_hold_min=d.get("avg_hold_min", 0.0),
            sample_count=d.get("sample_count", 0),
        )


class RegimeMemory:
    """
    Persistent memory of tier performance per regime.

    Key: (regime, tier, asset_class) -> RegimeTierStats
    """

    def __init__(self, state_path: Optional[Path] = None):
        self._stats: Dict[Tuple[str, str, str], RegimeTierStats] = {}
        self._state_path = state_path or Path("data/regime_memory.json")
        self._load()

    # ──────────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────────

    def get_preferred_tier(self, regime: str, conflict_type: str) -> Optional[str]:
        """
        Return the tier with highest empirical WR for this regime.
        Returns None if insufficient samples to override static table.
        """
        # Map conflict_type to the tiers involved
        tier_candidates = self._conflict_tiers(conflict_type)
        if not tier_candidates:
            return None

        best_tier = None
        best_wr = 0.0

        for tier in tier_candidates:
            # Aggregate across asset classes for robustness
            _combined = self._aggregate_across_assets(regime, tier)
            if _combined.sample_count >= MIN_SAMPLES and _combined.win_rate > best_wr:
                best_wr = _combined.win_rate
                best_tier = tier

        return best_tier

    def record_trade(
        self,
        regime: str,
        dominant_tier: str,
        asset_class: str,
        pnl: float,
        hold_min: float,
    ) -> None:
        """Record a closed trade outcome for learning."""
        key = (regime, dominant_tier, asset_class)
        stat = self._stats.setdefault(key, RegimeTierStats())
        stat.sample_count += 1
        stat.pnl_total += pnl
        if pnl > 0:
            stat.wins += 1
        else:
            stat.losses += 1
        # Rolling average hold time
        stat.avg_hold_min = (
            (stat.avg_hold_min * (stat.sample_count - 1) + hold_min)
            / stat.sample_count
        )
        logger.debug("regime_memory_recorded",
                     regime=regime, tier=dominant_tier, asset_class=asset_class,
                     pnl=round(pnl, 2), wr=round(stat.win_rate, 3))

    def get_stats(self, regime: str, tier: str, asset_class: str) -> RegimeTierStats:
        """Return stats for a specific bucket."""
        return self._stats.get((regime, tier, asset_class), RegimeTierStats())

    def save(self) -> None:
        """Persist to disk."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            _out = {
                f"{k[0]}|{k[1]}|{k[2]}": v.to_dict()
                for k, v in self._stats.items()
            }
            with open(self._state_path, "w") as f:
                json.dump(_out, f, indent=2)
            logger.info("regime_memory_saved", path=str(self._state_path), buckets=len(_out))
        except Exception as e:
            logger.warning("regime_memory_save_failed", error=str(e))

    # ──────────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._state_path.exists():
            return
        try:
            with open(self._state_path, "r") as f:
                _raw = json.load(f)
            for _key, _val in _raw.items():
                _parts = _key.split("|")
                if len(_parts) == 3:
                    self._stats[(_parts[0], _parts[1], _parts[2])] = RegimeTierStats.from_dict(_val)
            logger.info("regime_memory_loaded", path=str(self._state_path), buckets=len(self._stats))
        except Exception as e:
            logger.warning("regime_memory_load_failed", error=str(e))

    @staticmethod
    def _conflict_tiers(conflict_type: str) -> list:
        """Map conflict type to the tiers that compete in it."""
        _map = {
            "macro_vs_micro": ["regime", "microstructure", "institutional"],
            "sweep_vs_macro": ["microstructure", "regime", "macro"],
            "funding_vs_macro": ["funding", "regime", "institutional"],
            "oi_vs_macro": ["oi_momentum", "regime", "institutional"],
        }
        return _map.get(conflict_type, [])

    def _aggregate_across_assets(self, regime: str, tier: str) -> RegimeTierStats:
        """Aggregate stats for a (regime, tier) pair across all asset classes."""
        _combined = RegimeTierStats()
        for (r, t, a), stat in self._stats.items():
            if r == regime and t == tier:
                _combined.wins += stat.wins
                _combined.losses += stat.losses
                _combined.pnl_total += stat.pnl_total
                _combined.sample_count += stat.sample_count
        return _combined
