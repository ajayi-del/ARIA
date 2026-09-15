"""Pins for intelligence/hurst_regime.py — the Hurst/regime classification
brain (Governor spec 2026-09-15, SHADOW-from-birth).

Hurst pins use fixed seeds on synthetic series with KNOWN character:
  - strong trending random-walk-with-drift  -> H > 0.55
  - mean-reverting AR(1) negative-autocorr  -> H < 0.45
  - iid white noise closes                  -> H in [0.45, 0.55]
The estimator (raw-series R/S, Anis-Lloyd corrected — see module docstring)
was verified stable on seeds 1-10 before these seeds were pinned.
"""
import math
import os
import random

from data.candle_buffer import Candle
from intelligence.hurst_regime import (
    RegimeState, classify_regime, compute_state, hurst_exponent,
    measurement_enabled, should_fire, strategy_class_of,
)

H_MS = 3_600_000
BAR = 4 * H_MS
T0 = 1_758_000_000_000


def _trend_closes(seed=4, n=600, drift=0.002, sig=0.0005):
    r = random.Random(seed)
    lp = math.log(100.0)
    out = []
    for _ in range(n):
        lp += drift + sig * r.gauss(0, 1)
        out.append(math.exp(lp))
    return out


def _mr_closes(seed=5, n=600, phi=-0.8, sig=0.01):
    r = random.Random(seed)
    x = 0.0
    out = []
    for _ in range(n):
        x = phi * x + sig * r.gauss(0, 1)
        out.append(100.0 * math.exp(x))
    return out


def _noise_closes(seed=2, n=600, sig=0.01):
    r = random.Random(seed)
    return [100.0 * math.exp(sig * r.gauss(0, 1)) for _ in range(n)]


def _candles_from_closes(closes):
    # two-phase bar width: wide early, narrow in the last 20 bars -> the
    # realized-vol rank reads LOW, so classify follows the Hurst leg.
    n = len(closes)
    out = []
    for i, c in enumerate(closes):
        band = 0.008 if i < n - 20 else 0.002
        t = T0 + i * BAR
        out.append(Candle(open_time=t, open=c, high=c * (1 + band),
                          low=c * (1 - band), close=c, volume=1.0,
                          close_time=t + BAR - 1))
    return out


class TestHurstKnownCharacter:
    def test_trending_random_walk_with_drift_reads_persistent(self):
        h = hurst_exponent(_trend_closes())
        assert h is not None and h > 0.55

    def test_mean_reverting_ar1_negative_autocorr_reads_antipersistent(self):
        h = hurst_exponent(_mr_closes())
        assert h is not None and h < 0.45

    def test_white_noise_lands_in_random_band(self):
        h = hurst_exponent(_noise_closes())
        assert h is not None and 0.45 <= h <= 0.55

    def test_thin_input_abstains(self):
        assert hurst_exponent(_trend_closes()[:99]) is None
        assert hurst_exponent([]) is None
        assert hurst_exponent(None) is None

    def test_degenerate_input_abstains(self):
        assert hurst_exponent([100.0] * 200) is None      # zero variance
        assert hurst_exponent([100.0, -1.0] * 100) is None  # non-positive
        assert hurst_exponent(["x"] * 200) is None          # non-numeric

    def test_min_bars_floor_produces_estimate_at_100(self):
        # the Governor's floor: exactly 100 bars must still classify
        assert hurst_exponent(_trend_closes()[:100]) is not None


class TestClassifyMatrix:
    def test_chaotic_kills_everything_above_vol_75(self):
        assert classify_regime(0.9, 75.0) == "chaotic"
        assert classify_regime(0.1, 99.0) == "chaotic"
        assert classify_regime(0.5, 75.0) == "chaotic"

    def test_trending(self):
        assert classify_regime(0.7, 30.0) == "trending"

    def test_mean_reverting(self):
        assert classify_regime(0.3, 20.0) == "mean_reverting"

    def test_random_otherwise(self):
        assert classify_regime(0.5, 30.0) == "random"       # dead zone
        assert classify_regime(0.7, 70.0) == "random"       # hot but not chaotic
        assert classify_regime(0.3, 66.0) == "random"       # vol 65..75 -> random

    def test_unknown_on_none_axes(self):
        assert classify_regime(None, 30.0) == "unknown"
        assert classify_regime(0.7, None) == "unknown"
        assert classify_regime(None, None) == "unknown"


