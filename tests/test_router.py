"""tests/test_router.py — pins for intelligence/router.py (R1, 2026-09-28).

Governor doctrine (verbatim): "Router assigns class → class has ONE exit
doctrine. All other exit modules are UNSUBSCRIBED for that class. One
symbol = one class at a time. Assignment made ONCE at session start; cannot
change class mid-session; next-session reassessment allowed."

Conventions follow tests/test_main_fast_cycle_splice.py: behavioral pins on
the zero-I/O brain plus source pins on main.py for the closure wiring (the
three exemption splices + boot block + refresh loop). Every kill-switch
off-state must reproduce the pre-module system bit-for-bit.
"""
from __future__ import annotations

import ast
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = os.path.join(_REPO, "main.py")
_ROUTER_SRC = os.path.join(_REPO, "intelligence", "router.py")


def _main_src() -> str:
    with open(_MAIN_SRC) as fh:
        return fh.read()


def _router_src() -> str:
    with open(_ROUTER_SRC) as fh:
        return fh.read()


class _FakeParamStore:
    """Minimal set_ai_param/get_ai_param TTL store (memory/param_store.py
    idiom) — no disk, no config."""

    def __init__(self, now_fn=None):
        self._ai = {}
        self._now = now_fn or (lambda: int(time.time()))

    def set_ai_param(self, key, value, ttl_seconds=3600):
        self._ai[key] = {"value": value,
                         "expires_at": int(self._now()) + max(1, ttl_seconds)}

    def get_ai_param(self, key, default=None):
        entry = self._ai.get(key)
        if entry is None:
            return default
        if int(self._now()) >= entry.get("expires_at", 0):
            self._ai.pop(key, None)
            return default
        return entry.get("value", default)


class _BrokenParamStore:
    def get_ai_param(self, key, default=None):
        raise RuntimeError("store on fire")

    def set_ai_param(self, key, value, ttl_seconds=3600):
        raise RuntimeError("store on fire")


# ── (a) Assignment rule matrix ────────────────────────────────────────────────

class TestAssignClassRules:
    def test_campaign_fleet_wins(self):
        from intelligence.router import assign_class, CLASS_VOLUME_MAKER
        assert assign_class("SOL-USD", is_campaign_fleet_sym=True) == CLASS_VOLUME_MAKER

    def test_campaign_fleet_priority_over_whale(self):
        # Rule 1 outranks rules 2/3 even with contradictory whale evidence.
        from intelligence.router import assign_class, CLASS_VOLUME_MAKER
        assert assign_class("SOL-USD", side="short", whale_ratio=0.5,
                            is_campaign_fleet_sym=True) == CLASS_VOLUME_MAKER

    def test_whale_hold_no_side(self):
        from intelligence.router import assign_class, CLASS_STRUCTURAL_HOLD
        assert assign_class("BTC-USD", whale_ratio=1.5) == CLASS_STRUCTURAL_HOLD

    def test_whale_hold_long_side(self):
        from intelligence.router import assign_class, CLASS_STRUCTURAL_HOLD
        assert assign_class("BTC-USD", side="long", whale_ratio=2.3) == CLASS_STRUCTURAL_HOLD

    def test_whale_hold_side_gated_against_short(self):
        from intelligence.router import assign_class
        assert assign_class("BTC-USD", side="short", whale_ratio=1.5) is None

    def test_whale_short_no_side(self):
        from intelligence.router import assign_class, CLASS_STRUCTURAL_SHORT
        assert assign_class("ZEC-USD", whale_ratio=1.09) == CLASS_STRUCTURAL_SHORT

    def test_whale_short_short_side(self):
        from intelligence.router import assign_class, CLASS_STRUCTURAL_SHORT
        assert assign_class("ZEC-USD", side="short", whale_ratio=0.8) == CLASS_STRUCTURAL_SHORT

    def test_whale_short_side_gated_against_long(self):
        from intelligence.router import assign_class
        assert assign_class("ZEC-USD", side="long", whale_ratio=0.8) is None

    def test_whale_neutral_band_abstains(self):
        # 1.1 <= ratio < 1.5 matches neither rule.
        from intelligence.router import assign_class
        assert assign_class("ETH-USD", whale_ratio=1.2) is None
        assert assign_class("ETH-USD", whale_ratio=1.1) is None

    def test_no_evidence_abstains(self):
        from intelligence.router import assign_class
        assert assign_class("ETH-USD") is None
        assert assign_class("ETH-USD", side="long") is None

    def test_malformed_inputs_abstain(self):
        from intelligence.router import assign_class
        assert assign_class("", is_campaign_fleet_sym=True) is None
        assert assign_class("BTC-USD", whale_ratio="not-a-number") is None
        assert assign_class("BTC-USD", whale_ratio=object()) is None

    def test_r2_channels_accepted_not_consumed(self):
        # band_tight_pct / regime / now_ts are R2 evidence channels — R1
        # must accept and ignore them (no rule reads them).
        from intelligence.router import assign_class
        assert assign_class("BTC-USD", band_tight_pct=0.4, regime="trend",
                            now_ts=123.0) is None


