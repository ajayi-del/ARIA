"""
intelligence/oracle_engine.py — ORACLE Pre-Cascade Smart Money Detector

ORACLE is Sovereign's pre-cascade intelligence layer. It synthesizes 4 cross-venue
indicators to detect institutional positioning BEFORE liquidation cascades manifest
in on-chain data — capturing the 2-3 minute lead that separates cause from effect.

Equity extension (Leak 6 fix):
  - SoDEX-only assets (equities) have no Bybit OI or cross-venue basis.
  - VPIN and funding drift are fed from SoDEX directly.
  - Mark-index divergence (from MarkPriceStore) proxies basis for equities.
  - Equity cluster requires 2/3 sub-signals (OI absent).

Philosophy:
  ARIA sees the EFFECT (liquidations) at t=0.
  ORACLE infers the CAUSE (smart positioning) at t=-120s.
  The edge is in the gap.

Sub-signals (4 total, need >= MIN_ALIGNED_SUBS to fire):
  1. VPIN spike     : BTC/ETH/SOL VPIN > 0.55 -> informed flow active
  2. OI momentum    : Rolling 5m OI delta > 1.5% on anchor assets -> institutions entering
  3. Cross-venue basis: Bybit mark leading SoDEX > 0.05% -> smart money pricing ahead
  4. Funding drift  : 3 consecutive readings trending same direction -> carry pressure building

Signal coherence contribution:
  3/4 subs aligned -> coherence_boost = 0.8 (moderate — advisory)
  4/4 subs aligned -> coherence_boost = 1.5 (strong — near-certain cluster)

Fusion multiplier (oracle + cascade aligned):
  3/4 subs -> 1.10x size
  4/4 subs -> 1.25x size

Integration (main.py):
  _oracle_engine = OracleEngine()

  # oracle_loop (every 30s):
  _oracle_engine.update_vpin(sym, vpin)
  _oracle_engine.update_oi(sym, oi_value)
  _oracle_engine.update_basis(sym, bybit_mark, sodex_mark)
  _oracle_engine.update_funding(sym, rate)
  _oracle_engine.update_mark_index_divergence(sym, div_pct)   # equities only
  _oracle_engine.tick()

  # on_signal_ready (after augur_whisper):
  _oracle_boost = _oracle_engine.get_coherence_boost(symbol, direction)
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

import structlog

log = structlog.get_logger(__name__)

# ── Thresholds ────────────────────────────────────────────────────────────────
SIGNAL_TTL_S           = 300.0   # 5 min signal validity after fire
MIN_ALIGNED_SUBS       = 3       # >=3 of 4 sub-signals required (crypto)
MIN_ALIGNED_EQUITY     = 2       # >=2 of 3 sub-signals required (equity, no OI)
VPIN_THRESHOLD         = 0.55    # informed flow threshold
OI_DELTA_PCT_THRESHOLD = 1.5     # % OI change in 5m to qualify
BASIS_THRESHOLD        = 0.0005  # 0.05% Bybit-SoDEX basis lead
FUNDING_STREAK_LEN     = 3       # consecutive readings in same direction
COHERENCE_BOOST_STRONG   = 1.5   # 4/4 subs
COHERENCE_BOOST_MODERATE = 0.8   # 3/4 subs

_ANCHOR_SYMS = ("BTC-USD", "ETH-USD", "SOL-USD")
_EQUITY_ANCHOR_SYMS = (
    "NVDA-USD", "MSFT-USD", "AAPL-USD", "AMZN-USD",
    "GOOGL-USD", "META-USD", "TSLA-USD", "TSM-USD", "ORCL-USD",
    "SPCX-USD", "USTECH100-USD",
)


@dataclass
class OracleSignal:
    direction:       str    # "long" | "short"
    strength:        float  # 0.0-1.0 composite score
    subs_fired:      int    # 2-4
    strategy_hint:   str    # "scalp" | "trend"
    coherence_boost: float
    long_subs:       int    # debug: how many long sub-signals
    short_subs:      int    # debug: how many short sub-signals
    fired_at:        float  = field(default_factory=time.time)

    def is_valid(self) -> bool:
        return (time.time() - self.fired_at) < SIGNAL_TTL_S

    @property
    def fusion_mult(self) -> float:
        """Size multiplier when oracle + cascade aligned (applied in aftermath path)."""
        return 1.25 if self.subs_fired >= 4 else 1.10

    @property
    def age_s(self) -> float:
        return time.time() - self.fired_at


class OracleEngine:
    """
    Pre-cascade smart money cluster detector using ARIA's existing cross-venue data.

    Reuses data already fetched by ARIA (VPIN from MarkPriceStore, OI from Bybit
    ticker stores, basis from mark price comparison, funding from live funding dict)
    — zero new external API calls.
    """

    def __init__(self) -> None:
        self._vpin:             Dict[str, float]                   = {}
        self._oi_history:       Dict[str, Deque[Tuple[float, float]]] = {}
        self._basis:            Dict[str, float]                   = {}
        self._funding:          Dict[str, Deque[float]]            = {}
        self._mark_index_div:   Dict[str, float]                   = {}
        self._crypto_signal:    Optional[OracleSignal]             = None
        self._equity_signal:    Optional[OracleSignal]             = None

    # ── Data ingestion ────────────────────────────────────────────────────────

    def update_vpin(self, sym: str, vpin: float) -> None:
        self._vpin[sym] = float(vpin)

    def update_oi(self, sym: str, oi_value: float) -> None:
        if oi_value <= 0:
            return
        now = time.time()
        if sym not in self._oi_history:
            self._oi_history[sym] = deque(maxlen=20)
        self._oi_history[sym].append((now, oi_value))
        cutoff = now - 360.0  # 6 min retention
        hist = self._oi_history[sym]
        while hist and hist[0][0] < cutoff:
            hist.popleft()

    def update_basis(self, sym: str, bybit_price: float, sodex_price: float) -> None:
        if bybit_price > 0 and sodex_price > 0:
            self._basis[sym] = (bybit_price - sodex_price) / sodex_price

    def update_funding(self, sym: str, rate: float) -> None:
        if sym not in self._funding:
            self._funding[sym] = deque(maxlen=10)
        self._funding[sym].append(float(rate))

    def update_mark_index_divergence(self, sym: str, div_pct: float) -> None:
        """SoDEX mark-index divergence for equity basis proxy (Leak 6)."""
        self._mark_index_div[sym] = float(div_pct)

    # ── Cluster evaluation ────────────────────────────────────────────────────

    def _evaluate_crypto(self) -> Optional[OracleSignal]:
        """Evaluate 4 sub-signals on crypto anchors. Require 3/4 aligned."""
        long_subs = short_subs = 0
        detail: Dict[str, str] = {}

        # Sub-signal 1 — VPIN spike
        peak_vpin = max((self._vpin.get(s, 0.0) for s in _ANCHOR_SYMS), default=0.0)
        if peak_vpin > VPIN_THRESHOLD:
            btc_basis = self._basis.get("BTC-USD", 0.0)
            if btc_basis > 0:
                long_subs += 1
                detail["vpin"] = f"long (peak={round(peak_vpin,2)}, btc_basis=+)"
            elif btc_basis < 0:
                short_subs += 1
                detail["vpin"] = f"short (peak={round(peak_vpin,2)}, btc_basis=-)"

        # Sub-signal 2 — OI momentum
        oi_long_n = oi_short_n = 0
        for sym in _ANCHOR_SYMS:
            hist = self._oi_history.get(sym)
            if not hist or len(hist) < 3:
                continue
            old_oi = hist[0][1]
            new_oi = hist[-1][1]
            if old_oi > 0:
                delta_pct = (new_oi - old_oi) / old_oi * 100
                if delta_pct > OI_DELTA_PCT_THRESHOLD:
                    oi_long_n += 1
                elif delta_pct < -OI_DELTA_PCT_THRESHOLD:
                    oi_short_n += 1
        if oi_long_n >= 2:
            long_subs += 1
            detail["oi"] = f"long ({oi_long_n}/3 anchors expanding)"
        elif oi_short_n >= 2:
            short_subs += 1
            detail["oi"] = f"short ({oi_short_n}/3 anchors contracting)"

        # Sub-signal 3 — Cross-venue basis
        basis_long_n  = sum(1 for s in _ANCHOR_SYMS if self._basis.get(s, 0.0) >  BASIS_THRESHOLD)
        basis_short_n = sum(1 for s in _ANCHOR_SYMS if self._basis.get(s, 0.0) < -BASIS_THRESHOLD)
        if basis_long_n >= 2:
            long_subs += 1
            detail["basis"] = f"long ({basis_long_n}/3 bybit>sodex)"
        elif basis_short_n >= 2:
            short_subs += 1
            detail["basis"] = f"short ({basis_short_n}/3 bybit<sodex)"

        # Sub-signal 4 — Funding drift
        _found = False
        for sym in _ANCHOR_SYMS:
            if _found:
                break
            hist = self._funding.get(sym)
            if not hist or len(hist) < FUNDING_STREAK_LEN:
                continue
            recent = list(hist)[-FUNDING_STREAK_LEN:]
            if all(recent[i] < recent[i - 1] for i in range(1, len(recent))):
                short_subs += 1
                detail["funding"] = f"short (declining {FUNDING_STREAK_LEN}x on {sym})"
                _found = True
            elif all(recent[i] > recent[i - 1] for i in range(1, len(recent))):
                long_subs += 1
                detail["funding"] = f"long (rising {FUNDING_STREAK_LEN}x on {sym})"
                _found = True

        return self._make_signal(long_subs, short_subs, detail, MIN_ALIGNED_SUBS, "crypto")

    def _evaluate_equity(self) -> Optional[OracleSignal]:
        """Evaluate 3 sub-signals on equity anchors (no OI). Require 2/3 aligned."""
        long_subs = short_subs = 0
        detail: Dict[str, str] = {}

        # Sub-signal 1 — VPIN spike (equity VPIN from MarkPriceStore trade flow)
        peak_vpin = max((self._vpin.get(s, 0.0) for s in _EQUITY_ANCHOR_SYMS), default=0.0)
        if peak_vpin > VPIN_THRESHOLD:
            # Tiebreak via SPY proxy (SPCX or USTECH100 divergence)
            spy_div = self._mark_index_div.get("SPCX-USD", 0.0)
            if spy_div == 0.0:
                spy_div = self._mark_index_div.get("USTECH100-USD", 0.0)
            if spy_div > 0:
                long_subs += 1
                detail["vpin"] = f"long (peak={round(peak_vpin,2)}, spy_div=+)"
            elif spy_div < 0:
                short_subs += 1
                detail["vpin"] = f"short (peak={round(peak_vpin,2)}, spy_div=-)"

        # Sub-signal 2 — Mark-index divergence (proxy for cross-venue basis)
        div_long_n  = sum(1 for s in _EQUITY_ANCHOR_SYMS if self._mark_index_div.get(s, 0.0) >  BASIS_THRESHOLD * 100)
        div_short_n = sum(1 for s in _EQUITY_ANCHOR_SYMS if self._mark_index_div.get(s, 0.0) < -BASIS_THRESHOLD * 100)
        # Note: div_pct is in percent (e.g. 0.05), basis is in decimal (0.0005).
        # We compare div_pct > 0.05 which equals basis > 0.0005.
        if div_long_n >= 2:
            long_subs += 1
            detail["div"] = f"long ({div_long_n}/{len(_EQUITY_ANCHOR_SYMS)} mark>index)"
        elif div_short_n >= 2:
            short_subs += 1
            detail["div"] = f"short ({div_short_n}/{len(_EQUITY_ANCHOR_SYMS)} mark<index)"

        # Sub-signal 3 — Funding drift (SoDEX funding rates for equities)
        _found = False
        for sym in _EQUITY_ANCHOR_SYMS:
            if _found:
                break
            hist = self._funding.get(sym)
            if not hist or len(hist) < FUNDING_STREAK_LEN:
                continue
            recent = list(hist)[-FUNDING_STREAK_LEN:]
            if all(recent[i] < recent[i - 1] for i in range(1, len(recent))):
                short_subs += 1
                detail["funding"] = f"short (declining {FUNDING_STREAK_LEN}x on {sym})"
                _found = True
            elif all(recent[i] > recent[i - 1] for i in range(1, len(recent))):
                long_subs += 1
                detail["funding"] = f"long (rising {FUNDING_STREAK_LEN}x on {sym})"
                _found = True

        return self._make_signal(long_subs, short_subs, detail, MIN_ALIGNED_EQUITY, "equity")

    def _make_signal(
        self,
        long_subs: int,
        short_subs: int,
        detail: Dict[str, str],
        min_aligned: int,
        domain: str,
    ) -> Optional[OracleSignal]:
        dominant_dir: Optional[str] = None
        dominant_count = 0

        if long_subs >= min_aligned and long_subs > short_subs:
            dominant_dir = "long"
            dominant_count = long_subs
        elif short_subs >= min_aligned and short_subs > long_subs:
            dominant_dir = "short"
            dominant_count = short_subs

        if dominant_dir is None:
            return None

        strength = min(1.0, dominant_count / 4.0)
        coh_boost = COHERENCE_BOOST_STRONG if dominant_count >= 4 else COHERENCE_BOOST_MODERATE
        strategy = "scalp" if ("vpin" in detail and ("oi" in detail or "div" in detail)) else "trend"

        log.info(
            f"oracle_cluster_detected_{domain}",
            direction=dominant_dir,
            strength=round(strength, 2),
            subs_fired=dominant_count,
            strategy=strategy,
            coherence_boost=coh_boost,
            sub_detail=detail,
        )

        return OracleSignal(
            direction=dominant_dir,
            strength=strength,
            subs_fired=dominant_count,
            strategy_hint=strategy,
            coherence_boost=coh_boost,
            long_subs=long_subs,
            short_subs=short_subs,
        )

    def tick(self) -> None:
        """
        Recompute cluster signals from current sub-signal state.
        Call every 30s from oracle_loop in main.py.
        """
        # Crypto signal
        new_crypto = self._evaluate_crypto()
        if new_crypto is not None:
            self._crypto_signal = new_crypto
        elif self._crypto_signal and not self._crypto_signal.is_valid():
            self._crypto_signal = None

        # Equity signal
        new_equity = self._evaluate_equity()
        if new_equity is not None:
            self._equity_signal = new_equity
        elif self._equity_signal and not self._equity_signal.is_valid():
            self._equity_signal = None

    # ── Public interface ──────────────────────────────────────────────────────

    def _is_equity(self, symbol: str) -> bool:
        return symbol in _EQUITY_ANCHOR_SYMS

    def get_coherence_boost(self, symbol: str, direction: str) -> float:
        """
        Coherence boost for (symbol, direction). 0.0 if no active signal or mismatch.
        Routes equity symbols to equity signal, crypto to crypto signal.
        """
        sig = self._equity_signal if self._is_equity(symbol) else self._crypto_signal
        if sig is None or not sig.is_valid():
            # Clear stale reference
            if self._is_equity(symbol):
                self._equity_signal = None
            else:
                self._crypto_signal = None
            return 0.0
        if sig.direction != direction:
            return 0.0
        return sig.coherence_boost

    def get_active_signal(self, symbol: str = "") -> Optional[OracleSignal]:
        sig = self._equity_signal if self._is_equity(symbol) else self._crypto_signal
        if sig and sig.is_valid():
            return sig
        return None

    def get_fusion_mult(self, direction: str, symbol: str = "") -> float:
        """
        Size multiplier for oracle + cascade fusion.
        Called from aftermath/cascade execution path when both signals align.
        """
        sig = self.get_active_signal(symbol)
        if sig and sig.direction == direction:
            return sig.fusion_mult
        return 1.0

    def summary(self) -> dict:
        crypto = self._crypto_signal
        equity = self._equity_signal
        return {
            "crypto": {
                "active":    crypto is not None and crypto.is_valid(),
                "direction": crypto.direction if crypto else None,
                "strength":  round(crypto.strength, 2) if crypto else 0.0,
                "subs":      crypto.subs_fired if crypto else 0,
                "strategy":  crypto.strategy_hint if crypto else None,
                "boost":     crypto.coherence_boost if crypto else 0.0,
                "age_s":     round(crypto.age_s, 0) if crypto else 0,
            },
            "equity": {
                "active":    equity is not None and equity.is_valid(),
                "direction": equity.direction if equity else None,
                "strength":  round(equity.strength, 2) if equity else 0.0,
                "subs":      equity.subs_fired if equity else 0,
                "strategy":  equity.strategy_hint if equity else None,
                "boost":     equity.coherence_boost if equity else 0.0,
                "age_s":     round(equity.age_s, 0) if equity else 0,
            },
        }
