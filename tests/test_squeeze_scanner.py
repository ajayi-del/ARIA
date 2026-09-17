"""Pins for intelligence/squeeze_scanner.py — the ZEC 2026-09-17 shadow watchlist.

Thresholds are UNSOURCED hypotheses (n=1 reconstruction); these tests pin the
contract, not the truth of the thresholds.
"""
import json

import pytest

from intelligence.squeeze_scanner import (
    COIL_MOVE_PCT,
    FUNDING_ACCUM_RATIO,
    OI_BUILDING_PCT,
    OI_STRONG_PCT,
    RV_RANK_LOW,
    WHALE_LS_LONG,
    SqueezeInputs,
    SqueezeWatchlist,
    post_squeeze_exhausted,
    scanner_enabled,
    squeeze_score,
)


def _inp(**kw):
    base = dict(symbol="ZEC-USD", funding_rate=5.0, funding_avg=5.0,
                oi_delta_pct=0.0, rv_rank=50.0, whale_ls=None,
                price_day_move_pct=5.0)
    base.update(kw)
    return SqueezeInputs(**base)


# ── funding leg ───────────────────────────────────────────────────────────────
class TestFundingLeg:
    def test_below_ratio_scores_3(self):
        # 2.648 < 0.65 * 5.13 (3.3345)
        r = squeeze_score(_inp(funding_rate=2.648, funding_avg=5.13))
        assert r.legs.get("funding_accum") == 3

    def test_at_ratio_boundary_scores_0(self):
        avg = 10.0
        r = squeeze_score(_inp(funding_rate=FUNDING_ACCUM_RATIO * avg, funding_avg=avg))
        assert "funding_accum" not in r.legs

    def test_negative_funding_adds_paying_point(self):
        r = squeeze_score(_inp(funding_rate=-0.5, funding_avg=5.0))
        assert r.legs.get("funding_accum") == 3      # -0.5 < 3.25 too
        assert r.legs.get("funding_paying") == 1

    def test_zero_funding_not_paying(self):
        r = squeeze_score(_inp(funding_rate=0.0, funding_avg=5.0))
        assert "funding_paying" not in r.legs

    def test_zero_avg_skips_ratio_leg(self):
        r = squeeze_score(_inp(funding_rate=-0.1, funding_avg=0.0))
        assert "funding_accum" not in r.legs
        assert r.legs.get("funding_paying") == 1


# ── OI leg ────────────────────────────────────────────────────────────────────
class TestOiLeg:
    def test_strong(self):
        assert squeeze_score(_inp(oi_delta_pct=16.0)).legs.get("oi_strong") == 2

    def test_at_strong_boundary_falls_to_building(self):
        assert squeeze_score(_inp(oi_delta_pct=OI_STRONG_PCT)).legs.get("oi_building") == 1

    def test_building(self):
        assert squeeze_score(_inp(oi_delta_pct=6.0)).legs.get("oi_building") == 1

    def test_at_building_boundary_scores_0(self):
        r = squeeze_score(_inp(oi_delta_pct=OI_BUILDING_PCT))
        assert "oi_strong" not in r.legs and "oi_building" not in r.legs


# ── whale leg ─────────────────────────────────────────────────────────────────
class TestWhaleLeg:
    def test_long_ratio_scores_2(self):
        assert squeeze_score(_inp(whale_ls=1.5)).legs.get("whale_long") == 2

    def test_at_boundary_scores_0(self):
        assert "whale_long" not in squeeze_score(_inp(whale_ls=WHALE_LS_LONG)).legs

    def test_none_abstains(self):
        assert "whale_long" not in squeeze_score(_inp(whale_ls=None)).legs


# ── rv_rank leg ───────────────────────────────────────────────────────────────
class TestRvLeg:
    def test_low_vol_scores_2(self):
        assert squeeze_score(_inp(rv_rank=20.0)).legs.get("rv_not_priced") == 2

    def test_at_boundary_scores_0(self):
        assert "rv_not_priced" not in squeeze_score(_inp(rv_rank=RV_RANK_LOW)).legs

    def test_bad_rv_never_credits(self):
        assert "rv_not_priced" not in squeeze_score(_inp(rv_rank="bogus")).legs


# ── coil leg ──────────────────────────────────────────────────────────────────
class TestCoilLeg:
    def test_flat_scores_1(self):
        assert squeeze_score(_inp(price_day_move_pct=1.9)).legs.get("coiled") == 1

    def test_negative_flat_scores_1(self):
        assert squeeze_score(_inp(price_day_move_pct=-1.9)).legs.get("coiled") == 1

    def test_at_boundary_scores_0(self):
        assert "coiled" not in squeeze_score(_inp(price_day_move_pct=COIL_MOVE_PCT)).legs


# ── verdict bands ─────────────────────────────────────────────────────────────
class TestVerdictBands:
    def test_setup_at_7(self):
        # 3 + 2 + 2 = 7
        r = squeeze_score(_inp(funding_rate=1.0, funding_avg=5.0,
                               oi_delta_pct=16.0, whale_ls=1.5,
                               rv_rank=50.0, price_day_move_pct=5.0))
        assert r.score == 7 and r.verdict == "SQUEEZE_SETUP"

    def test_watch_at_5(self):
        # 3 + 2 = 5
        r = squeeze_score(_inp(funding_rate=1.0, funding_avg=5.0,
                               rv_rank=20.0, price_day_move_pct=5.0))
        assert r.score == 5 and r.verdict == "WATCH"

    def test_watch_at_6_not_setup(self):
        # 3 + 2 + 1 = 6
        r = squeeze_score(_inp(funding_rate=1.0, funding_avg=5.0,
                               rv_rank=20.0, price_day_move_pct=0.5))
        assert r.score == 6 and r.verdict == "WATCH"

    def test_none_below_5(self):
        r = squeeze_score(_inp())
        assert r.score == 0 and r.verdict == "NONE"