# ── (b) Stickiness (one class per symbol per session) ────────────────────────

class TestStickiness:
    def test_reassignment_within_ttl_returns_existing(self):
        from intelligence.router import RouterAssignments
        ra = RouterAssignments()
        assert ra.assign("ENA-USD", "STRUCTURAL_HOLD", now_ts=1000.0,
                         ttl_s=14400.0) == "STRUCTURAL_HOLD"
        # A conflicting class inside the window loses — sticky.
        assert ra.assign("ENA-USD", "VOLUME_MAKER", now_ts=2000.0,
                         ttl_s=14400.0) == "STRUCTURAL_HOLD"
        assert ra.get("ENA-USD") == "STRUCTURAL_HOLD"

    def test_force_reassignment_wins(self):
        from intelligence.router import RouterAssignments
        ra = RouterAssignments()
        ra.assign("ENA-USD", "STRUCTURAL_HOLD", now_ts=1000.0, ttl_s=14400.0)
        assert ra.assign("ENA-USD", "VOLUME_MAKER", now_ts=2000.0,
                         ttl_s=14400.0, force=True) == "VOLUME_MAKER"
        assert ra.get("ENA-USD") == "VOLUME_MAKER"

    def test_reassignment_after_ttl_expiry(self):
        from intelligence.router import RouterAssignments
        ra = RouterAssignments()
        ra.assign("ENA-USD", "STRUCTURAL_HOLD", now_ts=1000.0, ttl_s=100.0)
        assert ra.assign("ENA-USD", "VOLUME_MAKER", now_ts=1200.0,
                         ttl_s=100.0) == "VOLUME_MAKER"

    def test_get_and_assigned_ts(self):
        from intelligence.router import RouterAssignments
        ra = RouterAssignments()
        assert ra.get("NOPE-USD") is None
        assert ra.assigned_ts("NOPE-USD") is None
        ra.assign("SOL-USD", "VOLUME_MAKER", now_ts=42.0, ttl_s=10.0)
        assert ra.get("SOL-USD") == "VOLUME_MAKER"
        assert ra.assigned_ts("SOL-USD") == 42.0
        assert ra.items() == [("SOL-USD", ("VOLUME_MAKER", 42.0))]


# ── (c) param_store publish/read round-trip ───────────────────────────────────

class TestPublishRead:
    def test_round_trip(self):
        from intelligence.router import RouterAssignments, router_class_for
        ps = _FakeParamStore()
        assert RouterAssignments.publish(ps, "VIRTUAL-USD", "VOLUME_MAKER",
                                         14400.0) is True
        assert router_class_for(ps, "VIRTUAL-USD") == "VOLUME_MAKER"
        assert router_class_for(ps, "OTHER-USD") is None

    def test_key_namespace(self):
        from intelligence.router import param_key, PARAM_KEY_PREFIX
        assert PARAM_KEY_PREFIX == "router:class:"
        assert param_key("SOL-USD") == "router:class:SOL-USD"

    def test_expired_key_reads_unassigned(self):
        now = [1000]
        from intelligence.router import RouterAssignments, router_class_for
        ps = _FakeParamStore(now_fn=lambda: now[0])
        RouterAssignments.publish(ps, "SOL-USD", "VOLUME_MAKER", 100.0)
        assert router_class_for(ps, "SOL-USD") == "VOLUME_MAKER"
        now[0] += 200  # past the TTL
        assert router_class_for(ps, "SOL-USD") is None

    def test_reader_fail_open_on_broken_store(self):
        from intelligence.router import router_class_for
        assert router_class_for(_BrokenParamStore(), "SOL-USD") is None
        assert router_class_for(None, "SOL-USD") is None

    def test_reader_rejects_malformed_value(self):
        from intelligence.router import router_class_for
        ps = _FakeParamStore()
        ps.set_ai_param("router:class:SOL-USD", {"not": "a class"}, 100)
        assert router_class_for(ps, "SOL-USD") is None
        ps.set_ai_param("router:class:SOL-USD", "NOT_A_CLASS", 100)
        assert router_class_for(ps, "SOL-USD") is None

    def test_publish_fail_open_on_broken_store(self):
        from intelligence.router import RouterAssignments
        assert RouterAssignments.publish(_BrokenParamStore(), "SOL-USD",
                                         "VOLUME_MAKER", 100.0) is False
        assert RouterAssignments.publish(None, "SOL-USD",
                                         "VOLUME_MAKER", 100.0) is False

    def test_exempt_set_contents(self):
        from intelligence.router import EXEMPT_CLASSES
        assert EXEMPT_CLASSES == frozenset(
            {"VOLUME_MAKER", "STRUCTURAL_HOLD", "STRUCTURAL_SHORT"})


