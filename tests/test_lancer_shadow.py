"""
tests/test_lancer_shadow.py — pins for the LANCER trade-class shadow gate.

Doctrine under test (spec 2026-10-04, SHADOW-ONLY):
  - candidate classes: rotation family (rotation_laggard / Tier-1 SSI
    institutional component) + QNT-class structural (LEVERAGE_CAPS 10x+
    tier with structure-tier provenance); cascade class hard-excluded
  - dedup: one open shadow per symbol (skipped_duplicate)
  - live-envelope admission caps bind the shadow cohort (concurrency,
    per-day, no same-day reload after stop-out)
  - stop = max(1.0 x ATR15/mark, 4%) via the EXISTING ATR accessor
    (intelligence/conviction_review.atr_pct_from_candles), never a
    recomputed ATR
  - trail-only exit mirroring the live loop ratchet (main.py:16345-16380)
  - 72h timeout flat-close
  - excursion journaling: MFE/MAE in R and % every tick; dense close row
  - kill switch False = total no-op, zero state mutation
  - fail-closed: nothing inside evaluate/on_tick ever propagates

No network, no Settings() real env — SimpleNamespace cfgs per repo idiom.
"""
import json
from types import SimpleNamespace

import pytest

from intelligence.lancer import (
    ASTER_LEVERAGE,
    LancerShadow,
    append_close,
    base_name,
    choose_venue,
    classify_signal,
    stop_geometry,
)
from intelligence.conviction_review import atr_pct_from_candles

T0 = 1_760_000_000.0   # fixed epoch anchor for deterministic yday math
DAY = 86_400.0
HOUR = 3_600.0


def cfg(**over):
    base = dict(
        lancer_shadow_enabled=True,
        lancer_max_concurrent=1,
        lancer_max_per_day=3,
        lancer_no_reload_same_day=True,
        lancer_stop_atr_mult=1.0,
        lancer_stop_floor_pct=0.04,
        lancer_timeout_h=72.0,
        trail_activation_atr=2.0,
        trail_distance_atr=1.0,
    )
    base.update(over)
    return SimpleNamespace(**base)


def candle(h, l, c):
    return SimpleNamespace(high=h, low=l, close=c)


def atr_pct_from_range(range_pct, px, n=16):
    """Hand-built candle sequence whose ATR15/price == range_pct:
    every candle ranges r*px around a flat close (TR = r*px each bar)."""
    r = range_pct
    return atr_pct_from_candles(
        [candle(px * (1 + r / 2), px * (1 - r / 2), px) for _ in range(n)])


def open_rotation(sh, c, symbol="BTC-USD", side="long", entry=100.0,
                  atr_pct=None, venue="sodex", ts=T0):
    return sh.evaluate_candidate(
        cfg=c, symbol=symbol, direction=side, entry_price=entry,
        atr_pct=atr_pct, venue=venue, now_ts=ts,
        components={"rotation_laggard": 0.4},
        personality="SCOUT", cascade_zscore=0.0)


# ── pure helpers ─────────────────────────────────────────────────────────────

