"""Whale Absorption Signal pins (2026-08-30): the state machine IS the
anti-falling-knife doctrine — arm on the forced window, demand stabilization,
classify true (whale identity) vs footprint-only, knife resets, noise
bursts never arm. SHADOW-ONLY: nothing here touches live orders."""
from types import SimpleNamespace

import pytest

from intelligence.whale_absorption import (WhaleAbsorption, thesis_band,
                                           _STAB_WINDOW_S)


class _Harness:
    """Injected-callable rig with a manual clock."""

    def __init__(self, phase="quiet", z=0.0, liq_dir="none", forced=0.0,
                 price=100.0, depth=50_000.0, flows=()):
        self.t = 1_000.0
        self.phase, self.z, self.liq_dir = phase, z, liq_dir
        self.forced, self.price, self.depth = forced, price, depth
        self.flows = list(flows)
        self.was = WhaleAbsorption(
            liq_snap_fn=lambda s: SimpleNamespace(
                phase=self.phase, zscore=self.z, last_direction=self.liq_dir),
            forced_notional_fn=lambda s, d, w: self.forced,
            whale_flows_fn=lambda s, d: self.flows,
            book_depth_fn=lambda s, side: self.depth,
            price_fn=lambda s: self.price,
            price_change_fn=lambda s, w: 0.0,
            min_forced_notional_usd=250_000.0,
            time_fn=lambda: self.t)

    def force_sell_window(self):
        self.phase, self.z, self.liq_dir = "expansion", 3.1, "bearish"

    def stop_forcing(self, forced=500_000.0):
        self.phase, self.z = "aftermath", 0.4
        self.forced = forced


class TestArming:
    def test_armed_on_forced_window(self):
        h = _Harness()
        assert h.was.tick("BTC-USD") is None          # IDLE, quiet
        h.force_sell_window()
        assert h.was.tick("BTC-USD") is None          # → ARMED
        assert h.was._state["BTC-USD"]["phase"] == "ARMED"
        assert h.was._state["BTC-USD"]["absorb_dir"] == "long"

    def test_bullish_liqs_arm_short_absorption(self):
        h = _Harness()
        h.phase, h.z, h.liq_dir = "exhaustion", 2.7, "bullish"
        h.was.tick("ETH-USD")
        assert h.was._state["ETH-USD"]["absorb_dir"] == "short"

    def test_weak_z_does_not_arm(self):
        h = _Harness(phase="expansion", z=1.5, liq_dir="bearish")
        h.was.tick("BTC-USD")
        assert h.was._state["BTC-USD"]["phase"] == "IDLE"

    def test_noise_burst_resets_without_candidate(self):
        h = _Harness()
        h.force_sell_window()
        h.was.tick("BTC-USD")                          # ARMED
        h.stop_forcing(forced=100_000.0)               # below materiality
        assert h.was.tick("BTC-USD") is None
        assert h.was._state["BTC-USD"]["phase"] == "IDLE"


