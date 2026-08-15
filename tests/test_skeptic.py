"""Unit tests for the Skeptic (Mode 3) + Phase-B compression switch — no I/O."""
import math
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from intelligence.skeptic import Skeptic
from intelligence.explosive_scanner import ExplosiveScanner
from intelligence.coherence import CoherenceEngine


class _Journal:
    def __init__(self, records):
        self._records = records

    def scored_records(self):
        return list(self._records)


def _rec(symbol="BTC-USD", coherence=6.0, regime="risk_on", energy=40.0,
         won=True):
    return {"symbol": symbol, "coherence": coherence, "regime": regime,
            "market_energy": energy, "won_24h": won}


class TestSkeptic(unittest.TestCase):
    def test_empty_journal_returns_prior(self):
        s = Skeptic(_Journal([]))
        wr, n = s.base_rate(coherence=6.0, regime="risk_on",
                            market_energy=40.0, symbol="BTC-USD",
                            prior_wr=0.47)
        self.assertEqual(n, 0)
        self.assertAlmostEqual(wr, 0.47)

    def test_shrinkage_math(self):
        # 16/20 wins, k=20, prior 0.5 → (16 + 10) / 40 = 0.65
        s = Skeptic(_Journal([_rec(won=i < 16) for i in range(20)]))
        wr, n = s.base_rate(coherence=6.0, regime="risk_on",
                            market_energy=40.0, symbol="BTC-USD",
                            prior_wr=0.5)
        self.assertEqual(n, 20)
        self.assertAlmostEqual(wr, 0.65)

    def test_large_sample_dominates_prior(self):
        s = Skeptic(_Journal([_rec(won=i < 90) for i in range(100)]))
        wr, n = s.base_rate(coherence=6.0, regime="risk_on",
                            market_energy=40.0, symbol="BTC-USD",
                            prior_wr=0.2)
        self.assertEqual(n, 100)
        self.assertAlmostEqual(wr, (90 + 20 * 0.2) / 120, places=6)
        self.assertGreater(wr, 0.75)     # data speaks, 0.2 prior overruled

    def test_coherence_band_filters(self):
        s = Skeptic(_Journal([_rec(coherence=8.0)]))
        _, n = s.base_rate(coherence=6.0, regime="risk_on",
                           market_energy=40.0, symbol="BTC-USD", prior_wr=0.5)
        self.assertEqual(n, 0)           # 8.0 vs 6.0 > ±0.5 band

    def test_regime_and_energy_filters(self):
        s = Skeptic(_Journal([
            _rec(regime="risk_off"),     # regime mismatch
            _rec(energy=70.0),           # energy mismatch (>±10)
            _rec(),                      # the one true match
        ]))
        _, n = s.base_rate(coherence=6.0, regime="risk_on",
                           market_energy=40.0, symbol="BTC-USD", prior_wr=0.5)
        self.assertEqual(n, 1)

    def test_category_filter(self):
        s = Skeptic(_Journal([_rec(symbol="FARTCOIN-USD")]))   # meme
        _, n = s.base_rate(coherence=6.0, regime="risk_on",
                           market_energy=40.0, symbol="BTC-USD",  # large_cap
                           prior_wr=0.5)
        self.assertEqual(n, 0)

    def test_unscored_records_skipped(self):
        s = Skeptic(_Journal([{"symbol": "BTC-USD", "coherence": 6.0}]))
        _, n = s.base_rate(coherence=6.0, symbol="BTC-USD", prior_wr=0.5)
        self.assertEqual(n, 0)

    def test_memo_caches_result(self):
        j = _Journal([_rec()])
        s = Skeptic(j)
        _, n1 = s.base_rate(coherence=6.0, regime="risk_on",
                            market_energy=40.0, symbol="BTC-USD", prior_wr=0.5)
        j._records.extend([_rec()] * 50)
        _, n2 = s.base_rate(coherence=6.0, regime="risk_on",
                            market_energy=40.0, symbol="BTC-USD", prior_wr=0.5)
        self.assertEqual(n1, n2)         # 60s memo — not rescanned


class _Buf:
    def __init__(self, candles):
        self._candles = candles

    def latest(self, n):
        return self._candles[-n:]


def _candles(closes, vols):
    return [SimpleNamespace(close=c, volume=v, high=c * 1.001, low=c * 0.999)
            for c, v in zip(closes, vols)]


def _loaded_buffers():
    closes, vols = [], []
    px = 2.0
    for i in range(120):
        amp = 0.02 if i < 95 else 0.0005
        px *= (1 + amp * math.sin(i * 1.7))
        closes.append(px)
        vols.append(5000.0 if i >= 105 else 100.0)
    return {"ACE-USD": {"1m": _Buf(_candles(closes, vols))}}


class TestReadiness(unittest.TestCase):
    def test_update_and_read(self):
        now = time.time()
        w = SimpleNamespace(oi_change_pct=lambda *a, **k: None)
        tickers = {"ACE-USD": {"funding_rate": -0.0008}}
        s = ExplosiveScanner()
        self.assertEqual(s.readiness("ACE-USD", now=now), 0.0)   # nothing yet
        s.update_readiness(["ACE-USD"], _loaded_buffers(), tickers, w, now=now)
        # compression + funding_extreme + volume_breakout = 3 precursors
        self.assertAlmostEqual(s.readiness("ACE-USD", now=now), 0.75)

    def test_stale_readiness_reads_zero(self):
        now = time.time()
        w = SimpleNamespace(oi_change_pct=lambda *a, **k: None)
        s = ExplosiveScanner()
        s.update_readiness(["ACE-USD"], _loaded_buffers(),
                           {"ACE-USD": {"funding_rate": -0.0008}}, w, now=now)
        self.assertEqual(s.readiness("ACE-USD", now=now + 600), 0.0)


class TestCompressionSwitch(unittest.TestCase):
    def _structure_score(self, readiness):
        eng = CoherenceEngine()
        with patch("intelligence.coherence._get_breakout_readiness",
                   return_value=readiness):
            _, _, comp = eng.calculate_weighted_score(
                "BTC-USD", {"market_type": "compression"})
        return comp["structure"]

    def test_flat_when_silent(self):
        self.assertEqual(self._structure_score(0.0), 0.5)

    def test_loading_at_two_precursors(self):
        self.assertEqual(self._structure_score(0.5), 1.25)

    def test_expansion_parity_when_spring_fires(self):
        self.assertEqual(self._structure_score(0.75), 2.0)
        self.assertEqual(self._structure_score(1.0), 2.0)


if __name__ == "__main__":
    unittest.main()
