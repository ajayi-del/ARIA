"""Prop-trend bridge pins — band map, target-forcing geometry, arm gates."""

from __future__ import annotations

import math

import pytest

from intelligence.prop_trend_bridge import (ARM_TTL_S, MAX_LEVERAGE,
                                            PROFIT_TARGET_USD,
                                            PropTrendBridge, SniperArm,
                                            compute_band, sniper_geometry)


class _Cfg:
    prop_trend_bridge_enabled = True
    prop_sniper_leverage = 15
    prop_sniper_margin_cap_usd = 70.0


NOW = 1_800_000_000.0


def _bars_around(center, width_pct=0.006, n=80):
    """Oscillating 5m bars filling [center*(1-w), center*(1+w)]."""
    bars = []
    lo, hi = center * (1 - width_pct), center * (1 + width_pct)
    for i in range(n):
        px = lo + (hi - lo) * ((i * 7) % 11) / 10.0
        bars.append((i * 300_000, px, min(hi, px * 1.0008),
                     max(lo, px * 0.9992), px))
    return bars


def _snap(asset="BCH", side="sell", entry=260.88, stop=264.6608,
          opened=NOW - 3600, ts=NOW):
    return {
        "ts": ts, "account_id": "urn:prp-account:x", "sleeve": "crypto",
        "positions": [{
            "asset": asset, "side": side, "entry_ref": entry,
            "stop_price": stop, "quantity": 1.0, "risk_usd": 5.0,
            "opened_ts": opened,
        }],
    }


class TestComputeBand:
    def test_maps_oscillation(self):
        band = compute_band(_bars_around(258.0), "short", 257.9)
        assert band is not None
        top, bottom, touches = band
        assert bottom < 258.0 < top
        assert touches >= 6

    def test_empty_bars_none(self):
        assert compute_band([], "short", 100.0) is None

    def test_bad_mark_none(self):
        assert compute_band(_bars_around(100.0), "short", 0.0) is None
        assert compute_band(_bars_around(100.0), "short", None) is None
        assert compute_band(_bars_around(100.0), "short", math.nan) is None

    def test_bad_direction_none(self):
        assert compute_band(_bars_around(100.0), "diagonal", 100.0) is None

    def test_flat_tape_too_thin(self):
        # 0.05% oscillation < MIN_BAND_PCT 0.20% — can't pay fees
        assert compute_band(_bars_around(100.0, width_pct=0.0005),
                            "short", 100.0) is None


class TestSniperGeometry:
    def test_bch_class_short_forces_target(self):
        # live 2026-11-07 BCH band: 257.10-258.66, mark 257.88
        geo = sniper_geometry("short", 258.66, 257.10, 257.88,
                              leverage=15, margin_cap=70.0)
        assert geo is not None
        band = (258.66 - 257.10) / 257.88
        want_notional = PROFIT_TARGET_USD / (band - 0.0009)
        assert geo["notional_usd"] == pytest.approx(want_notional, rel=1e-6)
        assert geo["margin_usd"] == pytest.approx(want_notional / 15, rel=1e-6)
        assert geo["margin_usd"] <= 70.0
        assert geo["risk_usd"] <= 8.0
        assert geo["entry_zone"] == 258.66       # short sells the top
        assert geo["target_price"] == 257.10
        assert geo["stop_price"] > 258.66
        # net profit at full band traverse == target by construction
        net = geo["notional_usd"] * (band - 0.0009)
        assert net == pytest.approx(PROFIT_TARGET_USD, rel=1e-6)

    def test_long_mirrors(self):
        geo = sniper_geometry("long", 258.66, 257.10, 257.88,
                              leverage=15, margin_cap=70.0)
        assert geo["entry_zone"] == 257.10       # long buys the bottom
        assert geo["target_price"] == 258.66
        assert geo["stop_price"] < 257.10

    def test_thin_band_stands_down(self):
        # 0.08% band < 9bps round-trip fees — cannot net anything
        assert sniper_geometry("short", 100.04, 99.96, 100.0,
                               leverage=15, margin_cap=70.0) is None

    def test_margin_cap_standdown_not_shrink(self):
        # 0.25% band → needs ~$2,273 notional = $152 margin > $70 cap.
        # Governor: the $5 target is the point — stand down, never shrink.
        assert sniper_geometry("short", 100.125, 99.875, 100.0,
                               leverage=15, margin_cap=70.0) is None

    def test_risk_clamp_standdown(self):
        # risk/target ratio is fixed by band geometry — the clamp can never
        # be re-solved, so a wide-band big-target case stands down
        assert sniper_geometry("short", 102.0, 98.0, 100.0, leverage=15,
                               margin_cap=500.0, target_usd=40.0) is None

    def test_risk_clamp_unresolvable_stands_down(self):
        assert sniper_geometry("short", 101.0, 99.0, 100.0, leverage=15,
                               margin_cap=500.0, target_usd=100.0) is None

    def test_leverage_capped(self):
        geo = sniper_geometry("short", 258.66, 257.10, 257.88,
                              leverage=50, margin_cap=500.0)
        assert geo["leverage"] == MAX_LEVERAGE


