"""Paper-synthesis bundle pins (2026-08-22).

- LPPL (Sornette): log-periodic super-exponential run-up scores high, random
  walk and thin data score None/low; scanner readiness boost is additive and
  kill-switchable.
- Conviction Review winner-side (Frazzini mirror): green beyond the noise
  band + thesis inversion → bank early; kill switch → hold_winner.
- Lo-MacKinlay path class: mr shortens grace by the mult, trend doesn't.
- Aster book anchor: fresh L4 mid re-anchors the entry reference; stale book
  / wrong venue / kill switch are all fail-open to the mark.
- Digest: fundamental_law (Grinold-Kahn IC×√breadth) + recheck_yield math.
"""
import math
import random
import unittest
from types import SimpleNamespace
from unittest import mock

from intelligence.lppl import lppl_confidence
from intelligence.conviction_review import (
    PositionSnapshot, abandonment_verdict, BASE_GRACE_S,
)
from intelligence.explosive_scanner import ExplosiveScanner
from tools.daily_digest import fundamental_law, recheck_yield


# ── LPPL ─────────────────────────────────────────────────────────────────────

def _lppl_series(n=120, tc_mult=1.10, m=0.5, w=8.0, B=-0.02, seed=1):
    """Synthetic LPPL run-up: log p = A + B·dt^m + C·dt^m·cos(w ln dt)."""
    random.seed(seed)
    t = list(range(1, n + 1))
    tc = tc_mult * n
    out = []
    for ti in t:
        dt = tc - ti
        # C/B ratio ≈ 0.05 — the empirical LPPL band; a larger ratio fails
        # Sornette's damping condition m|B|/(ω·amp) ≥ 1 by construction.
        v = 4.6 + B * dt ** m + 0.001 * dt ** m * math.cos(w * math.log(dt))
        out.append(math.exp(v) * (1 + random.gauss(0, 0.0005)))
    return out


class TestLPPL(unittest.TestCase):
    def test_lppl_signature_scores_high(self):
        conf = lppl_confidence(_lppl_series())
        self.assertIsNotNone(conf)
        self.assertGreaterEqual(conf, 0.5)

    def test_random_walk_low_or_none(self):
        random.seed(4)
        p, out = 100.0, []
        for _ in range(120):
            p *= 1 + random.gauss(0, 0.002)
            out.append(p)
        conf = lppl_confidence(out)
        self.assertTrue(conf is None or conf < 0.5)

    def test_thin_and_bad_none(self):
        self.assertIsNone(lppl_confidence([100.0] * 30))
        self.assertIsNone(lppl_confidence([0.0] * 120))


class _Buf:
    def __init__(self, candles):
        self._c = candles

    def latest(self, n):
        return self._c[-n:]


def _scanner_candles(n=120, px=100.0, width=0.01):
    """Alternating-tight candles: compressed BB, quiet volume — base score 0
    unless precursors fire; LPPL boost must then be visible in isolation."""
    out = []
    p = px
    for i in range(n):
        c = p * (1 + (width if i % 2 == 0 else -width))
        out.append(SimpleNamespace(close=c, volume=1.0))
        p = c
    return out


class TestScannerLPPLBoost(unittest.TestCase):
    def _run(self, lppl_enabled, fake_conf):
        sc = ExplosiveScanner()
        bufs = {"X-USD": {"1m": _Buf(_scanner_candles())}}
        with mock.patch("intelligence.lppl.lppl_confidence",
                        return_value=fake_conf):
            sc.update_readiness(["X-USD"], bufs, {}, None,
                                lppl_enabled=lppl_enabled)
        return sc._readiness["X-USD"][1], sc.metrics["X-USD"]

    def test_boost_applied_at_high_conf(self):
        base, _ = self._run(False, None)
        boosted, m = self._run(True, 0.9)
        self.assertAlmostEqual(boosted, min(1.0, base + 0.225), places=6)
        self.assertEqual(m["lppl_conf"], 0.9)

    def test_no_boost_below_half_conf(self):
        base, _ = self._run(False, None)
        same, m = self._run(True, 0.4)
        self.assertAlmostEqual(same, base, places=6)
        self.assertEqual(m["lppl_conf"], 0.4)

    def test_kill_switch_bit_for_bit(self):
        base, m0 = self._run(False, None)
        off, m1 = self._run(False, 0.9)   # conf would boost, but never called
        self.assertAlmostEqual(base, off, places=6)
        self.assertIsNone(m1["lppl_conf"])


