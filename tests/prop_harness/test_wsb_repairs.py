"""Pins: WS-B repairs 2-5 (2026-11-17).

2. Algedonic latch (drawdown.py): a LOCKED/BREACHED latch file WINS the
   fresh-equity recompute — the Governor's floor survives restarts;
   computed BREACHED is never downgraded; corrupt/missing → fail-open.
   Clear procedure: the Governor deletes the latch file.
3. budget.realized signed (budget.py): release() accumulates the SIGNED
   realized leg — loser closes must drive realized negative so the
   intraday budget honest-contracts.
4. MFE/MAE R-form persistence (eval_profile ratchets + executor
   track_excursions): raise-only peak, lower-only trough, first mark
   seeds the trough so a one-tick life journals 0.0.
5. loss_veto asset grain (loss_stats.py + executor._loss_veto): schema 2
   (asset, side) keys; schema≠2 under the grain → fail CLOSED to EMPTY
   (a legacy family-keyed artifact must never veto under asset keys);
   kill OFF → legacy (family, side) bit-for-bit.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from prop_harness.brains import eval_profile as ep
from prop_harness.brains import loss_stats as ls
from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor


def _dd(tmp_path: Path, **kw) -> DrawdownBrain:
    return DrawdownBrain(tmp_path / "dd.json", floor_line=1000.0,
                         floor_usd=150.0, **kw)


class TestAlgedonicLatch:
    def test_latch_locks_healthy_recompute(self, tmp_path):
        latch = tmp_path / "algedonic.json"
        latch.write_text(json.dumps({"band": "LOCKED"}))
        dd = _dd(tmp_path, latch_path=latch)
        dd.update(5000.0)  # healthy equity → computed ACTIVE
        assert dd.state.band == "LOCKED"
        assert dd.mult == 0.0
        assert dd.last_latch_enforcement == {
            "computed_band": "ACTIVE", "latched_band": "LOCKED"}

    def test_latch_breached(self, tmp_path):
        latch = tmp_path / "algedonic.json"
        latch.write_text(json.dumps({"band": "BREACHED"}))
        dd = _dd(tmp_path, latch_path=latch)
        dd.update(5000.0)
        assert dd.state.band == "BREACHED"
        assert dd.state.breached is True
        assert dd.mult == 0.0

    def test_computed_breached_never_downgraded(self, tmp_path):
        latch = tmp_path / "algedonic.json"
        latch.write_text(json.dumps({"band": "LOCKED"}))
        dd = _dd(tmp_path, latch_path=latch)
        dd.update(900.0)  # equity <= floor_line → computed BREACHED
        assert dd.state.band == "BREACHED"   # latch LOCKED cannot soften
        assert dd.state.breached is True
        assert dd.last_latch_enforcement is None

    def test_latch_disabled_legacy(self, tmp_path):
        latch = tmp_path / "algedonic.json"
        latch.write_text(json.dumps({"band": "LOCKED"}))
        dd = _dd(tmp_path, latch_path=latch, latch_enabled=False)
        dd.update(5000.0)
        assert dd.state.band == "ACTIVE"
        assert dd.mult == 1.0
        assert dd.last_latch_enforcement is None

    def test_latch_path_none_legacy(self, tmp_path):
        dd = _dd(tmp_path, latch_path=None)
        dd.update(5000.0)
        assert dd.state.band == "ACTIVE"
        assert dd.mult == 1.0

    def test_corrupt_latch_fail_open(self, tmp_path):
        latch = tmp_path / "algedonic.json"
        latch.write_text("not json{{{")
        dd = _dd(tmp_path, latch_path=latch)
        dd.update(5000.0)
        assert dd.state.band == "ACTIVE"
        assert dd.mult == 1.0

    def test_non_terminal_band_ignored(self, tmp_path):
        latch = tmp_path / "algedonic.json"
        latch.write_text(json.dumps({"band": "CAUTIOUS"}))
        dd = _dd(tmp_path, latch_path=latch)
        dd.update(5000.0)
        assert dd.state.band == "ACTIVE"

    def test_missing_latch_file(self, tmp_path):
        dd = _dd(tmp_path, latch_path=tmp_path / "absent.json")
        dd.update(5000.0)
        assert dd.state.band == "ACTIVE"
        assert dd.mult == 1.0


class TestBudgetRealizedSigned:
    def _budget(self, tmp_path: Path) -> BudgetBrain:
        b = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                        divisor=10.0, floor_budget=40.0)
        b.roll(10_000.0)
        return b

    def test_loser_close_drives_realized_negative(self, tmp_path):
        b = self._budget(tmp_path)
        b.release(10.0, -25.0)
        assert b.state.realized == pytest.approx(-25.0)
        b.release(5.0, 2.5)
        assert b.state.realized == pytest.approx(-22.5)

    def test_release_persists_signed_value(self, tmp_path):
        b = self._budget(tmp_path)
        b.release(10.0, -25.0)
        reloaded = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                               divisor=10.0, floor_budget=40.0)
        assert reloaded.state.realized == pytest.approx(-25.0)

    def test_committed_clamps_at_zero(self, tmp_path):
        b = self._budget(tmp_path)
        b.release(10.0, -25.0)   # committed was 0 → never negative
        assert b.state.committed == 0.0


def _r_bracket(**over) -> dict:
    b = {"semantic_id": "s1", "asset": "BTC", "side": "buy",
         "entry_ref": "100", "stop_price": "92", "status": "open"}
    b.update(over)
    return b


class TestExcursionRatchets:
    def test_mfe_raise_only(self):
        b = _r_bracket()
        assert ep.ratchet_mfe_peak_r(b, 104.0) == pytest.approx(0.5)
        assert ep.ratchet_mfe_peak_r(b, 103.0) == pytest.approx(0.5)
        assert ep.ratchet_mfe_peak_r(b, 108.0) == pytest.approx(1.0)

    def test_mae_lower_only_first_mark_seeds(self):
        b = _r_bracket()
        assert ep.ratchet_mae_trough_r(b, 104.0) == pytest.approx(0.5)
        assert ep.ratchet_mae_trough_r(b, 90.0) == pytest.approx(-1.25)
        assert ep.ratchet_mae_trough_r(b, 91.0) == pytest.approx(-1.25)

    def test_sell_mirror(self):
        b = _r_bracket(side="sell", stop_price="108")
        assert ep.ratchet_mfe_peak_r(b, 96.0) == pytest.approx(0.5)
        assert ep.ratchet_mae_trough_r(b, 101.0) == pytest.approx(-0.125)

    def test_bad_geometry_abstains(self):
        b = _r_bracket(entry_ref=None)
        assert ep.ratchet_mfe_peak_r(b, 104.0) is None
        assert ep.ratchet_mae_trough_r(b, 104.0) is None
        assert "mfe_peak_r" not in b and "mae_trough_r" not in b
        b2 = _r_bracket(stop_price="100")  # zero stop dist
        assert ep.ratchet_mfe_peak_r(b2, 104.0) is None


class _NoopClient:
    account_id = "acct1"

    def place_orders(self, body):
        return [{"orderId": "x"}]

    def get_open_orders(self, base=None):
        return []


def _exec(tmp_path: Path, client=None, **over) -> ProprExecutor:
    knobs = dict(
        enabled=True, state_root=tmp_path, profile="evaluation",
        asset_class="all", crypto_comove_enabled=False,
        family_cap_enabled=False, post_win_respend_enabled=False,
        cost_move_gate_enabled=False, max_positions=7,
        slot_reserve_enabled=False, eval_crypto_min_risk_enabled=False,
        crypto_min_coh_fire_enabled=False,
        eval_margin_cap_frac=0.99,
        eval_trend_budget_usd=5_000.0,
    )
    knobs.update(over)
    cfg = PropConfig(**knobs)
    (tmp_path / "journal").mkdir(exist_ok=True)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=1000.0,
                       floor_usd=150.0)
    dd.update(5000.0)
    budget = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                         divisor=10.0, floor_budget=40.0)
    budget.roll(10_000.0)
    return ProprExecutor(cfg, client or _NoopClient(), budget, dd)


class TestTrackExcursions:
    def test_writes_and_reports_mutation(self, tmp_path):
        ex = _exec(tmp_path)
        b = _r_bracket()
        assert ex.track_excursions(b, 104.0) is True
        assert b["mfe_peak_r"] == pytest.approx(0.5)
        assert b["mae_trough_r"] == pytest.approx(0.5)
        # same mark again → no mutation → no registry flush
        assert ex.track_excursions(b, 104.0) is False

    def test_kill_off_bit_for_bit(self, tmp_path):
        ex = _exec(tmp_path, mae_tracker_enabled=False)
        b = _r_bracket()
        assert ex.track_excursions(b, 104.0) is False
        assert "mfe_peak_r" not in b and "mae_trough_r" not in b

    def test_mark_none_and_closed_status(self, tmp_path):
        ex = _exec(tmp_path)
        assert ex.track_excursions(_r_bracket(), None) is False
        assert ex.track_excursions(
            _r_bracket(status="closed"), 104.0) is False


def _closed(asset="BTC", family="majors", side="buy", pnl=-10.0,
            ts=None) -> dict:
    return {"asset": asset, "family": family, "side": side,
            "realized_pnl_final": pnl,
            "closed_ts": time.time() if ts is None else ts}


class TestLossVetoGrain:
    def test_family_vs_asset_keying(self):
        rows = [_closed(asset="BTC"), _closed(asset="ETH"),
                _closed(asset="ETH")]
        fam = ls.CohortStats().build(rows)
        assert set(fam) == {"majors|buy"}
        assert fam["majors|buy"]["n"] == 3
        ast = ls.CohortStats().build(rows, grain="asset")
        assert set(ast) == {"BTC|buy", "ETH|buy"}
        assert ast["ETH|buy"]["n"] == 2

    def test_asset_grain_skips_missing_asset(self):
        rows = [_closed(asset=None), _closed(asset="BTC")]
        ast = ls.CohortStats().build(rows, grain="asset")
        assert set(ast) == {"BTC|buy"}

    def test_veto_thresholds(self):
        stats = ls.CohortStats().build([_closed(pnl=-10.0)] * 5)
        assert stats["majors|buy"]["veto"] is True
        assert stats["majors|buy"]["shrunk_expectancy"] < 0.0
        thin = ls.CohortStats().build([_closed(pnl=-10.0)] * 4)
        assert thin["majors|buy"]["veto"] is False   # n < min_n
        green = ls.CohortStats().build([_closed(pnl=+1.0)] * 5)
        assert green["majors|buy"]["veto"] is False


def _write_veto(tmp_path: Path, artifact: dict) -> None:
    prog = tmp_path / "program"
    prog.mkdir(exist_ok=True)
    (prog / "loss_veto.json").write_text(json.dumps(artifact))


_VETO_ROW = {"n": 5, "wins": 0, "pnl": -50.0, "veto": True,
             "shrunk_expectancy": -2.0}


class TestExecutorLossVeto:
    def test_grain_off_legacy_family_path(self, tmp_path):
        _write_veto(tmp_path, {"ts": time.time(),
                               "stats": {"majors|buy": dict(_VETO_ROW)}})
        ex = _exec(tmp_path)  # loss_veto_asset_grain_enabled default False
        row = ex._loss_veto("BTC", "majors", "buy")
        assert row is not None and row["veto"] is True
        assert ex._loss_veto("BTC", "majors", "sell") is None

    def test_grain_on_legacy_schema_fail_closed_empty(self, tmp_path):
        # schema≠2 artifact must NEVER veto under asset keys — even when
        # an asset-keyed-looking row happens to be present
        _write_veto(tmp_path, {"ts": time.time(),
                               "stats": {"BTC|buy": dict(_VETO_ROW)}})
        ex = _exec(tmp_path, loss_veto_asset_grain_enabled=True)
        assert ex._loss_veto("BTC", "majors", "buy") is None

    def test_grain_on_schema2_asset_keyed(self, tmp_path):
        _write_veto(tmp_path, {
            "ts": time.time(), "schema": 2,
            "stats": {"BTC|buy": dict(_VETO_ROW),
                      "majors|buy": dict(_VETO_ROW)}})
        ex = _exec(tmp_path, loss_veto_asset_grain_enabled=True)
        row = ex._loss_veto("BTC", "majors", "buy")
        assert row is not None and row["veto"] is True
        # asset grain never falls back to the family row
        assert ex._loss_veto("ETH", "majors", "buy") is None

    def test_stale_artifact_fail_open(self, tmp_path):
        _write_veto(tmp_path, {
            "ts": time.time() - 7 * 3600, "schema": 2,
            "stats": {"BTC|buy": dict(_VETO_ROW)}})
        assert _exec(tmp_path)._loss_veto("BTC", "majors", "buy") is None
        assert _exec(tmp_path, loss_veto_asset_grain_enabled=True
                     )._loss_veto("BTC", "majors", "buy") is None

    def test_loss_veto_disabled(self, tmp_path):
        _write_veto(tmp_path, {"ts": time.time(),
                               "stats": {"majors|buy": dict(_VETO_ROW)}})
        ex = _exec(tmp_path, loss_veto_enabled=False)
        assert ex._loss_veto("BTC", "majors", "buy") is None


class TestConfigParse:
    def test_wsb_defaults(self, monkeypatch, tmp_path):
        for k in ("PROP_STOP_ARBITER", "PROP_STOP_ARBITER_DUST_FRAC",
                  "PROP_ALGEDONIC_LATCH", "PROP_BUDGET_REALIZED",
                  "PROP_MAE_TRACKER", "PROP_LOSS_VETO_ASSET_GRAIN"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        cfg = PropConfig.from_env()
        assert cfg.stop_arbiter_enabled is True
        assert cfg.stop_arbiter_dust_frac == pytest.approx(0.0005)
        assert cfg.algedonic_latch_enabled is True
        assert cfg.budget_realized_enabled is True
        assert cfg.mae_tracker_enabled is True
        # grain stays Governor-armed per sleeve
        assert cfg.loss_veto_asset_grain_enabled is False

    def test_wsb_env_binds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        monkeypatch.setenv("PROP_STOP_ARBITER", "0")
        monkeypatch.setenv("PROP_ALGEDONIC_LATCH", "0")
        monkeypatch.setenv("PROP_BUDGET_REALIZED", "0")
        monkeypatch.setenv("PROP_MAE_TRACKER", "0")
        monkeypatch.setenv("PROP_LOSS_VETO_ASSET_GRAIN", "1")
        cfg = PropConfig.from_env()
        assert cfg.stop_arbiter_enabled is False
        assert cfg.algedonic_latch_enabled is False
        assert cfg.budget_realized_enabled is False
        assert cfg.mae_tracker_enabled is False
        assert cfg.loss_veto_asset_grain_enabled is True