class TestHelpers:
    def test_base_name(self):
        assert base_name("BTC-USD") == "BTC"
        assert base_name("btc") == "BTC"
        assert base_name("USTECH100-USD") == "USTECH100"

    def test_venue_argmax_sodex_majors(self):
        # Spec table: SoDEX ETH 40 / BTC 38 / XAUT-USTECH100 25.
        assert choose_venue("ETH-USD", "sodex")[:2] == ("sodex", 40)
        assert choose_venue("BTC-USD", "sodex")[:2] == ("sodex", 38)
        assert choose_venue("XAUT-USD", "sodex")[:2] == ("sodex", 25)
        assert choose_venue("USTECH100-USD", "sodex")[:2] == ("sodex", 25)

    def test_venue_argmax_mid_tier(self):
        # SoDEX mid tier = 10 (LEVERAGE_CAPS); unmapped SoDEX symbol
        # defaults mid 10.
        assert choose_venue("LINK-USD", "sodex")[:2] == ("sodex", 10)
        assert choose_venue("FARTCOIN-USD", "sodex")[:2] == ("sodex", 10)
        # Aster-routed mid: post-uplift 15 beats the SoDEX 10 tier.
        v, lev, table = choose_venue("LINK-USD", "aster")
        assert (v, lev) == ("aster", ASTER_LEVERAGE)
        assert table == {"sodex": 10, "aster": 15}

    def test_venue_argmax_aster_only_alt(self):
        v, lev, _ = choose_venue("HYPE-USD", "aster")
        assert (v, lev) == ("aster", 15)

    def test_classify_rotation_and_structural(self):
        assert classify_signal("SOL-USD", {"rotation_laggard": 0.3}) == ("rotation", "")
        assert classify_signal("SOL-USD", {"institutional": 0.5}) == ("rotation", "")
        # QNT-class structural: 10x+ tier + structure provenance.
        assert classify_signal("QNT-USD", {"structure": 0.5}) == ("structural", "")
        # No provenance, no class.
        assert classify_signal("QNT-USD", {"structure": 0.0})[0] == "excluded"
        assert classify_signal("SPCX-USD", {})[1] == "not_lancer_class"

    def test_stop_geometry_floor_vs_atr(self):
        # 1 x ATR < 4% -> floor binds; wider ATR -> ATR binds.
        s, d = stop_geometry("long", 100.0, 0.01, 1.0, 0.04)
        assert (s, d) == (96.0, 0.04)
        s, d = stop_geometry("long", 100.0, 0.06, 1.0, 0.04)
        assert (s, round(d, 6)) == (94.0, 0.06)
        s, d = stop_geometry("short", 100.0, None, 1.0, 0.04)
        assert (s, d) == (104.0, 0.04)   # dark ATR -> floor (fail-closed wide)
        assert stop_geometry("long", 0.0, 0.05, 1.0, 0.04) == (0.0, 0.0)


# ── admission ────────────────────────────────────────────────────────────────