class TestStabilization:
    def _armed(self, h):
        h.force_sell_window()
        h.was.tick("BTC-USD")
        h.stop_forcing()
        h.was.tick("BTC-USD")                          # → STABILIZING
        assert h.was._state["BTC-USD"]["phase"] == "STABILIZING"

    def test_true_absorption_with_identity(self):
        flows = [{"venue": "aster", "quality": "direct",
                  "notional_delta_usd": 75_000.0}]
        h = _Harness(flows=flows)
        self._armed(h)
        h.depth = 65_000.0                             # wall refilled ×1.3
        h.t += _STAB_WINDOW_S + 1
        ev = h.was.tick("BTC-USD")
        assert ev is not None
        f = ev["features"]
        assert f["class"] == "true_absorption"
        assert ev["direction"] == "long"               # absorbing forced sells
        assert f["absorption_ratio"] == pytest.approx(0.15)   # feature, no gate
        assert f["forced_notional_usd"] == 500_000.0
        assert f["whale_notional_usd"] == 75_000.0
        assert f["replenishment_ratio"] == pytest.approx(1.3)
        assert f["impact_efficiency_per_1m"] is not None
        assert f["n_whale_flows"] == 1
        assert ev["confidence"] is None                # learned — shadow
        assert "thesis_half_life" in f
        # cooldown armed after emission
        assert h.was._state["BTC-USD"]["phase"] == "COOLDOWN"

    def test_footprint_only_class(self):
        h = _Harness(flows=[])                         # no whale identity
        self._armed(h)
        h.depth = 80_000.0                             # refill ×1.6 ≥ 1.2
        h.t += _STAB_WINDOW_S + 1
        ev = h.was.tick("BTC-USD")
        assert ev is not None
        assert ev["features"]["class"] == "footprint_only"
        assert ev["features"]["absorption_ratio"] is None

    def test_no_evidence_no_emission(self):
        h = _Harness(flows=[])
        self._armed(h)
        h.depth = 50_000.0                             # no refill (1.0)
        h.t += _STAB_WINDOW_S + 1
        assert h.was.tick("BTC-USD") is None
        assert h.was._state["BTC-USD"]["phase"] == "COOLDOWN"

    def test_falling_knife_fails_stabilization(self):
        h = _Harness(flows=[{"venue": "aster",
                             "notional_delta_usd": 60_000.0}])
        self._armed(h)
        h.t += 60
        h.price = 99.0                                 # −1.0% adverse > 0.4%
        assert h.was.tick("BTC-USD") is None
        st = h.was._state["BTC-USD"]
        assert st["phase"] == "COOLDOWN"
        assert st.get("false_absorption") is True

    def test_stabilization_window_must_elapse(self):
        h = _Harness(flows=[{"venue": "aster",
                             "notional_delta_usd": 60_000.0}])
        self._armed(h)
        h.t += _STAB_WINDOW_S - 10                     # too early
        assert h.was.tick("BTC-USD") is None
        assert h.was._state["BTC-USD"]["phase"] == "STABILIZING"

    def test_cooldown_expires_to_idle(self):
        h = _Harness(flows=[])
        self._armed(h)
        h.depth = 80_000.0
        h.t += _STAB_WINDOW_S + 1
        h.was.tick("BTC-USD")
        h.t += 1801
        assert h.was.tick("BTC-USD") is None
        assert h.was._state["BTC-USD"]["phase"] == "IDLE"

    def test_tick_never_raises(self):
        h = _Harness()
        h.was._snap = lambda s: (_ for _ in ()).throw(RuntimeError("x"))
        assert h.was.tick("BTC-USD") is None


class TestArmRelaxation:
    """2026-10-21 (Governor msg-975): the tick-time z floor relaxes via the
    arm_min_z constructor knob — the phase state already gated z>=2.5/4.0 on
    entry. Legacy default 2.5 = bit-for-bit."""

    def _relaxed(self, phase, z, liq_dir):
        h = _Harness(phase=phase, z=z, liq_dir=liq_dir)
        h.was._arm_min_z = 1.0   # as main.py wires it via config knob
        return h

    def test_relaxed_arm_fires_at_trigger_level_z(self):
        # The starvation signature: phase=expansion, fresh z collapsed below
        # 2.5 (post-cascade baseline contamination) — arms under the relaxed
        # floor, IDLE under legacy.
        h = self._relaxed("expansion", 1.2, "bearish")
        h.was.tick("BTC-USD")
        assert h.was._state["BTC-USD"]["phase"] == "ARMED"
        assert h.was._state["BTC-USD"]["absorb_dir"] == "long"

    def test_legacy_default_still_requires_2_5(self):
        h = _Harness(phase="expansion", z=1.2, liq_dir="bearish")
        h.was.tick("BTC-USD")                      # default arm_min_z=2.5
        assert h.was._state["BTC-USD"]["phase"] == "IDLE"

    def test_relaxed_floor_still_excludes_baseline_noise(self):
        h = self._relaxed("expansion", 0.4, "bearish")
        h.was.tick("BTC-USD")
        assert h.was._state["BTC-USD"]["phase"] == "IDLE"

    def test_constructor_knob_binds(self):
        was = WhaleAbsorption(
            liq_snap_fn=lambda s: SimpleNamespace(
                phase="expansion", zscore=1.5, last_direction="bearish"),
            forced_notional_fn=lambda s, d, w: 0.0,
            whale_flows_fn=lambda s, d: [],
            book_depth_fn=lambda s, side: None,
            price_fn=lambda s: 100.0,
            price_change_fn=lambda s, w: 0.0,
            arm_min_z=2.5)
        was.tick("SOL-USD")
        assert was._state["SOL-USD"]["phase"] == "IDLE"


class TestThesisBands:
    def test_bands(self):
        assert thesis_band(0) == "full"
        assert thesis_band(899) == "full"
        assert thesis_band(901) == "decay"
        assert thesis_band(3599) == "decay"
        assert thesis_band(3601) == "reduced"
        assert thesis_band(10799) == "reduced"
        assert thesis_band(10801) == "stale"
        assert thesis_band(99999) == "stale"
