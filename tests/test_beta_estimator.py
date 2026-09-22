"""Pins for intelligence/beta_estimator.py — the low-beta survival plane."""

import math

import pytest

from intelligence.beta_estimator import (
    RollingBeta,
    beta_gate_ok,
    classify,
    estimate_beta,
    size_multiplier,
)


def _series(n: int, slope: float, base: float = 0.001) -> tuple:
    """symbol = slope * btc exactly (noise-free); btc a varied small series."""
    btc = [base * (1.0 if i % 2 == 0 else -1.0) * (1 + (i % 7) * 0.1) for i in range(n)]
    sym = [slope * b for b in btc]
    return sym, btc


# ---------------------------------------------------------------------------
# estimate_beta — OLS correctness
# ---------------------------------------------------------------------------

class TestEstimateBeta:
    def test_known_slope_half(self):
        sym, btc = _series(60, 0.5)
        beta = estimate_beta(sym, btc)
        assert beta == pytest.approx(0.5, abs=1e-9)

    def test_known_slope_one(self):
        sym, btc = _series(60, 1.0)
        assert estimate_beta(sym, btc) == pytest.approx(1.0, abs=1e-9)

    def test_known_slope_above_one(self):
        sym, btc = _series(60, 1.4)
        assert estimate_beta(sym, btc) == pytest.approx(1.4, abs=1e-9)

    def test_zero_btc_variance_returns_none(self):
        sym = [0.001] * 50
        btc = [0.002] * 50  # flat BTC tape carries no beta information
        assert estimate_beta(sym, btc) is None

    def test_below_min_samples_returns_none(self):
        sym, btc = _series(19, 0.5)
        assert estimate_beta(sym, btc, min_samples=20) is None

    def test_at_min_samples_estimates(self):
        sym, btc = _series(20, 0.5)
        assert estimate_beta(sym, btc, min_samples=20) == pytest.approx(0.5, abs=1e-9)

    def test_custom_min_samples(self):
        sym, btc = _series(10, 0.5)
        assert estimate_beta(sym, btc, min_samples=5) == pytest.approx(0.5, abs=1e-9)

    def test_winsorization_kills_liquidation_wick(self):
        # True slope 0.5 on quiet tape; one 20-sigma paired wick would,
        # unclipped, dominate cov/var and drag beta toward the wick slope.
        # Winsorization must strip its dominance: residual bias of one
        # clipped point on a 200-sample window is a few percent.
        n = 200
        sym, btc = _series(n, 0.5)
        btc_std = (sum((b - sum(btc) / n) ** 2 for b in btc) / n) ** 0.5
        wick_btc = 20.0 * btc_std
        wick_sym = 5.0 * wick_btc  # wick slope 5.0, far from true 0.5
        sym_w = sym + [wick_sym]
        btc_w = btc + [wick_btc]
        clipped = estimate_beta(sym_w, btc_w)
        assert clipped == pytest.approx(0.5, abs=0.10)
        assert classify(clipped) == "low"  # stays in the true bucket
        # Sanity: the unclipped OLS is dominated by the wick.
        raw = estimate_beta(sym_w, btc_w, winsor_sigma=0.0)
        assert raw is not None and raw > 1.5


# ---------------------------------------------------------------------------
# RollingBeta — state, shrinkage, snapshot
# ---------------------------------------------------------------------------

