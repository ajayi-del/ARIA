"""Drawdown brain — the internal $150 floor.

The venue floor is $300 (6% static, equity-based). Ours is $150 — half.
headroom = equity - floor_line. Band ladder re-based from the founding
architecture doc to the $150 floor, merged with the quant's hard-zero
(headroom <= $30 -> no new risk):

  ACTIVE     used <  $50   mult 1.00
  CAUTIOUS   $50-75        mult 0.60
  DEFENSIVE  $75-100       mult 0.35   (A+ signals only — enforced upstream)
  SURVIVAL   $100-120      mult 0.15
  LOCKED     $120-150      mult 0.00   (headroom <= $30)
  BREACHED   equity <= floor_line
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..io.journal import atomic_write_json, read_json

BANDS: tuple[tuple[str, float, float, float], ...] = (
    ("ACTIVE", 0.0, 50.0, 1.0),
    ("CAUTIOUS", 50.0, 75.0, 0.6),
    ("DEFENSIVE", 75.0, 100.0, 0.35),
    ("SURVIVAL", 100.0, 120.0, 0.15),
    ("LOCKED", 120.0, 150.0, 0.0),
)


@dataclass
class DrawdownState:
    equity: float
    headroom: float
    used: float
    band: str
    breached: bool
    ts: float


class DrawdownBrain:
    def __init__(self, register_path: Path, floor_line: float, floor_usd: float = 150.0):
        self.path = register_path
        self.floor_line = floor_line
        self.floor_usd = floor_usd
        self.state = self._load()

    def _load(self) -> DrawdownState | None:
        raw = read_json(self.path)
        if raw:
            return DrawdownState(**{k: raw[k] for k in
                                    ("equity", "headroom", "used", "band", "breached", "ts")})
        return None

    @staticmethod
    def band_for(used: float) -> tuple[str, float]:
        for name, lo, hi, mult in BANDS:
            if lo <= used < hi:
                return name, mult
        return "LOCKED", 0.0

    def update(self, equity: float) -> tuple[DrawdownState, str | None]:
        """Returns (state, transition) — transition is 'OLD->NEW' or None."""
        headroom = equity - self.floor_line
        used = self.floor_usd - headroom
        breached = equity <= self.floor_line
        band, _ = self.band_for(used)
        if breached:
            band = "BREACHED"
        prev = self.state.band if self.state else None
        self.state = DrawdownState(
            equity=equity, headroom=headroom, used=used,
            band=band, breached=breached, ts=time.time(),
        )
        atomic_write_json(self.path, self.state.__dict__)
        transition = f"{prev}->{band}" if prev and prev != band else None
        return self.state, transition

    @property
    def mult(self) -> float:
        if not self.state:
            return 0.0  # ABSTAIN until first equity poll
        if self.state.breached:
            return 0.0
        _, mult = self.band_for(self.state.used)
        return mult

    @property
    def headroom(self) -> float:
        return self.state.headroom if self.state else 0.0

    @property
    def snapshot(self) -> dict[str, Any]:
        return dict(self.state.__dict__) if self.state else {}