# ── Conviction Review winner-side + path class ───────────────────────────────

def _snap(upnl=0.30, age=1200.0, entry=100.0, size=0.06, atr=0.004):
    return PositionSnapshot(symbol="X-USD", side="long", upnl=upnl,
                            entry=entry, size=size, age_s=age,
                            initial_margin=1.2, atr_pct=atr)


class TestWinnerInversion(unittest.TestCase):
    def test_green_beyond_band_holds_without_inversion(self):
        v = abandonment_verdict(_snap(), now=10_000.0, trend_verdict="aligned")
        self.assertFalse(v.abandon)
        self.assertEqual(v.reason, "hold_winner")

    def test_winner_inversion_banks_early(self):
        v = abandonment_verdict(
            _snap(), now=10_000.0, trend_verdict="counter",
            last_opp_dir_ts=9_500.0)          # fresh opposite signal
        self.assertTrue(v.abandon)
        self.assertEqual(v.reason, "winner_inversion")

    def test_winner_inversion_kill_switch(self):
        v = abandonment_verdict(
            _snap(), now=10_000.0, trend_verdict="counter",
            last_opp_dir_ts=9_500.0, winner_inversion_enabled=False)
        self.assertFalse(v.abandon)
        self.assertEqual(v.reason, "hold_winner")

    def test_winner_inversion_needs_min_age(self):
        v = abandonment_verdict(
            _snap(age=300.0), now=10_000.0, trend_verdict="counter",
            last_opp_dir_ts=9_800.0)
        self.assertFalse(v.abandon)
        self.assertEqual(v.reason, "hold_winner")

    def test_winner_inversion_needs_fresh_opp(self):
        v = abandonment_verdict(
            _snap(), now=10_000.0, trend_verdict="counter",
            last_opp_dir_ts=8_000.0)          # stale — not inversion
        self.assertFalse(v.abandon)

    def test_small_green_still_noise_band(self):
        v = abandonment_verdict(_snap(upnl=0.01), now=10_000.0)
        self.assertFalse(v.abandon)
        self.assertEqual(v.reason, "hold_noise_band")


class TestPathClassGrace(unittest.TestCase):
    def test_mr_shortens_grace(self):
        v = abandonment_verdict(_snap(upnl=-0.05, age=100.0), now=10_000.0,
                                trend_verdict="aligned", path_class="mr",
                                mr_grace_mult=0.75)
        self.assertAlmostEqual(v.grace_s, BASE_GRACE_S * 4.0 * 0.75)

    def test_trend_keeps_grace(self):
        v = abandonment_verdict(_snap(upnl=-0.05, age=100.0), now=10_000.0,
                                trend_verdict="aligned", path_class="trend",
                                mr_grace_mult=0.75)
        self.assertAlmostEqual(v.grace_s, BASE_GRACE_S * 4.0)

    def test_mr_base_grace(self):
        v = abandonment_verdict(_snap(upnl=-0.05, age=100.0), now=10_000.0,
                                trend_verdict="unknown", path_class="mr",
                                mr_grace_mult=0.75)
        self.assertAlmostEqual(v.grace_s, BASE_GRACE_S * 0.75)


# ── Aster book anchor ────────────────────────────────────────────────────────

class TestAsterBookAnchor(unittest.TestCase):
    def _cand(self):
        return SimpleNamespace(symbol="X-USD", entry_price=100.0)

    def _store(self, age, bid=100.2, ask=100.4):
        return SimpleNamespace(age_ms=lambda: age,
                               top_of_book=lambda: (bid, ask, ask - bid))

    def test_fresh_book_reanchors(self):
        from main import _anchor_aster_entry_price
        c = self._cand()
        with mock.patch("main.venue.venue_for", return_value="aster"):
            _anchor_aster_entry_price(c, {"X-USD": self._store(120)}, True)
        self.assertAlmostEqual(c.entry_price, 100.3)

    def test_stale_book_fail_open(self):
        from main import _anchor_aster_entry_price
        c = self._cand()
        with mock.patch("main.venue.venue_for", return_value="aster"):
            _anchor_aster_entry_price(c, {"X-USD": self._store(900)}, True)
        self.assertEqual(c.entry_price, 100.0)

    def test_sodex_symbol_untouched(self):
        from main import _anchor_aster_entry_price
        c = self._cand()
        with mock.patch("main.venue.venue_for", return_value="sodex"):
            _anchor_aster_entry_price(c, {"X-USD": self._store(50)}, True)
        self.assertEqual(c.entry_price, 100.0)

    def test_kill_switch(self):
        from main import _anchor_aster_entry_price
        c = self._cand()
        with mock.patch("main.venue.venue_for", return_value="aster"):
            _anchor_aster_entry_price(c, {"X-USD": self._store(50)}, False)
        self.assertEqual(c.entry_price, 100.0)