class TestRollingBeta:
    def _feed(self, rb: RollingBeta, symbol: str, n: int, slope: float):
        sym, btc = _series(n, slope)
        for i, (s, b) in enumerate(zip(sym, btc)):
            rb.update(symbol, s, b, ts=float(i))

    def test_no_data_beta_none(self):
        rb = RollingBeta()
        assert rb.beta("XMR-USD") is None

    def test_below_min_samples_beta_none(self):
        rb = RollingBeta(min_samples=20)
        self._feed(rb, "XMR-USD", 19, 0.45)
        assert rb.beta("XMR-USD") is None

    def test_shrinkage_halfway_at_n_equals_k(self):
        # n == k == 20 -> shrunk = (raw + prior) / 2
        rb = RollingBeta(min_samples=20, shrink_k=20.0, prior=1.0)
        self._feed(rb, "XMR-USD", 20, 0.4)
        assert rb.beta("XMR-USD") == pytest.approx((0.4 + 1.0) / 2, abs=1e-9)

    def test_large_n_raw_dominates(self):
        # n=160, k=20 -> weight on raw = 160/180; shrunk near raw 0.4.
        rb = RollingBeta(window=200, min_samples=20, shrink_k=20.0, prior=1.0)
        self._feed(rb, "SEI-USD", 160, 0.4)
        expected = (160 * 0.4 + 20.0 * 1.0) / 180.0
        assert rb.beta("SEI-USD") == pytest.approx(expected, abs=1e-9)
        assert abs(rb.beta("SEI-USD") - 0.4) < 0.08  # raw-dominant

    def test_window_bounded(self):
        rb = RollingBeta(window=30, min_samples=5)
        self._feed(rb, "ENA-USD", 100, 0.6)
        assert rb.snapshot("ENA-USD")["n"] == 30

    def test_zero_variance_btc_beta_none(self):
        rb = RollingBeta(min_samples=5)
        for i in range(30):
            rb.update("FLAT-USD", 0.001, 0.002, ts=float(i))
        assert rb.beta("FLAT-USD") is None

    def test_nan_updates_ignored(self):
        rb = RollingBeta(min_samples=5)
        rb.update("XMR-USD", float("nan"), 0.001, ts=1.0)
        rb.update("XMR-USD", 0.001, float("nan"), ts=2.0)
        assert rb.snapshot("XMR-USD")["n"] == 0

    def test_symbols_isolated(self):
        rb = RollingBeta(min_samples=20)
        self._feed(rb, "XMR-USD", 25, 0.45)
        assert rb.beta("ETH-USD") is None
        assert rb.beta("XMR-USD") is not None

    def test_snapshot_fields_and_clock(self):
        rb = RollingBeta(min_samples=20, shrink_k=20.0, clock=lambda: 123.5)
        self._feed(rb, "XMR-USD", 20, 0.4)
        snap = rb.snapshot("XMR-USD")
        assert snap["symbol"] == "XMR-USD"
        assert snap["raw"] == pytest.approx(0.4, abs=1e-9)
        assert snap["shrunk"] == pytest.approx(0.7, abs=1e-9)
        assert snap["n"] == 20
        assert snap["window"] == 168
        assert snap["shrink_k"] == 20.0
        assert snap["prior"] == 1.0
        assert snap["class"] == "mid"
        assert snap["ts"] == 123.5

    def test_snapshot_unknown_when_no_raw(self):
        rb = RollingBeta()
        snap = rb.snapshot("NONE-USD")
        assert snap["raw"] is None and snap["shrunk"] is None
        assert snap["class"] == "unknown"
        assert snap["n"] == 0


# ---------------------------------------------------------------------------
# classify — bucket edges
# ---------------------------------------------------------------------------

class TestClassify:
    def test_low(self):
        assert classify(0.45) == "low"
        assert classify(0.6499) == "low"

    def test_edge_065_is_mid(self):
        assert classify(0.65) == "mid"

    def test_mid(self):
        assert classify(0.70) == "mid"
        assert classify(0.80) == "mid"  # exactly 0.80 is not > 0.80

    def test_edge_above_080_is_high(self):
        assert classify(0.8001) == "high"
        assert classify(0.95) == "high"

    def test_none_is_unknown(self):
        assert classify(None) == "unknown"

    def test_injectable_thresholds(self):
        assert classify(0.55, low_max=0.50, high_min=0.60) == "mid"
        assert classify(0.55, low_max=0.60) == "low"


# ---------------------------------------------------------------------------
# size_multiplier — the Governor's formula
# ---------------------------------------------------------------------------

class TestSizeMultiplier:
    def test_beta_045_hits_cap(self):
        # 1 + (0.70 - 0.45) * 2 = 1.5
        assert size_multiplier(0.45) == pytest.approx(1.5)

    def test_beta_065(self):
        # 1 + (0.70 - 0.65) * 2 = 1.1
        assert size_multiplier(0.65) == pytest.approx(1.1)

    def test_beta_070_neutral(self):
        assert size_multiplier(0.70) == pytest.approx(1.0)

    def test_beta_above_pivot_neutral(self):
        assert size_multiplier(0.95) == pytest.approx(1.0)

    def test_none_beta_fail_open_neutral(self):
        assert size_multiplier(None) == 1.0

    def test_cap_enforced_at_beta_zero(self):
        # 1 + 0.70*2 = 2.4 -> clamped to cap 1.5
        assert size_multiplier(0.0) == pytest.approx(1.5)

    def test_injectable_constants(self):
        assert size_multiplier(0.60, pivot=0.80, slope=1.0, cap=3.0) == pytest.approx(1.2)
        assert size_multiplier(0.0, pivot=0.70, slope=2.0, cap=1.2) == pytest.approx(1.2)


# ---------------------------------------------------------------------------
# beta_gate_ok — abstain on missing data
# ---------------------------------------------------------------------------

class TestBetaGateOk:
    def test_none_abstains(self):
        assert beta_gate_ok(None) is None

    def test_low_beta_passes(self):
        assert beta_gate_ok(0.60) is True
        assert beta_gate_ok(0.70) is True  # at threshold: passes

    def test_high_beta_blocks(self):
        assert beta_gate_ok(0.7001) is False
        assert beta_gate_ok(0.95) is False

    def test_injectable_threshold(self):
        assert beta_gate_ok(0.80, max_beta=0.85) is True
