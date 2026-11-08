"""Pins for prop→SoDEX sniper integrator — SHIP-WITH-GATES."""

from __future__ import annotations

import math

import pytest

from intelligence.parasite_scalper import ScalpSignalGrade
from intelligence.prop_sniper_integrator import (
    BOOST_POINTS, INJECT_SCORE, PropSniperIntegrator, in_entry_zone,
    load_snapshots, map_prop_asset, rho_blocked)
from intelligence.prop_trend_bridge import ARM_TTL_S
from tests.test_prop_trend_bridge import _bars_around, _snap


class _Cfg:
    prop_sniper_enabled = True
    prop_sniper_live_enabled = False
    prop_trend_bridge_enabled = True
    prop_sniper_leverage = 15
    prop_sniper_margin_cap_usd = 70.0


NOW = 1_800_000_000.0
SYM_MAP = {"BCH": "BCH-USD", "ZEC": "ZEC-USD"}


def _grade(sym="BCH-USD", direction="short", score=72, source="predictive"):
    return ScalpSignalGrade(
        grade="A", direction=direction, source=source,
        primary_symbol=sym, ecosystem_symbols=(sym,),
        prediction_score=score, leverage=8, margin_per_position=15.0,
        tp_pct=0.024, stop_pct=0.006)


def _integrate(cfg=None, snapshots=None, grades=None, marks=None,
               bars=None, families=None, open_sodex=None, venue="sodex",
               live=None, integ=None, now=None):
    cfg = cfg or _Cfg()
    if live is not None:
        cfg.prop_sniper_live_enabled = live
    integ = integ or PropSniperIntegrator(cfg)
    marks = marks or {"BCH-USD": 257.0}
    bars = bars or {"BCH-USD": _bars_around(258.0)}
    out = integ.integrate(
        snapshots=snapshots if snapshots is not None else [_snap()],
        grades=grades if grades is not None else [],
        mark_fn=lambda s: marks.get(s),
        bars_fn=lambda s, since: bars.get(s, []),
        family_fn=lambda s: (families or {}).get(s, []),
        symbol_map=SYM_MAP,
        open_sodex=open_sodex or (),
        now=now if now is not None else NOW,
        venue=venue)
    return out, integ.journal_rows(), integ


def _arm_then_pullback(**kwargs):
    """Tick 1: confirm ≥+1R at mark 257. Tick 2: pullback to entry zone."""
    live = kwargs.pop("live", True)
    grades = kwargs.pop("grades", [])
    open_sodex = kwargs.pop("open_sodex", ())
    cfg = kwargs.pop("cfg", _Cfg())
    integ = PropSniperIntegrator(cfg)
    _integrate(cfg=cfg, integ=integ, marks={"BCH-USD": 257.0},
               grades=[], live=live, **kwargs)
    return _integrate(cfg=cfg, integ=integ, marks={"BCH-USD": 258.3},
                      grades=grades, open_sodex=open_sodex, live=live,
                      now=NOW + 30.0, **kwargs)


class TestHelpers:
    def test_map_prop_asset(self):
        assert map_prop_asset("BCH", {"BCH": "BCH-USD"}) == "BCH-USD"
        assert map_prop_asset("xyz:NVDA", {}) == "NVDA-USD"
        assert map_prop_asset("SOL-USD", {}) == "SOL-USD"

    def test_in_entry_zone_short_upper_half(self):
        assert in_entry_zone("short", 258.4, 258.66, 257.10) is True
        assert in_entry_zone("short", 257.2, 258.66, 257.10) is False

    def test_in_entry_zone_long_lower_half(self):
        assert in_entry_zone("long", 257.2, 258.66, 257.10) is True
        assert in_entry_zone("long", 258.4, 258.66, 257.10) is False

    def test_rho_same_symbol(self):
        assert rho_blocked("BCH-USD", "short",
                           [{"symbol": "BCH-USD", "direction": "short"}],
                           lambda s: []) is True

    def test_rho_family_peer(self):
        fam = lambda s: ["BCH-USD", "ZEC-USD"]
        assert rho_blocked(
            "BCH-USD", "short",
            [{"symbol": "ZEC-USD", "side": "sell"}], fam) is True

    def test_rho_clear(self):
        assert rho_blocked("BCH-USD", "short", [], lambda s: []) is False

    def test_load_snapshots(self):
        snaps = load_snapshots(
            ["a.json", "", "b.json"],
            lambda p: {"ts": 1, "path": p} if p == "a.json" else None)
        assert len(snaps) == 1 and snaps[0]["path"] == "a.json"