# ── (d) main.py source pins: the three exemption splices + wiring ────────────

class TestMainSplices:
    def test_main_parses(self):
        ast.parse(_main_src())

    def test_guarded_import(self):
        src = _main_src()
        assert "from intelligence.router import (" in src
        assert "except ImportError:  # fail-open: pre-router system bit-for-bit" in src
        assert "_RouterAssignments = None" in src

    def test_exemption_event_at_least_three_sites(self):
        src = _main_src()
        assert src.count("router_exit_exempted") >= 3

    def test_param_key_namespace_greppable(self):
        assert "router:class:" in _main_src()
        assert "router:class:" in _router_src()

    def test_roe_ratchet_splice(self):
        src = _main_src()
        assert 'if _router_exit_exempted(_sym, "roe_ratchet"):\n                        continue' in src

    def test_conviction_decay_splice(self):
        src = _main_src()
        assert 'if _router_exit_exempted(_cr_sym, "conviction_decay"):\n                        continue' in src

    def test_graduation_boost_splice(self):
        src = _main_src()
        assert ('if (_is_graduated_sym\n'
                '                    and not _router_exit_exempted(symbol, "graduation_boost")):') in src

    def test_boot_assignment_block(self):
        src = _main_src()
        assert 'logger.info("router_class_assigned", symbol=_rb_sym,' in src
        assert 'cls=_rb_eff, source="boot")' in src

    def test_refresh_loop_registered(self):
        src = _main_src()
        assert "async def _router_refresh_loop" in src
        assert '_supervise(_router_refresh_loop, "router_refresh")' in src

    def test_config_knob_defaults(self):
        from core.config import Settings
        s = Settings()
        assert s.router_enabled is True
        assert s.router_exit_exemptions_enabled is True
        assert s.router_class_ttl_s == 14400.0


# ── (e) Kill-switch off-state = pre-module bit-for-bit ───────────────────────

class TestKillSwitchOff:
    def test_instance_none_when_flag_false(self):
        # The construction law: master gate False (or failed import) = the
        # object is never built and every splice fail-opens.
        src = _main_src()
        assert ("_router_assignments = (\n"
                "        _RouterAssignments()\n"
                "        if (_RouterAssignments is not None\n"
                "            and bool(getattr(config, \"router_enabled\", False)))\n"
                "        else None)") in src

    def test_helper_checks_both_knobs_before_reading(self):
        src = _main_src()
        assert 'if not bool(getattr(config, "router_enabled", True)):\n                return False' in src
        assert 'if not bool(getattr(config, "router_exit_exemptions_enabled", True)):\n                return False' in src

    def test_helper_fail_open_swallow(self):
        # The outermost handler returns False (module acts = legacy) on ANY
        # error — the exemption can never break an exit module.
        src = _main_src()
        assert "        except Exception:\n            return False" in src

    def test_refresh_loop_self_gates(self):
        src = _main_src()
        assert ("if (_router_assignments is None\n"
                "                        or not bool(getattr(config, \"router_enabled\", False))):") in src

    def test_boot_block_guarded_on_instance_and_store(self):
        src = _main_src()
        assert "if _router_assignments is not None and _param_store is not None:" in src
