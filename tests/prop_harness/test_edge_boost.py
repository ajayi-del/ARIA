"""Pins: edge boost — the profit mirror of loss_veto (2026-11-17).

The same cohort artifact that vetoes toxic cohorts arms a bounded size
multiplier on proven-positive ones. k=20 shrinkage (loss_stats) keeps a
lucky handful from boosting; the multiplier is bounded [1.0, cap];
cap < 1.0 clamps to 1.0 (a boost knob can never become a veto); the
rho-box / headroom clamps downstream still bind. Grain discipline
mirrors _loss_veto: schema 2 = (asset, side) keys only, legacy =
(family, side) only, mismatch abstains to 1.0. Kill PROP_EDGE_BOOST=0 →
legacy sizing bit-for-bit.
"""

from __future__ import annotations

import pytest

from prop_harness.brains import edge_boost as eb


class TestBoostMult:
    def test_missing_row_no_boost(self):
        assert eb.boost_mult(None) == 1.0
        assert eb.boost_mult({}) == 1.0
        assert eb.boost_mult("junk") == 1.0

    def test_thin_n_no_boost(self):
        row = {"n": 4, "shrunk_expectancy": 5.0}
        assert eb.boost_mult(row, min_n=5) == 1.0

    def test_negative_and_below_threshold_no_boost(self):
        assert eb.boost_mult({"n": 10, "shrunk_expectancy": -3.0}) == 1.0
        assert eb.boost_mult({"n": 10, "shrunk_expectancy": 0.0}) == 1.0
        assert eb.boost_mult({"n": 10, "shrunk_expectancy": 0.49},
                             min_expectancy=0.5) == 1.0

    def test_ramp_and_saturation(self):
        # min_exp 0.5 → full cap 1.5 at se >= 1.0
        assert eb.boost_mult({"n": 5, "shrunk_expectancy": 0.5},
                             min_expectancy=0.5, cap=1.5) == pytest.approx(1.25)
        assert eb.boost_mult({"n": 5, "shrunk_expectancy": 1.0},
                             min_expectancy=0.5, cap=1.5) == pytest.approx(1.5)
        assert eb.boost_mult({"n": 5, "shrunk_expectancy": 9.0},
                             min_expectancy=0.5, cap=1.5) == pytest.approx(1.5)

    def test_cap_below_one_clamps_to_one(self):
        assert eb.boost_mult({"n": 5, "shrunk_expectancy": 9.0},
                             min_expectancy=0.5, cap=0.5) == 1.0

    def test_zero_min_expectancy_abstains(self):
        assert eb.boost_mult({"n": 5, "shrunk_expectancy": 9.0},
                             min_expectancy=0.0) == 1.0

    def test_junk_fields_no_boost(self):
        assert eb.boost_mult({"n": "x", "shrunk_expectancy": 9.0}) == 1.0
        assert eb.boost_mult({"n": 9, "shrunk_expectancy": "x"}) == 1.0
        assert eb.boost_mult(
            {"n": 9, "shrunk_expectancy": float("nan")}) == 1.0


class TestBoostFor:
    def test_returns_row_only_when_boosted(self):
        stats = {"BTC|buy": {"n": 6, "shrunk_expectancy": 1.0},
                 "ETH|buy": {"n": 6, "shrunk_expectancy": -1.0}}
        mult, row = eb.boost_for(stats, "BTC|buy", min_n=5,
                                 min_expectancy=0.5, cap=1.5)
        assert mult == pytest.approx(1.5)
        assert row is stats["BTC|buy"]
        mult2, row2 = eb.boost_for(stats, "ETH|buy", min_n=5)
        assert mult2 == 1.0 and row2 is None
        assert eb.boost_for(stats, "SOL|buy") == (1.0, None)

    def test_none_stats(self):
        assert eb.boost_for(None, "BTC|buy") == (1.0, None)


class TestKeyFor:
    def test_asset_grain_requires_schema2(self):
        assert eb.key_for({"schema": 2, "stats": {}}, asset="BTC",
                          side="buy", family="majors",
                          asset_grain=True) == "BTC|buy"
        assert eb.key_for({"stats": {}}, asset="BTC", side="buy",
                          family="majors", asset_grain=True) is None

    def test_family_grain_rejects_schema2(self):
        assert eb.key_for({"stats": {}}, asset="BTC", side="buy",
                          family="majors",
                          asset_grain=False) == "majors|buy"
        assert eb.key_for({"schema": 2, "stats": {}}, asset="BTC",
                          side="buy", family="majors",
                          asset_grain=False) is None

    def test_missing_pieces_abstain(self):
        assert eb.key_for(None, asset="BTC", side="buy", family="majors",
                          asset_grain=False) is None
        assert eb.key_for({"stats": {}}, asset="BTC", side=None,
                          family="majors", asset_grain=False) is None
        assert eb.key_for({"stats": {}}, asset="BTC", side="buy",
                          family=None, asset_grain=False) is None
        assert eb.key_for({"schema": 2}, asset=None, side="buy",
                          family="majors", asset_grain=True) is None


class TestConfigParse:
    def test_defaults(self, monkeypatch, tmp_path):
        for k in ("PROP_EDGE_BOOST", "PROP_EDGE_BOOST_MIN_N",
                  "PROP_EDGE_BOOST_MIN_EXPECTANCY",
                  "PROP_EDGE_BOOST_MAX_MULT"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        from prop_harness.config import PropConfig
        cfg = PropConfig.from_env()
        assert cfg.edge_boost_enabled is True
        assert cfg.edge_boost_min_n == 5
        assert cfg.edge_boost_min_expectancy == pytest.approx(0.5)
        assert cfg.edge_boost_max_mult == pytest.approx(1.5)

    def test_env_binds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        monkeypatch.setenv("PROP_EDGE_BOOST", "0")
        monkeypatch.setenv("PROP_EDGE_BOOST_MAX_MULT", "1.25")
        from prop_harness.config import PropConfig
        cfg = PropConfig.from_env()
        assert cfg.edge_boost_enabled is False
        assert cfg.edge_boost_max_mult == pytest.approx(1.25)
