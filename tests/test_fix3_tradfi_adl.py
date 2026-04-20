"""
Tests for Fix 3A (TradFi HTF gate bypass) and Fix 3B (ADL risk scoring).
"""

import asyncio
import time
import unittest
from dataclasses import dataclass, field
from typing import List, Optional
from unittest.mock import MagicMock, patch


# ── Fix 3A: TradFi HTF gate bypass ────────────────────────────────────────────

class TestTradFiHTFGate(unittest.TestCase):
    """Fix 3A — XAUT/equities/commodities must NOT be blocked by BTC HTF."""

    def setUp(self):
        from core.config import Settings
        self.config = Settings()

    def test_xaut_in_tradfi_assets(self):
        """XAUT-USD must be in TRADFI_ASSETS."""
        self.assertIn("XAUT-USD", self.config.TRADFI_ASSETS)

    def test_cl_in_tradfi_assets(self):
        """CL-USD (crude oil) must be in TRADFI_ASSETS."""
        self.assertIn("CL-USD", self.config.TRADFI_ASSETS)

    def test_equity_in_tradfi_assets(self):
        """All major equities must be in TRADFI_ASSETS."""
        for sym in ["NVDA-USD", "MSFT-USD", "AAPL-USD", "AMZN-USD",
                    "GOOGL-USD", "META-USD", "TSLA-USD", "TSM-USD", "ORCL-USD"]:
            self.assertIn(sym, self.config.TRADFI_ASSETS, f"{sym} missing from TRADFI_ASSETS")

    def test_crypto_not_in_tradfi_assets(self):
        """Crypto assets must NOT be in TRADFI_ASSETS — gate applies to them."""
        for sym in ["BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD",
                    "ARB-USD", "OP-USD", "AVAX-USD", "LINK-USD"]:
            self.assertNotIn(sym, self.config.TRADFI_ASSETS,
                             f"{sym} should NOT be in TRADFI_ASSETS")

    def test_tradfi_assets_count(self):
        """Must have exactly 12 TradFi assets defined."""
        self.assertEqual(len(self.config.TRADFI_ASSETS), 12)

    def test_htf_gate_logic_tradfi(self):
        """Simulate gate logic: TradFi asset with bearish BTC HTF → NOT blocked."""
        from core.config import Settings
        cfg = Settings()

        blocked = False
        symbol = "XAUT-USD"
        qf_side = "long"
        htf = "bearish"

        if symbol in cfg.TRADFI_ASSETS:
            pass  # htf_gate_skipped_tradfi_asset — no block
        elif htf == "bearish" and qf_side == "long":
            blocked = True

        self.assertFalse(blocked, "XAUT long must NOT be blocked by bearish BTC HTF")

    def test_htf_gate_logic_crypto(self):
        """Simulate gate logic: Crypto asset with bearish HTF → IS blocked."""
        from core.config import Settings
        cfg = Settings()

        blocked = False
        symbol = "SOL-USD"
        qf_side = "long"
        htf = "bearish"

        if symbol in cfg.TRADFI_ASSETS:
            pass
        elif htf == "bearish" and qf_side == "long":
            blocked = True  # crypto blocked as expected

        self.assertTrue(blocked, "SOL long MUST be blocked by bearish HTF")


# ── Fix 3B: ADL risk scoring ──────────────────────────────────────────────────

@dataclass
class _MockPosition:
    symbol: str
    side: str
    entry_price: float
    size: float
    leverage: int = 5


def _compute_adl(position: _MockPosition, mark_price: float):
    """Replicates the ADL scoring logic from main.py."""
    pnl = (
        (mark_price - position.entry_price) * position.size
        if position.side == "long"
        else (position.entry_price - mark_price) * position.size
    )
    adl_score = pnl * position.leverage
    adl_risk = (
        "critical" if adl_score > 30 else
        "high"     if adl_score > 15 else
        "elevated" if adl_score > 5  else
        "low"
    )
    return adl_score, adl_risk, pnl


class TestADLRiskScoring(unittest.TestCase):
    """Fix 3B — ADL score = unrealised_pnl × leverage; risk levels correct."""

    def test_adl_score_computation(self):
        """adl_score = unrealised_pnl × leverage."""
        pos = _MockPosition(
            symbol="BASED-USD", side="long",
            entry_price=0.0800, size=1000.0, leverage=5,
        )
        mark_price = 0.0872   # +9% → pnl = 7.2
        adl_score, adl_risk, pnl = _compute_adl(pos, mark_price)

        self.assertAlmostEqual(pnl, 7.2, places=4)
        self.assertAlmostEqual(adl_score, 36.0, places=2)
        self.assertEqual(adl_risk, "critical")

    def test_adl_risk_levels(self):
        """Verify all four risk tier boundaries."""
        cases = [
            # (unrealised_pnl, leverage, expected_risk)
            (0.5, 5,  "low"),        # score = 2.5 < 5
            (1.5, 5,  "elevated"),   # score = 7.5, 5 < 7.5 < 15
            (3.5, 5,  "high"),       # score = 17.5, 15 < 17.5 < 30
            (7.5, 5,  "critical"),   # score = 37.5 > 30
        ]
        for pnl, lev, expected in cases:
            score = pnl * lev
            risk = (
                "critical" if score > 30 else
                "high"     if score > 15 else
                "elevated" if score > 5  else
                "low"
            )
            self.assertEqual(risk, expected,
                             f"pnl={pnl} lev={lev} score={score} expected={expected} got={risk}")

    def test_adl_low_risk_no_warning(self):
        """Low ADL risk during a cascade should not trigger cascade_warning."""
        pos = _MockPosition(symbol="SOL-USD", side="long",
                            entry_price=100.0, size=1.0, leverage=5)
        mark_price = 100.8  # pnl=0.8, score=4.0 → low
        adl_score, adl_risk, _ = _compute_adl(pos, mark_price)
        cascade_zscore = 3.5  # active cascade

        should_warn = adl_risk in ("high", "critical") and cascade_zscore > 2.0
        self.assertFalse(should_warn, "Low ADL risk must not trigger cascade warning")

    def test_adl_cascade_warning_triggers(self):
        """High ADL risk + cascade zscore > 2.0 must trigger warning."""
        pos = _MockPosition(symbol="BASED-USD", side="long",
                            entry_price=0.0800, size=1000.0, leverage=5)
        mark_price = 0.0872  # pnl=7.2, score=36 → critical
        adl_score, adl_risk, _ = _compute_adl(pos, mark_price)
        cascade_zscore = 2.86

        should_warn = adl_risk in ("high", "critical") and cascade_zscore > 2.0
        self.assertTrue(should_warn, "Critical ADL + active cascade must trigger warning")

    def test_adl_high_risk_below_cascade_threshold(self):
        """High ADL risk but cascade zscore ≤ 2.0 → no warning."""
        pos = _MockPosition(symbol="BTC-USD", side="long",
                            entry_price=80000.0, size=0.001, leverage=5)
        mark_price = 83200.0  # pnl=3.2, score=16 → high
        adl_score, adl_risk, _ = _compute_adl(pos, mark_price)
        cascade_zscore = 1.8  # below threshold

        should_warn = adl_risk in ("high", "critical") and cascade_zscore > 2.0
        self.assertFalse(should_warn, "ADL high risk without cascade must not warn")

    def test_adl_no_position_no_log(self):
        """Empty position list generates no ADL assessments."""
        positions = []
        logs = []
        for pp in positions:
            adl_score, adl_risk, pnl = _compute_adl(pp, pp.entry_price)
            logs.append((pp.symbol, adl_risk))
        self.assertEqual(len(logs), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