def _bridge_tick(snapshots, marks, bars_by_sym, families, cfg=None,
                 symbol_map=None):
    br = PropTrendBridge(cfg or _Cfg())
    arms = br.tick(
        snapshots=snapshots,
        mark_fn=lambda s: marks.get(s),
        bars_fn=lambda s, since: bars_by_sym.get(s, []),
        family_fn=lambda k: families.get(k, []),
        symbol_map=symbol_map or {"BCH": "BCH-USD", "ZEC": "ZEC-USD"},
        now=NOW)
    return arms, br.journal_rows()


class TestBridgeTick:
    def test_kill_switch_off(self):
        cfg = _Cfg(); cfg.prop_trend_bridge_enabled = False
        arms, rows = _bridge_tick([_snap()], {}, {}, {}, cfg=cfg)
        assert arms == [] and rows == []

    def test_stale_snapshot_no_arm(self):
        arms, rows = _bridge_tick([_snap(ts=NOW - 300)],
                                  {"BCH-USD": 255.0},
                                  {"BCH-USD": _bars_around(258.0)}, {})
        assert arms == []
        assert rows[0]["reason"] == "stale_snapshot"

    def test_below_r_confirm_stands_down(self):
        # mark 260.5 vs entry 260.88 short → +0.10R < 1R
        arms, rows = _bridge_tick([_snap()], {"BCH-USD": 260.5},
                                  {"BCH-USD": _bars_around(258.0)}, {})
        assert arms == []
        assert rows[0]["reason"] == "below_r_confirm"

    def test_armed_prop_leg(self):
        # entry 260.88, stop 264.6608 → 1R = 3.7808; mark 257.0 → +1.03R
        arms, rows = _bridge_tick([_snap()], {"BCH-USD": 257.0},
                                  {"BCH-USD": _bars_around(258.0)}, {})
        assert len(arms) == 1
        arm = arms[0]
        assert arm.symbol == "BCH-USD" and arm.direction == "short"
        assert arm.source == "prop_leg" and arm.r_multiple > 1.0
        assert arm.armed_until == NOW + ARM_TTL_S
        assert arm.notional_usd * (arm.band_pct - 0.0009) >= 5.0
        assert rows[0]["type"] == "prop_sniper_armed"

    def test_mark_dark_stands_down(self):
        arms, rows = _bridge_tick([_snap()], {},
                                  {"BCH-USD": _bars_around(258.0)}, {})
        assert arms == []
        assert rows[0]["reason"] == "mark_dark"

    def test_unmapped_asset(self):
        arms, rows = _bridge_tick([_snap(asset="DOGE")],
                                  {"BCH-USD": 257.0}, {}, {})
        assert rows[0]["reason"] == "unmapped_asset"

    def test_buy_side_is_long(self):
        # long entry 100 stop 99 → 1R = 1.0; mark 101.5 → +1.5R confirmed
        snap = _snap(side="buy", entry=100.0, stop=99.0, opened=NOW - 600)
        arms, _ = _bridge_tick([snap], {"BCH-USD": 101.5},
                               {"BCH-USD": _bars_around(101.2)}, {})
        assert arms and arms[0].direction == "long"

    def test_family_needs_two_legs(self):
        fams = {"BCH-USD": ["BCH-USD", "ZEC-USD", "LTC-USD"]}
        arms, rows = _bridge_tick([_snap()], {"BCH-USD": 257.0},
                                  {"BCH-USD": _bars_around(258.0)}, fams)
        assert all(a.source == "prop_leg" for a in arms)

    def test_family_extension_half_margin_cap(self):
        zec_snap = _snap(asset="ZEC", entry=1278.05, stop=1304.122,
                         opened=NOW - 3600)
        # BCH leg + ZEC leg same family same direction → LTC arms
        marks = {"BCH-USD": 257.0, "ZEC-USD": 1250.0, "LTC-USD": 52.0}
        bars = {"BCH-USD": _bars_around(258.0),
                "ZEC-USD": _bars_around(1262.0),
                "LTC-USD": _bars_around(52.05)}
        members = ["BCH-USD", "ZEC-USD", "LTC-USD"]
        fams = {s: members for s in members}
        arms, rows = _bridge_tick([_snap(), zec_snap], marks, bars, fams)
        fam_arms = [a for a in arms if a.source == "family"]
        assert len(fam_arms) == 1
        assert fam_arms[0].symbol == "LTC-USD"
        assert fam_arms[0].direction == "short"
        assert fam_arms[0].family_size == 2
        assert fam_arms[0].margin_usd <= 35.0  # half the $70 cap
        armed = [r for r in rows if r["type"] == "prop_sniper_armed"]
        assert len(armed) == 3

    def test_fault_fail_closed(self):
        br = PropTrendBridge(_Cfg())
        arms = br.tick(snapshots=[_snap()],
                       mark_fn=_boom, bars_fn=_boom, family_fn=_boom,
                       symbol_map={"BCH": "BCH-USD"}, now=NOW)
        assert arms == []


def _boom(*a, **k):
    raise RuntimeError("dark plane")