class TestIntegratorGates:
    def test_kill_switch_off_passthrough(self):
        cfg = _Cfg(); cfg.prop_sniper_enabled = False
        g = [_grade()]
        out, rows, _ = _integrate(cfg=cfg, grades=g)
        assert out is not g and out[0].prediction_score == 72
        assert rows == []

    def test_venue_not_sodex(self):
        out, rows, _ = _integrate(venue="arcus", grades=[])
        assert out == []
        assert rows[0]["reason"] == "venue_not_sodex"

    def test_shadow_does_not_mutate_grades(self):
        out, rows, _ = _arm_then_pullback(grades=[], live=False)
        assert out == []
        assert any(r.get("type") == "prop_sniper_inject" for r in rows)
        assert all(r.get("live") is False for r in rows
                   if r.get("type") == "prop_sniper_inject")

    def test_live_inject_grade_a(self):
        out, rows, _ = _arm_then_pullback(grades=[], live=True)
        assert len(out) == 1
        assert out[0].source == "prop_sniper"
        assert out[0].grade == "A"
        assert out[0].prediction_score == INJECT_SCORE
        assert out[0].direction == "short"
        assert any(r["type"] == "prop_sniper_inject" for r in rows)

    def test_boost_same_dir_never_override_geometry(self):
        g = [_grade(score=72)]
        out, rows, _ = _arm_then_pullback(grades=g, live=True)
        assert len(out) == 1
        assert out[0].prediction_score == 72 + BOOST_POINTS
        assert out[0].source == "prop_sniper_boost"
        assert out[0].tp_pct == 0.024  # parasite geometry preserved

    def test_override_refused_on_conflict(self):
        g = [_grade(direction="long", score=80)]
        out, rows, _ = _arm_then_pullback(grades=g, live=True)
        assert out[0].direction == "long"
        assert out[0].prediction_score == 80
        assert any(r.get("reason") == "override_refused" for r in rows)

    def test_rho_gate_standdown(self):
        out, rows, _ = _arm_then_pullback(
            grades=[], live=True,
            open_sodex=[{"symbol": "BCH-USD", "direction": "short"}])
        assert out == []
        assert any(r.get("reason") == "rho_same_dir_open" for r in rows)

    def test_awaiting_pullback(self):
        # confirm at +1R but mark still at cheap edge → wait
        out, rows, _ = _integrate(grades=[], marks={"BCH-USD": 257.0},
                                  live=True)
        assert out == []
        assert any(r.get("reason") == "awaiting_pullback" for r in rows)
        assert any(r.get("type") == "prop_sniper_armed" for r in rows)

    def test_prop_stop_out_disarms(self):
        cfg = _Cfg(); cfg.prop_sniper_live_enabled = True
        integ = PropSniperIntegrator(cfg)
        _integrate(cfg=cfg, integ=integ, marks={"BCH-USD": 257.0},
                   grades=[], live=True)
        assert "BCH-USD" in integ._armed
        # empty book = infantry dead
        out, rows, _ = _integrate(cfg=cfg, integ=integ, snapshots=[],
                                  grades=[], marks={"BCH-USD": 258.3},
                                  live=True, now=NOW + 30.0)
        assert out == []
        assert "BCH-USD" not in integ._armed
        assert any(r.get("reason") == "prop_stop_out" for r in rows)

    def test_fault_fail_closed(self):
        cfg = _Cfg(); cfg.prop_sniper_live_enabled = True
        integ = PropSniperIntegrator(cfg)
        _integrate(cfg=cfg, integ=integ, marks={"BCH-USD": 257.0},
                   grades=[], live=True)

        def boom_family(_s):
            raise RuntimeError("x")

        out = integ.integrate(
            snapshots=[_snap()], grades=[_grade()],
            mark_fn=lambda s: 258.3,
            bars_fn=lambda s, since: _bars_around(258.0),
            family_fn=boom_family, symbol_map=SYM_MAP,
            open_sodex=[], now=NOW + 30.0, venue="sodex")
        assert len(out) == 1 and out[0].prediction_score == 72
        assert integ.journal_rows()[0]["reason"] == "integrator_fault"

    def test_be_armed_not_used_as_confirm(self):
        # below +1R even if snapshot carries be_armed — brain refuses
        snap = _snap()
        snap["positions"][0]["be_armed"] = True
        out, rows, _ = _integrate(snapshots=[snap], grades=[],
                                  marks={"BCH-USD": 260.5}, live=True)
        assert out == []
        assert any(r.get("reason") == "below_r_confirm" for r in rows)
