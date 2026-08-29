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


def trend_direction_guard(day_type: str, breakout_direction: str,
                          change_24h: Optional[float], signal_direction: str,
                          momentum_threshold: float = 5.0,
                          day_move_pct: Optional[float] = None,
                          day_move_threshold: Optional[float] = None) -> str:
    """Verdict for a signal against the day's trend: 'aligned' | 'counter' | 'unknown'.

    2026-08-20 (operator directive): day_type=trend fired all through the
    08-17→19 rally while mean-reversion shorts kept entering into +8-20%
    moves — trend direction reached exits (TP room) but never entries.

    Direction evidence: ORB breakout direction and/or strong 24h momentum
    (|change| > threshold) and/or the intraday move from 00:00 UTC
    (day_move_pct — added 2026-08-20 after the 08-20 autopsy: BTC was +3.7%
    from midnight by 08:00 UTC while breakout read "" and the 24h window still
    <5% — both legacy sources blind on exactly the symbols that bled).
    Sources that CONFLICT fail open — mixed evidence is no evidence.
    'unknown' = guard inert (never fires on a guess).
    """
    if day_type != "trend":
        return "unknown"
    _dirs = []
    if breakout_direction in ("up", "down"):
        _dirs.append("long" if breakout_direction == "up" else "short")
    if change_24h is not None:
        try:
            _c = float(change_24h)
            if abs(_c) > momentum_threshold:
                _dirs.append("long" if _c > 0 else "short")
        except (TypeError, ValueError):
            pass
    if day_move_pct is not None:
        try:
            _m = float(day_move_pct)
            _thr = (float(day_move_threshold) if day_move_threshold is not None
                    else momentum_threshold)
            if abs(_m) > _thr:
                _dirs.append("long" if _m > 0 else "short")
        except (TypeError, ValueError):
            pass
    if not _dirs or signal_direction not in ("long", "short"):
        return "unknown"
    if len(set(_dirs)) > 1:
        return "unknown"
    return "aligned" if signal_direction == _dirs[0] else "counter"


def recovery_trend_exempt(verdict: str, enabled: bool) -> bool:
    """True when a recovery-mode coherence-floor skip should be WAIVED: the
    candidate rides a locked trend day in the trend's direction.

    Shadow evidence 2026-08-29 (14,075 refused trades scored to +24h):
    recovery_skip netted -912% (n=1321, avoided +305% vs missed +1217%) and
    199/200 missed winners were trend-day-aligned (VELVET +84%, TRUMP +46%,
    ZEC +44%) — the floor sells the right tail to avoid the chop. Only
    'aligned' exempts; 'counter'/'unknown' fail closed. The SIZE cap (0.5x)
    and TP factor (0.8) still apply — participation at half size, not full
    offense. The exemption is doctrine, not tuning: gates that measure
    net-negative get loosened exactly where the shadow data says they leak.
    """
    return bool(enabled) and verdict == "aligned"


