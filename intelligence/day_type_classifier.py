"""
intelligence/day_type_classifier.py — Opening Range Breakout (ORB) Day Type Classifier

Classifies the trading day BEFORE the first trade using the first 30 minutes
of candles (14:30–15:00 UTC for US equities; 00:00–00:30 for crypto).

Outputs:
  trend   → OR > 1.5× ATR(20); momentum_cont bias, wider TPs
  range   → OR 0.7–1.5× ATR(20); normal personality selection
  chop    → OR < 0.7× ATR(20) OR price oscillating inside OR; scalp/mean-rev bias

Published to PersonalityContextCache so personality selection can pre-filter
before the first trade of the day.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

import structlog

log = structlog.get_logger(__name__)


class DayType(Enum):
    TREND = "trend"
    RANGE = "range"
    CHOP  = "chop"
    UNKNOWN = "unknown"


@dataclass
class DayTypeState:
    day_type: DayType = DayType.UNKNOWN
    or_high: float = 0.0
    or_low: float = 0.0
    or_range: float = 0.0
    atr20: float = 0.0
    ratio: float = 0.0
    volume_ratio: float = 0.0
    breakout_direction: str = ""  # "up" | "down" | ""
    classified_at_ms: int = 0


class DayTypeClassifier:
    """
    ORB-based day classifier.

    US equities: 14:30–15:00 UTC opening range (30 min).
    Crypto:      00:00–00:30 UTC (first 30 min of calendar day).

    Usage (main.py background loop, 60s cadence):
        classifier = DayTypeClassifier(config)
        classifier.update_candles(symbol, candles_1m)
        if classifier.is_ready(symbol):
            day_type = classifier.get_day_type(symbol)
            personality_context_cache.update_day_type(symbol, day_type.value)
    """

    def __init__(self, config=None) -> None:
        self._config = config
        # symbol → list of (timestamp_ms, open, high, low, close, volume)
        self._candles: Dict[str, List[tuple]] = {}
        # symbol → cached DayTypeState
        self._state: Dict[str, DayTypeState] = {}
        # symbol → prior day 30-min volume (for institutional participation check)
        self._prior_day_volume: Dict[str, float] = {}

    def ingest(self, symbol: str, candle: dict) -> None:
        """Ingest a single 1m candle (event-driven from CANDLE_CLOSED)."""
        try:
            _parsed = (
                int(candle.get("timestamp_ms", candle.get("t", 0))),
                float(candle.get("open", candle.get("o", 0))),
                float(candle.get("high", candle.get("h", 0))),
                float(candle.get("low", candle.get("l", 0))),
                float(candle.get("close", candle.get("c", 0))),
                float(candle.get("volume", candle.get("v", 0))),
            )
        except (TypeError, ValueError):
            return
        if symbol not in self._candles:
            self._candles[symbol] = []
        self._candles[symbol].append(_parsed)
        # Trim to max 30 candles to keep memory bounded
        if len(self._candles[symbol]) > 30:
            self._candles[symbol] = self._candles[symbol][-30:]
        self._classify(symbol)

    def update_candles(self, symbol: str, candles: List[dict]) -> None:
        """
        Ingest 1m candles. Each candle dict must have:
          timestamp_ms, open, high, low, close, volume
        """
        if not candles:
            return
        _parsed = []
        for c in candles:
            try:
                _parsed.append((
                    int(c.get("timestamp_ms", c.get("t", 0))),
                    float(c.get("open", c.get("o", 0))),
                    float(c.get("high", c.get("h", 0))),
                    float(c.get("low", c.get("l", 0))),
                    float(c.get("close", c.get("c", 0))),
                    float(c.get("volume", c.get("v", 0))),
                ))
            except (TypeError, ValueError):
                continue
        if _parsed:
            self._candles[symbol] = _parsed
            self._classify(symbol)

    def _classify(self, symbol: str) -> None:
        """Run ORB classification if enough candles are available."""
        _candles = self._candles.get(symbol, [])
        if len(_candles) < 15:
            return  # need at least 15 min of data

        # Opening range = first 15 min (or first 15 candles)
        _or_candles = _candles[:15]
        _or_high = max(c[2] for c in _or_candles)
        _or_low = min(c[3] for c in _or_candles)
        _or_range = _or_high - _or_low
        if _or_range <= 0:
            return

        # ATR(20) using last 20 candles (or fewer if not available)
        _atr_window = min(20, len(_candles))
        _atr = self._compute_atr(_candles[-_atr_window:])
        if _atr <= 0:
            return

        _ratio = _or_range / _atr

        # Volume check: compare first 30-min volume to prior day
        _vol_30 = sum(c[5] for c in _candles[:30]) if len(_candles) >= 30 else sum(c[5] for c in _candles)
        _prior_vol = self._prior_day_volume.get(symbol, 0.0)
        _vol_ratio = _vol_30 / _prior_vol if _prior_vol > 0 else 1.0

        # Directional breakout check using first 30 min (or all available)
        _check_candles = _candles[:30] if len(_candles) >= 30 else _candles
        _breakout_dir = ""
        if _check_candles:
            _last_close = _check_candles[-1][4]
            if _last_close > _or_high:
                _breakout_dir = "up"
            elif _last_close < _or_low:
                _breakout_dir = "down"

        # Classification rules
        if _ratio > 1.5:
            _day_type = DayType.TREND
        elif _ratio < 0.7:
            _day_type = DayType.CHOP
        else:
            # 0.7–1.5×: use breakout direction + volume to decide
            if _breakout_dir and _vol_ratio > 1.5:
                _day_type = DayType.TREND
            elif not _breakout_dir:
                _day_type = DayType.RANGE
            else:
                _day_type = DayType.RANGE

        self._state[symbol] = DayTypeState(
            day_type=_day_type,
            or_high=_or_high,
            or_low=_or_low,
            or_range=_or_range,
            atr20=_atr,
            ratio=_ratio,
            volume_ratio=_vol_ratio,
            breakout_direction=_breakout_dir,
            classified_at_ms=int(time.time() * 1000),
        )

        log.info("day_type_classified",
                 symbol=symbol,
                 day_type=_day_type.value,
                 or_range=round(_or_range, 4),
                 atr20=round(_atr, 4),
                ratio=round(_ratio, 3),
                 vol_ratio=round(_vol_ratio, 2),
                 breakout=_breakout_dir)

    def _compute_atr(self, candles: List[tuple]) -> float:
        """Simple ATR over given candles."""
        if len(candles) < 2:
            return 0.0
        _tr_sum = 0.0
        for i in range(1, len(candles)):
            _prev_close = candles[i - 1][4]
            _high = candles[i][2]
            _low = candles[i][3]
            _tr = max(_high - _low, abs(_high - _prev_close), abs(_low - _prev_close))
            _tr_sum += _tr
        return _tr_sum / (len(candles) - 1)

    def is_ready(self, symbol: str) -> bool:
        return symbol in self._state and self._state[symbol].day_type != DayType.UNKNOWN

    def get_day_type(self, symbol: str) -> DayType:
        return self._state.get(symbol, DayTypeState()).day_type

    def get_state(self, symbol: str) -> DayTypeState:
        return self._state.get(symbol, DayTypeState())

    def set_prior_day_volume(self, symbol: str, volume: float) -> None:
        """Call at day rollover with prior day's first 30-min volume."""
        self._prior_day_volume[symbol] = max(0.0, volume)

    def reset(self, symbol: str) -> None:
        """Reset at day rollover (e.g., 00:00 UTC crypto, 14:30 UTC equities)."""
        self._candles.pop(symbol, None)
        self._state.pop(symbol, None)
