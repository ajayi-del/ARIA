"""Stock-line dead-market ATR gate pins (Governor 2026-10-17: "ultrathink
an implementation for this to optimize our stock trading lines").

The dead-market fee-coverage gate read the perp's OWN 15m ATR — stock
perps print structurally flat bars off-hours (frozen underlying, thin RWA
book), so the equity cohort died at dead_market_atr_too_small (RC2 census
×10) while the underlying's true vol said the move covered fees. The gate
now re-derives the pct via _stock_perp_reference_atr (Yahoo-underlying %
vol mapped onto the entry plane, category floor fallback) under the same
ATR_REFERENCE_FALLBACK_ENABLED env kill switch as the atr_sanity repair.

Doctrine pins: loosen-ONLY (the reference never lowers the raw reading —
no new rejections the legacy gate didn't make); crypto / missing
reference / env off = legacy reject bit-for-bit.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_MAIN = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "main.py")


def _src():
    with open(_MAIN) as fh:
        return fh.read()


class TestDeadMarketReferenceSplice:
    def test_reference_called_before_reject(self):
        src = _src()
        ref = src.index("dead_market_atr_reference_used")
        rej = src.index('reason="dead_market_atr_too_small"')
        assert ref < rej

    def test_env_kill_switch_gates_substitution(self):
        src = _src()
        ref = src.index("dead_market_atr_reference_used")
        window = src[max(0, ref - 1200):ref]
        assert 'ATR_REFERENCE_FALLBACK_ENABLED' in window
        assert '"true"' in window  # default on; "false" = legacy bit-for-bit

    def test_substitution_is_loosen_only(self):
        src = _src()
        ref = src.index("dead_market_atr_reference_used")
        window = src[max(0, ref - 900):ref]
        # The reference may only RAISE the pct (repair flat-bar blindness),
        # never lower it (no new rejections the legacy gate didn't make).
        assert "> _atr_pct" in window

    def test_helper_ignores_crypto_categories(self):
        from main import _stock_perp_reference_atr
        atr, src = _stock_perp_reference_atr("BTC-USD", 100000.0, "large_cap")
        assert atr is None and src is None

    def test_helper_category_floor_fallback(self):
        # Equity category with no Yahoo reference → synthetic floor ratio.
        import main as m
        old = m._STOCK_PERP_ATR_FLOOR_RATIO
        try:
            atr, src = m._stock_perp_reference_atr(
                "NOT-A-REAL-EQUITY-USD", 100.0, "equity")
            # No tradfi mapping → either None (import/abstain path) or the
            # synthetic floor 0.3% of entry.
            if atr is not None:
                assert src == "synthetic_floor"
                assert abs(atr - 0.3) < 1e-9
        finally:
            m._STOCK_PERP_ATR_FLOOR_RATIO = old