class TestShouldFireMatrix:
    def test_trending_row(self):
        assert should_fire("trending", "momentum") is True
        assert should_fire("trending", "swing") is True
        assert should_fire("trending", "meanrev") is False
        assert should_fire("trending", "carry") is True

    def test_mean_reverting_row(self):
        assert should_fire("mean_reverting", "meanrev") is True
        assert should_fire("mean_reverting", "carry") is True
        assert should_fire("mean_reverting", "momentum") is False
        assert should_fire("mean_reverting", "swing") is False

    def test_random_row(self):
        assert should_fire("random", "carry") is True
        assert should_fire("random", "momentum") is False
        assert should_fire("random", "meanrev") is False
        assert should_fire("random", "swing") is False

    def test_chaotic_row_kills_all(self):
        for cls in ("momentum", "meanrev", "carry", "swing"):
            assert should_fire("chaotic", cls) is False

    def test_unknown_regime_fails_open(self):
        for cls in ("momentum", "meanrev", "carry", "swing", None, "garbage"):
            assert should_fire("unknown", cls) is True
        assert should_fire(None, "momentum") is True
        assert should_fire("nonsense", "carry") is True

    def test_unknown_strategy_class_abstains(self):
        # unmapped class abstains — even under chaotic (the kill matrix vets
        # only the four known classes; an unmapped class is never vetoed).
        assert should_fire("trending", None) is True
        assert should_fire("mean_reverting", "unmapped") is True
        assert should_fire("chaotic", "unmapped") is True


class TestStrategyClassOf:
    def test_known_mappings(self):
        assert strategy_class_of(tag="cascade_momentum") == "momentum"
        assert strategy_class_of(personality="APEX") == "momentum"
        assert strategy_class_of(tag="cascade_aftermath") == "meanrev"
        assert strategy_class_of(personality="AFTERMATH") == "meanrev"
        assert strategy_class_of(tag="funding_fade") == "carry"
        assert strategy_class_of(tag="aster_swing") == "swing"
        assert strategy_class_of(personality="S1_OI_PULLBACK") == "swing"

    def test_unmapped_abstains(self):
        assert strategy_class_of(tag="xaut_riskoff") is None
        assert strategy_class_of(personality="COIL") is None
        assert strategy_class_of() is None
        assert strategy_class_of(tag="", personality="") is None


class TestComputeState:
    def test_thin_candles_unknown(self):
        st = compute_state("BTC-USD", _candles_from_closes(_trend_closes()[:60]), T0)
        assert st.regime == "unknown"
        assert st.hurst is None
        assert st.n_bars == 60

    def test_full_candles_classified(self):
        st = compute_state("BTC-USD", _candles_from_closes(_trend_closes()), T0)
        assert isinstance(st, RegimeState)
        assert st.hurst is not None and st.hurst > 0.55
        assert st.regime == "trending"   # low vol-rank leg -> Hurst decides
        assert st.computed_at_ms == T0
        assert st.n_bars == 600

    def test_empty_and_garbage_fail_open(self):
        assert compute_state("BTC-USD", [], T0).regime == "unknown"
        assert compute_state("BTC-USD", None, T0).regime == "unknown"
        assert compute_state("", object(), T0).regime == "unknown"


class TestKillSwitch:
    def test_measurement_enabled_env_idiom(self, monkeypatch):
        assert measurement_enabled() is True
        monkeypatch.setenv("REGIME_CLASSIFY_ENABLED", "false")
        assert measurement_enabled() is False   # loop gates on this -> no writes
        monkeypatch.setenv("REGIME_CLASSIFY_ENABLED", " FALSE ")
        assert measurement_enabled() is False
        monkeypatch.setenv("REGIME_CLASSIFY_ENABLED", "true")
        assert measurement_enabled() is True

    def test_loop_gates_on_both_switches(self):
        # source pin: the supervised loop writes nothing unless BOTH the
        # config knob and the env idiom pass (measurement off = no writes).
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "main.py")).read()
        assert "regime_classify_enabled" in src
        assert "_hr_measurement_enabled()" in src
        assert "_supervise(_regime_classify_loop" in src
        assert "regime_classified" in src
        assert "regime_states.jsonl" in src
        cfg = open(os.path.join(root, "core", "config.py")).read()
        for knob in ("regime_classify_enabled", "regime_gate_live_enabled",
                     "regime_loop_interval_s", "regime_cache_ttl_s",
                     "regime_min_bars"):
            assert knob in cfg
        # enforcement must stay OFF — the knob default is False
        assert "regime_gate_live_enabled: bool = False" in cfg

    def test_shadow_gate_wiring_present(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "main.py")).read()
        assert '"regime_gate"' in src or "'regime_gate'" in src
        assert "regime_gate_would_block" in src
