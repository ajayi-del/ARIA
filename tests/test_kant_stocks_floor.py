"""Stocks coherence floor pins (Governor 2026-10-17: "redduce cooherence
for stocks... admissioon matched 2.05 =3.0 crypto").

Equity-class symbols (ASSET_CONFIG category in equity / equity_index /
index_equity) get their Kant gate-2 floor capped at stocks_coherence_min
(default 2.05, env STOCKS_COHERENCE_MIN). Measured distributions: stocks
median 2.28 / max 3.96 vs crypto median 4.05 — a 3.0 floor admits 66.3%
of crypto signals but 6.2% of equity; 2.05 admission-matches the cohort.
Supersedes the 2026-09-23 halving (1.5), which admitted the cohort
unfairly through the relaxed path while SYMBOL_MIN_COHERENCE 6.0 blindly
blocked it downstream. Commodity (SILVER/COPPER/CL/XAUT/PAXG) and crypto
keep the 3.0 floor. The cap rides ON TOP of any caller relief (min of the
two) — relieved, never waived.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import execution.kant_gate as kg  # noqa: E402
from execution.kant_gate import KantGate  # noqa: E402

EQUITY = "TSLA-USD"          # category: equity
EQUITY_INDEX = "SPCX-USD"    # category: equity_index
COMMODITY = "SILVER-USD"     # category: commodity — Governor said STOCKS
CRYPTO = "BTC-USD"           # category: large_cap


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch):
    monkeypatch.delenv("STOCKS_COHERENCE_MIN", raising=False)
    monkeypatch.delenv("TRADFI_COHERENCE_SHADOW_FLOOR", raising=False)
    kg._ASSET_CFG_CACHE = None
    kg._STOCKS_FLOOR_CACHE = None
    yield
    kg._ASSET_CFG_CACHE = None
    kg._STOCKS_FLOOR_CACHE = None


def _check(symbol, coherence, gate=None):
    gate = gate or KantGate()
    return gate.check(
        symbol=symbol, direction="long", coherence=coherence,
        rr_ratio=4.0, balance=1000.0, regime_state=None,
    )


class TestStocksFloor:
    def test_stocks_pass_above_2_05(self):
        assert _check(EQUITY, 2.1).allowed is True
        assert _check(EQUITY_INDEX, 2.1).allowed is True

    def test_stocks_reject_below_2_05(self):
        v = _check(EQUITY, 2.0)
        assert v.allowed is False
        assert "coherence_below_2.05" in v.reason
        v = _check(EQUITY_INDEX, 2.04)
        assert v.allowed is False
        assert "coherence_below_2.05" in v.reason

    def test_commodity_floor_unchanged_at_3(self):
        # Governor said stocks — SILVER (commodity) keeps the 3.0 floor.
        assert _check(COMMODITY, 2.9).allowed is False
        assert "coherence_below_3.0" in _check(COMMODITY, 2.9).reason
        assert _check(COMMODITY, 3.1).allowed is True

    def test_crypto_floor_unchanged_at_3(self):
        assert _check(CRYPTO, 2.9).allowed is False
        assert "coherence_below_3.0" in _check(CRYPTO, 2.9).reason
        assert _check(CRYPTO, 3.1).allowed is True

    def test_caller_relief_composes_as_min(self):
        # Caller relief (trend-day, floor 2.5) + stocks cap 2.05 → 2.05.
        gate = KantGate()
        v = gate.check(symbol=EQUITY, direction="long", coherence=2.1,
                       rr_ratio=4.0, balance=1000.0, regime_state=None,
                       coherence_minimum=2.5)
        assert v.allowed is True
        # A caller floor BELOW the stocks cap keeps the tighter floor.
        gate = KantGate()
        v = gate.check(symbol=EQUITY, direction="long", coherence=1.2,
                       rr_ratio=4.0, balance=1000.0, regime_state=None,
                       coherence_minimum=1.0)
        assert v.allowed is True

    def test_knob_env_override(self, monkeypatch):
        monkeypatch.setenv("STOCKS_COHERENCE_MIN", "2.0")
        assert _check(EQUITY, 1.9).allowed is False
        assert "coherence_below_2.0" in _check(EQUITY, 1.9).reason
        assert _check(EQUITY, 2.1).allowed is True
        # Crypto unaffected by the stocks knob.
        assert "coherence_below_3.0" in _check(CRYPTO, 2.9).reason

    def test_class_predicates(self):
        assert kg._is_stocks_class(EQUITY) is True
        assert kg._is_stocks_class(EQUITY_INDEX) is True
        assert kg._is_stocks_class("USTECH100-USD") is True
        assert kg._is_stocks_class(COMMODITY) is False
        assert kg._is_stocks_class("XAUT-USD") is False
        assert kg._is_stocks_class("CL-USD") is False
        assert kg._is_stocks_class(CRYPTO) is False
        assert kg._is_stocks_class("NOT-A-SYMBOL") is False

    def test_tradfi_shadow_band_open_below_stocks_floor(self):
        # Governor 2026-10-17: with the live stocks floor at 2.05 the
        # shadow floor (default 1.5) sits BELOW it — the [1.5, 2.05) band
        # scores the just-excluded equity cohort counterfactually.
        assert kg._tradfi_shadow_floor() < kg._stocks_floor()