# ── Digest sections ──────────────────────────────────────────────────────────

def _rec(coh, pnl, outcome="win"):
    return {"coherence_score": coh, "pnl_r": pnl, "outcome": outcome,
            "pnl_usd": pnl, "pnl_net_usd": pnl}


class TestFundamentalLaw(unittest.TestCase):
    def test_thin_not_measured(self):
        r = fundamental_law([_rec(7.0, 0.5)] * 5)
        self.assertEqual(r["n"], 5)
        self.assertIn("thin", r["note"])

    def test_positive_ic_verdict(self):
        recs = [_rec(5.0 + i * 0.3, -0.4 + i * 0.1,
                     "win" if i % 2 else "loss") for i in range(20)]
        r = fundamental_law(recs)
        self.assertGreater(r["ic"], 0.9)
        self.assertEqual(r["breadth"], 20)
        self.assertAlmostEqual(r["ir_weekly"], r["ic"] * math.sqrt(20), places=2)
        self.assertEqual(r["verdict"], "skill_positive")

    def test_zero_coherence_rows_skipped(self):
        recs = [_rec(0.0, 0.1)] * 15
        self.assertIn("thin", fundamental_law(recs)["note"])


class TestRecheckYield(unittest.TestCase):
    def test_math(self):
        cv = {"conviction_decay_deferred:hold_aligned_grace": 4,
              "conviction_decay_closed:signal_absent": 1}
        closed = [
            {"exit_reason": "conviction_decay:signal_absent", "pnl": "$-0.50"},
            {"exit_reason": "conviction_decay:thesis_inversion", "pnl": "$+0.20"},
            {"exit_reason": "software_stop", "pnl": "$-1.00"},
        ]
        r = recheck_yield(cv, closed)
        self.assertEqual(r["deferred"], 4)
        self.assertEqual(r["abandons"]["signal_absent"], {"n": 1, "pnl": -0.5})
        self.assertEqual(r["abandons"]["thesis_inversion"], {"n": 1, "pnl": 0.2})
        self.assertNotIn("software_stop", r["abandons"])


if __name__ == "__main__":
    unittest.main()


# ── Actionable dust census (distracted-mode deadlock fix, 2026-08-22) ────────

class TestActionableDustRatio(unittest.TestCase):
    def _pos(self, entry, size):
        return SimpleNamespace(entry_price=entry, size=size)

    def test_unclosable_sodex_dust_not_counted(self):
        from main import _actionable_dust_ratio
        items = [("BTC-USD", [self._pos(77000.0, 1e-05)]),   # $0.77 — unclosable
                 ("XAUT-USD", [self._pos(4600.0, 0.04)])]     # $184 — not dust
        with mock.patch("main.venue.venue_for", return_value="sodex"):
            self.assertEqual(_actionable_dust_ratio(items), 0.0)

    def test_actionable_dust_counted(self):
        from main import _actionable_dust_ratio
        items = [("SOL-USD", [self._pos(95.0, 0.15)]),        # $14.25 — closable dust
                 ("XAUT-USD", [self._pos(4600.0, 0.04)])]
        with mock.patch("main.venue.venue_for", return_value="sodex"):
            self.assertEqual(_actionable_dust_ratio(items), 0.5)

    def test_aster_min_close_is_one_dollar(self):
        from main import _actionable_dust_ratio
        items = [("UNI-USD", [self._pos(4.4, 1.0)])]          # $4.40 — dust, closable on aster
        with mock.patch("main.venue.venue_for", return_value="aster"):
            self.assertEqual(_actionable_dust_ratio(items), 1.0)

    def test_empty_book_zero(self):
        from main import _actionable_dust_ratio
        self.assertEqual(_actionable_dust_ratio([]), 0.0)