# ── exhausted flag ────────────────────────────────────────────────────────────
class TestExhausted:
    def test_zec_post_squeeze(self):
        assert post_squeeze_exhausted(92.8) is True

    def test_at_boundary_not_exhausted(self):
        assert post_squeeze_exhausted(85.0) is False

    def test_low_not_exhausted(self):
        assert post_squeeze_exhausted(20.0) is False

    def test_bad_input_no_opinion(self):
        assert post_squeeze_exhausted("bogus") is False

    def test_hv_slot_accepted_and_unused(self):
        assert post_squeeze_exhausted(90.0, hv=0.42) is True


# ── ZEC reconstructed case (2026-09-17, +18.2%) ──────────────────────────────
class TestZecCase:
    def test_pre_squeeze_scores_10_setup(self):
        r = squeeze_score(_inp(funding_rate=2.648, funding_avg=5.13,
                               oi_delta_pct=36.6, whale_ls=1.5,
                               rv_rank=20.0, price_day_move_pct=0.8))
        assert r.legs == {"funding_accum": 3, "oi_strong": 2,
                          "whale_long": 2, "rv_not_priced": 2, "coiled": 1}
        assert r.score == 10 and r.verdict == "SQUEEZE_SETUP"

    def test_post_squeeze_exhausted(self):
        assert post_squeeze_exhausted(92.8) is True


# ── watchlist ─────────────────────────────────────────────────────────────────
class TestWatchlist:
    def _wl(self, tmp_path):
        clock = {"t": 1_000_000.0}
        wl = SqueezeWatchlist(log_path=str(tmp_path / "wl.jsonl"),
                              now=lambda: clock["t"])
        return wl, clock

    def _rows(self, tmp_path):
        p = tmp_path / "wl.jsonl"
        if not p.exists():
            return []
        return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]

    def test_first_scan_logs(self, tmp_path):
        wl, _ = self._wl(tmp_path)
        out = wl.scan([_inp(symbol="AAA-USD"), _inp(symbol="BBB-USD", rv_rank=20.0)])
        assert len(self._rows(tmp_path)) == 2
        assert [r.symbol for r in out] == ["BBB-USD", "AAA-USD"]  # score desc
        assert wl.latest("BBB-USD").score == 2

    def test_same_verdict_within_4h_not_relogged(self, tmp_path):
        wl, clock = self._wl(tmp_path)
        wl.scan([_inp(symbol="AAA-USD")])
        clock["t"] += 3600.0
        wl.scan([_inp(symbol="AAA-USD")])
        assert len(self._rows(tmp_path)) == 1

    def test_verdict_change_logs_immediately(self, tmp_path):
        wl, clock = self._wl(tmp_path)
        wl.scan([_inp(symbol="AAA-USD")])                       # NONE
        clock["t"] += 60.0
        wl.scan([_inp(symbol="AAA-USD", funding_rate=1.0,
                      rv_rank=20.0)])                           # WATCH (5)
        rows = self._rows(tmp_path)
        assert len(rows) == 2
        assert rows[-1]["verdict"] == "WATCH"

    def test_4h_throttle_relogs_same_verdict(self, tmp_path):
        wl, clock = self._wl(tmp_path)
        wl.scan([_inp(symbol="AAA-USD")])
        clock["t"] += 4 * 3600.0 + 1
        wl.scan([_inp(symbol="AAA-USD")])
        assert len(self._rows(tmp_path)) == 2

    def test_row_shape(self, tmp_path):
        wl, _ = self._wl(tmp_path)
        wl.scan([_inp(symbol="ZEC-USD", funding_rate=2.648, funding_avg=5.13,
                      oi_delta_pct=36.6, whale_ls=1.5, rv_rank=20.0,
                      price_day_move_pct=0.8)])
        row = self._rows(tmp_path)[0]
        assert row["symbol"] == "ZEC-USD" and row["score"] == 10
        assert row["verdict"] == "SQUEEZE_SETUP"
        assert row["exhausted"] is False
        for k in ("funding_rate", "funding_avg", "oi_delta_pct",
                  "rv_rank", "whale_ls", "price_day_move_pct", "legs"):
            assert k in row

    def test_fail_silent_bad_path(self, tmp_path):
        wl = SqueezeWatchlist(log_path=str(tmp_path / "nope" / "wl.jsonl"))
        out = wl.scan([_inp(symbol="AAA-USD", rv_rank=20.0)])   # dir missing
        assert out[0].score == 2                                 # scan survives
        assert wl.latest("AAA-USD").verdict == "NONE"

    def test_scan_none_and_bad_rows(self, tmp_path):
        wl, _ = self._wl(tmp_path)
        assert wl.scan(None) == []


# ── kill switch ───────────────────────────────────────────────────────────────
class TestKillSwitch:
    def test_default_enabled(self):
        assert scanner_enabled() is True