class TestAdmission:
    def test_open_rotation_journals_venue_leverage(self):
        sh = LancerShadow()
        res = open_rotation(sh, cfg(), symbol="ETH-USD", entry=3000.0)
        assert res["action"] == "opened"
        rec = res["opened"]
        assert rec["venue"] == "sodex" and rec["leverage"] == 40
        assert rec["signal_class"] == "rotation"
        assert sh.open_symbols() == ["ETH-USD"]
        ev_names = [e[0] for e in res["events"]]
        assert "lancer_candidate_opened" in ev_names

    def test_cascade_class_excluded(self):
        sh = LancerShadow()
        for kw in ({"personality": "APEX"}, {"cascade_zscore": 2.0}):
            args = dict(cfg=cfg(), symbol="BTC-USD", direction="long",
                        entry_price=100.0, atr_pct=0.05, venue="sodex",
                        now_ts=T0, components={"rotation_laggard": 0.4},
                        personality="SCOUT", cascade_zscore=0.0)
            args.update(kw)
            res = sh.evaluate_candidate(**args)
            assert res["action"] == "skipped"
            assert res["reason"] == "cascade_class"
        assert not sh.has_open()

    def test_duplicate_symbol_refused_while_open(self):
        sh = LancerShadow()
        assert open_rotation(sh, cfg())["action"] == "opened"
        res = open_rotation(sh, cfg())
        assert res["action"] == "skipped"
        assert res["reason"] == "skipped_duplicate"
        assert len(sh.open_symbols()) == 1

    def test_concurrency_cap_one_enforced(self):
        sh = LancerShadow()
        assert open_rotation(sh, cfg(), symbol="BTC-USD")["action"] == "opened"
        res = open_rotation(sh, cfg(), symbol="ETH-USD", entry=3000.0)
        assert res["action"] == "skipped"
        assert res["reason"] == "skipped_concurrency"
        # Cap 2 admits the second symbol.
        sh2 = LancerShadow()
        c2 = cfg(lancer_max_concurrent=2)
        assert open_rotation(sh2, c2, symbol="BTC-USD")["action"] == "opened"
        assert open_rotation(sh2, c2, symbol="ETH-USD",
                             entry=3000.0)["action"] == "opened"

    def test_daily_cap_enforced(self):
        sh = LancerShadow()
        c = cfg(lancer_max_concurrent=3, lancer_max_per_day=3)
        for i, sym in enumerate(("BTC-USD", "ETH-USD", "SOL-USD")):
            assert open_rotation(sh, c, symbol=sym,
                                 entry=100.0 + i)["action"] == "opened"
        res = open_rotation(sh, c, symbol="XAUT-USD", entry=200.0)
        assert res["reason"] == "skipped_concurrency"  # cap 3 concurrent binds
        # Close one; the per-day cap must still refuse (admissions counted).
        sh.on_tick("BTC-USD", 90.0, T0 + 60)   # stop-out BTC
        res = open_rotation(sh, c, symbol="XAUT-USD", entry=200.0, ts=T0 + 120)
        assert res["reason"] == "skipped_daily_cap"

    def test_no_reload_same_day_after_stopout(self):
        sh = LancerShadow()
        c = cfg()
        assert open_rotation(sh, c, entry=100.0)["action"] == "opened"
        closes = sh.on_tick("BTC-USD", 95.0, T0 + 60)   # stop-out at 96
        assert closes and closes[0]["exit_class"] == "stop"
        res = open_rotation(sh, c, entry=100.0, ts=T0 + 120)
        assert res["reason"] == "skipped_no_reload"
        # Next UTC day: reload allowed.
        res = open_rotation(sh, c, entry=100.0, ts=T0 + DAY)
        assert res["action"] == "opened"

    def test_disabled_is_total_noop(self):
        sh = LancerShadow()
        res = open_rotation(sh, cfg(lancer_shadow_enabled=False))
        assert res["action"] == "skipped" and res["reason"] == "disabled"
        assert not sh.has_open()
        assert res["events"] == [] and res["opened"] is None

    def test_exception_never_propagates(self, monkeypatch):
        sh = LancerShadow()
        import intelligence.lancer as _lc
        monkeypatch.setattr(_lc, "classify_signal",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        res = open_rotation(sh, cfg())
        assert res["action"] == "skipped"
        assert res["reason"] == "internal_error"
        assert not sh.has_open()
        # Garbage inputs fail closed, never raise.
        res = sh.evaluate_candidate(cfg=cfg(), symbol="BTC-USD",
                                    direction="long", entry_price="abc",
                                    atr_pct="junk", venue="sodex",
                                    now_ts=T0, components=None)
        assert res["action"] == "skipped"
        assert sh.on_tick("BTC-USD", "nan-price", T0) == []


# ── exit machinery + excursion journaling ────────────────────────────────────

class TestExits:
    def test_stop_out_path(self):
        sh = LancerShadow()
        c = cfg()
        # ATR-dark -> 4% floor: stop = 96.
        assert open_rotation(sh, c, entry=100.0, atr_pct=None)["action"] == "opened"
        closes = sh.on_tick("BTC-USD", 99.0, T0 + 10)
        assert closes == []
        closes = sh.on_tick("BTC-USD", 95.9, T0 + 20)
        assert len(closes) == 1
        row = closes[0]
        assert row["exit_class"] == "stop"
        assert row["exit_price"] == pytest.approx(96.0)
        assert row["r_multiple"] == pytest.approx(-1.0)
        assert row["mae_R"] <= -1.0          # adverse excursion through the stop
        assert row["mfe_R"] == pytest.approx(0.0)
        assert row["gate"] == "lancer"
        assert not sh.has_open()             # deregistered on close

    def test_trail_exit_after_6r_mfe(self):
        sh = LancerShadow()
        c = cfg()
        # ATR15 = 4% of price -> stop dist 4%, atr_abs = 4.0; act 2x ATR
        # arms the trail at +8%, trail 1x ATR behind best.
        assert open_rotation(sh, c, entry=100.0, atr_pct=0.04)["action"] == "opened"
        assert sh.on_tick("BTC-USD", 107.9, T0 + 10) == []   # below activation
        assert sh.on_tick("BTC-USD", 124.0, T0 + 20) == []   # +24% = +6R MFE
        rec = sh._open["BTC-USD"]
        assert rec["trail_active"] is True
        assert rec["best_price"] == 124.0
        assert rec["current_stop"] == pytest.approx(120.0)   # 124 - 1x ATR
        assert sh.on_tick("BTC-USD", 122.0, T0 + 30) == []   # holds above trail
        closes = sh.on_tick("BTC-USD", 119.0, T0 + 40)
        assert len(closes) == 1
        row = closes[0]
        assert row["exit_class"] == "trail"
        assert row["trail_activated"] is True
        assert row["exit_price"] == pytest.approx(120.0)
        assert row["r_multiple"] == pytest.approx(5.0)       # +20% / 4%
        assert row["mfe_R"] == pytest.approx(6.0)            # +24% / 4%
        assert row["mfe_pct"] == pytest.approx(24.0)
        assert row["mae_R"] == pytest.approx(0.0)

    def test_timeout_flat_close(self):
        sh = LancerShadow()
        c = cfg()
        assert open_rotation(sh, c, entry=100.0)["action"] == "opened"
        t_late = T0 + 72.0 * HOUR + 1.0
        closes = sh.on_tick("BTC-USD", 100.5, t_late)
        assert len(closes) == 1
        row = closes[0]
        assert row["exit_class"] == "timeout"
        assert row["exit_price"] == pytest.approx(100.5)
        assert row["r_multiple"] == pytest.approx(0.125)     # +0.5% / 4%
        assert row["time_in_trade_min"] == pytest.approx(72 * 60 + 1 / 60, abs=0.1)

    def test_atr_floor_interaction_with_real_accessor(self):
        # Hand-built candle sequences through the EXISTING ATR15 accessor:
        # tight range (1%) -> stop = 4% floor; wide range (6%) -> ATR binds.
        sh = LancerShadow()
        c = cfg()
        atr_tight = atr_pct_from_range(0.01, 100.0)
        assert atr_tight == pytest.approx(0.01, abs=1e-9)
        res = open_rotation(sh, c, entry=100.0, atr_pct=atr_tight)
        assert res["opened"]["stop_dist_pct"] == pytest.approx(0.04)
        assert res["opened"]["stop"] == pytest.approx(96.0)

        sh2 = LancerShadow()
        atr_wide = atr_pct_from_range(0.06, 100.0)
        res = open_rotation(sh2, c, entry=100.0, atr_pct=atr_wide)
        assert res["opened"]["stop_dist_pct"] == pytest.approx(0.06, abs=1e-9)
        assert res["opened"]["stop"] == pytest.approx(94.0)

    def test_short_side_stop_and_trail(self):
        sh = LancerShadow()
        c = cfg()
        sh.evaluate_candidate(cfg=c, symbol="BTC-USD", direction="short",
                              entry_price=100.0, atr_pct=0.04, venue="sodex",
                              now_ts=T0, components={"rotation_laggard": 0.3})
        rec = sh._open["BTC-USD"]
        assert rec["stop"] == pytest.approx(104.0)
        # Rally into the short stop -> stop-out, R = -1.
        closes = sh.on_tick("BTC-USD", 104.5, T0 + 10)
        assert closes[0]["exit_class"] == "stop"
        assert closes[0]["r_multiple"] == pytest.approx(-1.0)


# ── journal writer ───────────────────────────────────────────────────────────

class TestWriter:
    def test_append_close_one_dense_line(self, tmp_path):
        p = str(tmp_path / "lancer_shadow.jsonl")
        row = {"gate": "lancer", "symbol": "BTC-USD", "r_multiple": -1.0}
        assert append_close(p, row) is True
        with open(p) as f:
            lines = f.readlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["gate"] == "lancer"

    def test_append_close_bad_row_costs_row_only(self, tmp_path):
        p = str(tmp_path / "lancer_shadow.jsonl")
        # Tuple keys raise TypeError even under default=str.
        assert append_close(p, {(1, 2): "x"}) is False     # unserializable
        assert append_close(p, {"ok": 1}) is True          # file still works
        with open(p) as f:
            assert len(f.readlines()) == 1