def recovery_trend_exempt_enabled() -> bool:
    import os
    return os.environ.get("RECOVERY_TREND_DAY_EXEMPT_ENABLED", "true").lower() != "false"


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
    locked: bool = False  # True after 30-candle ORB window — classification frozen


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
        # symbol → last day_type that was logged (state-change dedup)
        self._last_logged_type: Dict[str, DayType] = {}
        # symbol → SoDEX 24h snapshot (injected from background poller)
        self._sodex_snapshot: Dict[str, dict] = {}

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
        _existing = self._state.get(symbol)
        if _existing and _existing.locked:
            return  # ORB window closed — classification frozen

        # ── B4: Pre-Session Momentum Forecast ────────────────────────────────────
        # Before the ORB window opens (or with < 15 candles), use overnight SoDEX
        # 24h change to pre-classify.  Strong momentum → TREND immediately;
        # dead flat → CHOP immediately.  ORB data can override later if it
        # contradicts the preemptive call.
        _candles = self._candles.get(symbol, [])
        if len(_candles) < 15:
            _snap = self._sodex_snapshot.get(symbol, {})
            _change_24h = _snap.get("change_pct_24h") if _snap else None
            if _change_24h is not None:
                _change_24h = float(_change_24h)
                _pre_type = None
                if abs(_change_24h) > 5.0:
                    _pre_type = DayType.TREND
                elif abs(_change_24h) < 0.3:
                    _pre_type = DayType.CHOP
                if _pre_type is not None:
                    # Only set if currently unknown — ORB will refine once ready
                    _cur = self._state.get(symbol, DayTypeState())
                    if _cur.day_type == DayType.UNKNOWN:
                        self._state[symbol] = DayTypeState(
                            day_type=_pre_type,
                            classified_at_ms=int(time.time() * 1000),
                            locked=False,
                        )
                        _prev_logged = self._last_logged_type.get(symbol)
                        if _prev_logged != _pre_type:
                            self._last_logged_type[symbol] = _pre_type
                            log.info("day_type_preemptive",
                                     symbol=symbol,
                                     day_type=_pre_type.value,
                                     change_24h=round(_change_24h, 2),
                                     reason="soDEX_overnight_momentum")
            return  # need at least 15 min of data for full ORB classification

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
        # Minimum ATR floor: prevents division-by-zero and extreme ratios when
        # ATR is deceptively low (stale feed, quiet pre-session). Floor at
        # 0.02% of OR high so ratio stays bounded (max ~75×, not 8000×).
        _atr_min = _or_high * 0.0002
        if _atr < _atr_min:
            _atr = _atr_min

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

        # ── SoDEX 24h snapshot bias (injected from background poller) ──────────
        # When ORB is ambiguous (0.7–1.5×), use SoDEX 24h change to break ties.
        _snap = self._sodex_snapshot.get(symbol, {})
        _change_24h = _snap.get("change_pct_24h") if _snap else None
        if _change_24h is not None and _day_type == DayType.RANGE:
            _change_24h = float(_change_24h)
            # Strong daily momentum overrides ambiguous ORB
            if abs(_change_24h) > 5.0:
                _day_type = DayType.TREND
            elif abs(_change_24h) > 2.0 and _breakout_dir:
                # Breakout aligns with 24h direction → upgrade to trend
                _24h_dir = "up" if _change_24h > 0 else "down"
                if _breakout_dir == _24h_dir:
                    _day_type = DayType.TREND
            elif abs(_change_24h) < 0.5 and not _breakout_dir:
                # Dead day — no momentum, no breakout
                _day_type = DayType.CHOP

        _locked = len(_candles) >= 30

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
            locked=_locked,
        )

        # State-change dedup: only log when day_type transitions
        _prev_logged = self._last_logged_type.get(symbol)
        if _prev_logged != _day_type:
            self._last_logged_type[symbol] = _day_type
            log.info("day_type_classified",
                     symbol=symbol,
                     day_type=_day_type.value,
                     or_range=round(_or_range, 4),
                     atr20=round(_atr, 4),
                     ratio=round(_ratio, 3),
                     vol_ratio=round(_vol_ratio, 2),
                     breakout=_breakout_dir,
                     locked=_locked)

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

    def ingest_sodex_snapshot(self, symbol: str, snapshot: dict) -> None:
        """Ingest SoDEX 24h snapshot (change_pct, high, low, turnover) from background poller."""
        if snapshot:
            self._sodex_snapshot[symbol] = snapshot

    def set_prior_day_volume(self, symbol: str, volume: float) -> None:
        """Call at day rollover with prior day's first 30-min volume."""
        self._prior_day_volume[symbol] = max(0.0, volume)

    def reset(self, symbol: str) -> None:
        """Reset at day rollover (e.g., 00:00 UTC crypto, 14:30 UTC equities)."""
        self._candles.pop(symbol, None)
        self._state.pop(symbol, None)
        self._last_logged_type.pop(symbol, None)
        self._sodex_snapshot.pop(symbol, None)
