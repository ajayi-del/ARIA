"""TradFi coherence shadow floor (Governor 2026-09-10; re-encoded
2026-10-17 for the admission-matched stocks floor).

Measured on the coherence_tier_reject stream: tradfi/commodity coherence
p50 2.28-2.63, p99 ~3.0, max EXACTLY 3.0 across all 12 symbols — the 3.0
Kant floor sits at the top of the input's support (5th threshold-outside-
support instance). Shadow-first: would-be passes in [shadow_floor,
live_floor) emit signal_would_pass_tradfi_floor (shadow gate
tradfi_floor_soft, scored from birth).

2026-10-17 re-encode justification (Governor: "admissioon matched 2.05
=3.0 crypto"): the stocks-class live floor is now 2.05 (was 1.5), and the
shadow default moved 2.5 → 1.5 so the band stays BELOW the live floor:
stocks band [1.5, 2.05) scores the just-excluded equity cohort; commodity
keeps live 3.0 with band [1.5, 3.0). Every in-band case must STILL
reject — shadow scores, never admits.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from execution import kant_gate  # noqa: E402
from execution.kant_gate import KantGate  # noqa: E402


class _RecLog:
    def __init__(self):
        self.events = []

    def info(self, event, **kw):
        self.events.append((event, kw))

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _run(symbol, coherence, monkeypatch=None, env_floor=None,
         coherence_minimum=None):
    rec = _RecLog()
    old_log = kant_gate.log
    old_env = os.environ.get("TRADFI_COHERENCE_SHADOW_FLOOR")
    old_cfg = kant_gate._ASSET_CFG_CACHE
    old_sfl = kant_gate._STOCKS_FLOOR_CACHE
    kant_gate.log = rec
    kant_gate._ASSET_CFG_CACHE = None
    kant_gate._STOCKS_FLOOR_CACHE = None
    if env_floor is None:
        os.environ.pop("TRADFI_COHERENCE_SHADOW_FLOOR", None)
    else:
        os.environ["TRADFI_COHERENCE_SHADOW_FLOOR"] = env_floor
    try:
        v = KantGate().check(symbol, "long", coherence=coherence, rr_ratio=3.5,
                             balance=500.0, regime_state=None,
                             coherence_minimum=coherence_minimum)
    finally:
        kant_gate.log = old_log
        kant_gate._ASSET_CFG_CACHE = old_cfg
        kant_gate._STOCKS_FLOOR_CACHE = old_sfl
        if old_env is None:
            os.environ.pop("TRADFI_COHERENCE_SHADOW_FLOOR", None)
        else:
            os.environ["TRADFI_COHERENCE_SHADOW_FLOOR"] = old_env
    return v, [e for e, _ in rec.events], dict(rec.events[0][1]) if rec.events else {}


class TestShadowBand:
    def test_stocks_in_band_emits_and_still_rejects(self):
        # Stocks live floor 2.05, shadow 1.5: coherence 1.8 is in [1.5, 2.05).
        v, evs, kw = _run("TSLA-USD", 1.8)
        assert v.allowed is False and v.log_event == "coherence_tier_reject"
        assert evs == ["signal_would_pass_tradfi_floor"]
        assert kw["symbol"] == "TSLA-USD" and kw["direction"] == "long"
        assert kw["coherence"] == 1.8 and kw["threshold"] == 2.05
        assert kw["shadow_floor"] == 1.5

    def test_commodity_band_live_floor_3(self):
        # Commodity keeps the 3.0 live floor; shadow band [1.5, 3.0).
        for sym in ("CL-USD", "XAUT-USD"):
            v, evs, kw = _run(sym, 2.8)
            assert v.allowed is False, sym
            assert evs == ["signal_would_pass_tradfi_floor"], sym
            assert kw["threshold"] == 3.0 and kw["shadow_floor"] == 1.5, sym

    def test_stocks_above_live_floor_passes_no_emit(self):
        for sym in ("TSLA-USD", "SPCX-USD"):
            v, evs, _ = _run(sym, 2.8)
            assert v.allowed is True, sym
            assert evs == [], sym

    def test_crypto_never_emits(self):
        v, evs, _ = _run("BTC-USD", 2.7)
        assert v.allowed is False and evs == []

    def test_below_shadow_floor_no_emit(self):
        v, evs, _ = _run("TSLA-USD", 1.4)
        assert v.allowed is False and evs == []
        v, evs, _ = _run("CL-USD", 1.4)
        assert v.allowed is False and evs == []

    def test_env_kill_switch_empty_band(self):
        # shadow floor >= live floor → band empty → feature off
        v, evs, _ = _run("CL-USD", 2.7, env_floor="3.0")
        assert v.allowed is False and evs == []
        v, evs, _ = _run("TSLA-USD", 1.8, env_floor="2.05")
        assert v.allowed is False and evs == []

    def test_env_floor_widens_band(self):
        v, evs, kw = _run("TSLA-USD", 1.2, env_floor="1.0")
        assert v.allowed is False
        assert evs == ["signal_would_pass_tradfi_floor"]
        assert kw["shadow_floor"] == 1.0

    def test_relieved_floor_narrows_band(self):
        # Trend-day relief to 2.5 binds commodity (no stocks cap): band
        # [1.5, 2.5) — CL at 2.2 emits with the relieved threshold.
        v, evs, kw = _run("CL-USD", 2.2, coherence_minimum=2.5)
        assert v.allowed is False
        assert evs == ["signal_would_pass_tradfi_floor"]
        assert kw["threshold"] == 2.5 and kw["shadow_floor"] == 1.5
        # Relief never waives the stocks cap: TSLA floor stays 2.05.
        v, evs, kw = _run("TSLA-USD", 1.8, coherence_minimum=2.5)
        assert v.allowed is False
        assert evs == ["signal_would_pass_tradfi_floor"]
        assert kw["threshold"] == 2.05


class TestSymbolClass:
    def test_categories(self):
        assert kant_gate._is_tradfi_class("TSLA-USD") is True
        assert kant_gate._is_tradfi_class("CL-USD") is True
        assert kant_gate._is_tradfi_class("XAUT-USD") is True
        assert kant_gate._is_tradfi_class("SPCX-USD") is True
        assert kant_gate._is_tradfi_class("BTC-USD") is False
        # COIN-USD is config-categorized "crypto" (trades as a crypto proxy)
        assert kant_gate._is_tradfi_class("COIN-USD") is False
        assert kant_gate._is_tradfi_class("NOT-A-SYMBOL") is False


class TestRegistry:
    def test_shadow_gate_registered(self):
        from intelligence.shadow_journal import REJECTION_EVENTS
        assert REJECTION_EVENTS["signal_would_pass_tradfi_floor"] == "tradfi_floor_soft"

    def test_gate_value_reads_coherence(self):
        src = open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "intelligence", "shadow_journal.py")).read()
        i = src.index('gate in ("coherence_floor"')
        assert "tradfi_floor_soft" in src[i:i + 200]
