"""Pins: pre-deploy fix pack (CEO S5 + audit amendments, 2026-11-17).

C4  close_pnl_net_from_trades — net = gross − fees(all legs) − funding;
    fees_seen=False → net == gross == legacy close_pnl_from_trades.
C4  stamp_close_outcome restamp window — venue-lag first stamps upgrade
    inside close_restamp_window_s; outside the window first-write-wins
    holds bit-for-bit.
C9  publish_loss_veto ALWAYS writes the seat's OWN root
    (loss_veto_write_artifact), even with PROP_LOSS_VETO_PATH set —
    a consumer never pollutes the shared probe artifact.
C10 CohortStats.build account_id partition (missing-id rows excluded
    under an active filter) + epoch_ts warm-start rebase.
C12 boost_mult_r — R-based gate (size-invariant) + n-ramp to full_n;
    vetoed_recently quarantine; vetoed_ts carry-forward in the payload.
A9  HL jump guard — a >jump_frac mark move is quarantined, last good
    print survives, counter ticks; 0.0 disables.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from prop_harness.brains import edge_boost as eb
from prop_harness.brains import loss_stats as ls
from prop_harness.brains import stop_arbiter as sa
from prop_harness.config import PropConfig
from prop_harness.io.hl_plane import HLDataPlane


# ── C4: net-PnL from trades ──────────────────────────────────────────────

_OPEN = 1_700_000_000.0


def _iso(ts: float) -> str:
    # venue wire format: ISO-8601 string (parse_executed_at reads strings
    # only — a float ts reads as "unknown" and is never pre-open-excluded)
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _trade(ttype="reduce", pnl=None, ts=_OPEN + 10, **kw):
    row = {"type": ttype, "executedAt": _iso(ts)}
    if pnl is not None:
        row["realizedPnl"] = str(pnl)
    row.update(kw)
    return row


class TestClosePnlNet:
    def test_fees_subtracted_on_all_legs(self):
        trades = [
            _trade("open", None, fee="0.50"),          # opening taker fee
            _trade("reduce", 10.0, fee="0.09"),        # closing leg
        ]
        net, seen = ls.close_pnl_net_from_trades(trades, _OPEN)
        assert seen is True
        assert net == pytest.approx(10.0 - 0.50 - 0.09)

    def test_funding_subtracted(self):
        trades = [
            _trade("close", 5.0, fee="0.10"),
            _trade("funding", None, funding="0.25"),
        ]
        net, seen = ls.close_pnl_net_from_trades(trades, _OPEN)
        assert seen is True
        assert net == pytest.approx(5.0 - 0.10 - 0.25)

    def test_no_fee_fields_legacy_gross_bit_for_bit(self):
        trades = [_trade("open"), _trade("reduce", 7.5),
                  _trade("reduce", -2.0)]
        net, seen = ls.close_pnl_net_from_trades(trades, _OPEN)
        assert seen is False
        assert net == pytest.approx(5.5)
        assert ls.close_pnl_from_trades(trades, _OPEN) == pytest.approx(5.5)

    def test_no_closing_leg_none(self):
        net, seen = ls.close_pnl_net_from_trades([_trade("open")], _OPEN)
        assert net is None

    def test_pre_open_rows_excluded(self):
        trades = [_trade("reduce", 99.0, ts=_OPEN - 100, fee="1.0"),
                  _trade("reduce", 3.0, fee="0.10")]
        net, _ = ls.close_pnl_net_from_trades(trades, _OPEN)
        assert net == pytest.approx(3.0 - 0.10)

    def test_alternate_fee_field_names(self):
        trades = [_trade("close", 4.0, commission="0.20"),
                  _trade("close", 1.0, feeUsd="0.05")]
        net, seen = ls.close_pnl_net_from_trades(trades, _OPEN)
        assert seen is True
        assert net == pytest.approx(5.0 - 0.25)


class TestRestampWindow:
    def test_legacy_first_write_wins_without_window(self):
        b = {"closed_ts": _OPEN, "realized_pnl_final": 1.0}
        assert ls.stamp_close_outcome(b, realized_pnl_final=9.0) is False
        assert b["realized_pnl_final"] == 1.0
        assert "realized_pnl_restamped" not in b

    def test_within_window_restamps(self):
        now = time.time()
        b = {"closed_ts": now - 60.0, "realized_pnl_final": 1.0}
        assert ls.stamp_close_outcome(b, realized_pnl_final=9.0,
                                      restamp_window_s=600.0, now=now,
                                      realized_pnl_source="trades_net")
        assert b["realized_pnl_final"] == 9.0
        assert b["realized_pnl_restamped"] is True
        assert b["realized_pnl_source"] == "trades_net"

    def test_outside_window_refuses(self):
        now = time.time()
        b = {"closed_ts": now - 700.0, "realized_pnl_final": 1.0}
        assert ls.stamp_close_outcome(b, realized_pnl_final=9.0,
                                      restamp_window_s=600.0,
                                      now=now) is False
        assert b["realized_pnl_final"] == 1.0

    def test_same_value_no_flag(self):
        now = time.time()
        b = {"closed_ts": now - 60.0, "realized_pnl_final": 1.0}
        assert ls.stamp_close_outcome(b, realized_pnl_final=1.0,
                                      restamp_window_s=600.0,
                                      now=now) is False

    def test_account_id_stamped_once(self):
        b = {"closed_ts": _OPEN}
        assert ls.stamp_close_outcome(b, account_id="acctA")
        assert b["account_id"] == "acctA"
        assert ls.stamp_close_outcome(b, account_id="acctB") is False
        assert b["account_id"] == "acctA"

    def test_booked_fallback_source(self):
        b = {"closed_ts": _OPEN, "realized_booked": -3.0}
        assert ls.stamp_close_outcome(b)
        assert b["realized_pnl_final"] == -3.0
        assert b["realized_pnl_source"] == "booked_fallback"


# ── C10: account partition + epoch + R-form ──────────────────────────────


def _closed(asset="BTC", family="majors", side="buy", pnl=-10.0,
            ts=None, **kw):
    row = {"asset": asset, "family": family, "side": side,
           "realized_pnl_final": pnl,
           "closed_ts": time.time() if ts is None else ts}
    row.update(kw)
    return row


class TestCohortPartition:
    def test_account_filter_excludes_mismatch_and_missing(self):
        rows = [_closed(account_id="A"), _closed(account_id="acctB"),
                _closed()]   # no account_id
        stats = ls.CohortStats().build(rows, account_id="A")
        assert stats["majors|buy"]["n"] == 1

    def test_account_none_legacy_unpartitioned(self):
        rows = [_closed(account_id="A"), _closed()]
        stats = ls.CohortStats().build(rows, account_id=None)
        assert stats["majors|buy"]["n"] == 2

    def test_epoch_skips_older_rows(self):
        now = time.time()
        rows = [_closed(ts=now - 100.0), _closed(ts=now - 10.0)]
        stats = ls.CohortStats().build(rows, now=now, epoch_ts=now - 50.0)
        assert stats["majors|buy"]["n"] == 1

    def test_r_form_fields(self):
        rows = [_closed(pnl=5.0, risk_usd=10.0),
                _closed(pnl=-5.0, risk_usd=20.0)]
        c = ls.CohortStats(k=20.0).build(rows)["majors|buy"]
        assert c["n_r"] == 2
        assert c["pnl_r"] == pytest.approx(0.5 - 0.25)
        assert c["expectancy_r"] == pytest.approx(0.125)
        assert c["shrunk_expectancy_r"] == pytest.approx(0.25 / 22.0)
        assert c["mean_risk"] == pytest.approx(15.0)

    def test_r_form_absent_without_risk(self):
        c = ls.CohortStats().build([_closed()])["majors|buy"]
        assert c["n_r"] == 0
        assert "shrunk_expectancy_r" not in c


class TestArtifactFreshEvidenceAge:
    def test_fresh_heartbeat_stale_evidence_fails(self):
        now = time.time()
        art = {"ts": now,
               "stats": {"majors|buy": {"last_close_ts": now - 10_000.0}}}
        assert ls.artifact_fresh(art, now=now) is True      # legacy
        assert ls.artifact_fresh(art, now=now,
                                 evidence_max_age_s=3600.0) is False

    def test_fresh_evidence_passes(self):
        now = time.time()
        art = {"ts": now,
               "stats": {"majors|buy": {"last_close_ts": now - 100.0}}}
        assert ls.artifact_fresh(art, now=now,
                                 evidence_max_age_s=3600.0) is True

    def test_no_evidence_rows_fail_closed_under_gate(self):
        now = time.time()
        assert ls.artifact_fresh({"ts": now, "stats": {}}, now=now,
                                 evidence_max_age_s=3600.0) is False


# ── C12: R-based boost + quarantine ──────────────────────────────────────


def _r_row(n_r=20, se_r=0.30):
    return {"n": n_r, "n_r": n_r, "pnl_r": se_r * n_r,
            "shrunk_expectancy_r": se_r, "shrunk_expectancy": 1.0}


class TestBoostMultR:
    def test_thin_n_no_boost(self):
        assert eb.boost_mult_r(_r_row(n_r=4), min_n=5) == 1.0

    def test_below_min_r_no_boost(self):
        assert eb.boost_mult_r(_r_row(se_r=0.05), min_r=0.10) == 1.0

    def test_full_mult_at_saturation(self):
        m = eb.boost_mult_r(_r_row(n_r=20, se_r=0.20), min_r=0.10,
                            cap=1.5, full_n=20)
        assert m == pytest.approx(1.5)

    def test_n_ramp_partial_between_min_and_full(self):
        # se_r saturated (frac=1.0 pre-ramp); n_r=5 of min_n=5, full_n=20
        # → ramp (5-5+1)/(20-5+1) = 1/16
        m = eb.boost_mult_r(_r_row(n_r=5, se_r=0.50), min_n=5, min_r=0.10,
                            cap=1.5, full_n=20)
        assert m == pytest.approx(1.0 + 0.5 * (1.0 / 16.0))

    def test_cap_clamped_floor_one(self):
        assert eb.boost_mult_r(_r_row(), cap=0.5) == 1.0

    def test_boost_for_r_unearned_returns_none_row(self):
        mult, row = eb.boost_for_r({"BTC|buy": _r_row(n_r=2)}, "BTC|buy")
        assert mult == 1.0 and row is None

    def test_dollar_path_untouched(self):
        row = {"n": 20, "shrunk_expectancy": 1.0}
        assert eb.boost_mult(row, min_expectancy=0.5,
                             cap=1.5) == pytest.approx(1.5)


class TestVetoedRecently:
    def test_within_window_blocks(self):
        art = {"vetoed_ts": {"BTC|buy": 1000.0}}
        assert eb.vetoed_recently(art, "BTC|buy", now=1100.0,
                                  quarantine_s=600.0) is True

    def test_outside_window_allows(self):
        art = {"vetoed_ts": {"BTC|buy": 1000.0}}
        assert eb.vetoed_recently(art, "BTC|buy", now=1700.0,
                                  quarantine_s=600.0) is False

    def test_missing_map_or_key_fails_open(self):
        assert eb.vetoed_recently({}, "BTC|buy", now=1.0,
                                  quarantine_s=600.0) is False
        assert eb.vetoed_recently({"vetoed_ts": {}}, "BTC|buy", now=1.0,
                                  quarantine_s=600.0) is False

    def test_zero_quarantine_disabled(self):
        art = {"vetoed_ts": {"BTC|buy": 1000.0}}
        assert eb.vetoed_recently(art, "BTC|buy", now=1001.0,
                                  quarantine_s=0.0) is False


# ── stop_arbiter: eps_abs dust path ──────────────────────────────────────


class TestPastDustEpsAbs:
    def test_eps_abs_replaces_frac(self):
        # frac eps would be |100|*0.0005=0.05; eps_abs 0.5 is wider →
        # a +0.3 move fires under frac but suppresses under eps_abs.
        assert sa.past_dust("buy", 100.3, 100.0, 0.0005) is True
        assert sa.past_dust("buy", 100.3, 100.0, 0.0005,
                            eps_abs=0.5) is False
        assert sa.past_dust("buy", 100.6, 100.0, 0.0005,
                            eps_abs=0.5) is True

    def test_eps_abs_short_mirrored(self):
        assert sa.past_dust("sell", 99.7, 100.0, 0.0005,
                            eps_abs=0.5) is False
        assert sa.past_dust("sell", 99.4, 100.0, 0.0005,
                            eps_abs=0.5) is True

    def test_none_eps_abs_legacy(self):
        assert sa.past_dust("buy", 100.06, 100.0, 0.0005,
                            eps_abs=None) is True


# ── A9: HL jump guard ────────────────────────────────────────────────────


class _JumpPlane(HLDataPlane):
    def __init__(self, mids_seq, **kw):
        super().__init__(**kw)
        self._seq = list(mids_seq)

    def _all_mids(self, dex):
        if dex is not None:  # fetch_marks polls (None, "xyz") per call
            return {}
        if not self._seq:
            return {}
        return self._seq.pop(0)


class TestHLJumpGuard:
    def test_jump_quarantined_keeps_prev(self):
        p = _JumpPlane([{"BTC": "100"}, {"BTC": "200"}], jump_frac=0.15)
        assert p.fetch_marks(now=1000.0) == 1
        assert p.fetch_marks(now=1010.0) == 1
        assert p.mark("BTC", now=1010.0) == pytest.approx(100.0)
        assert p._jump_quarantined == 1

    def test_normal_move_accepted(self):
        p = _JumpPlane([{"BTC": "100"}, {"BTC": "110"}], jump_frac=0.15)
        p.fetch_marks(now=1000.0)
        p.fetch_marks(now=1010.0)
        assert p.mark("BTC", now=1010.0) == pytest.approx(110.0)
        assert p._jump_quarantined == 0

    def test_disabled_legacy(self):
        p = _JumpPlane([{"BTC": "100"}, {"BTC": "600"}], jump_frac=0.0)
        p.fetch_marks(now=1000.0)
        p.fetch_marks(now=1010.0)
        assert p.mark("BTC", now=1010.0) == pytest.approx(600.0)

    def test_snapshot_carries_counter(self):
        p = _JumpPlane([{"BTC": "100"}, {"BTC": "200"}], jump_frac=0.15)
        p.fetch_marks(now=1000.0)
        p.fetch_marks(now=1010.0)
        assert p.snapshot(now=1010.0)["jump_quarantined"] == 1


# ── config: new knobs parse + C9 write path ──────────────────────────────


class TestConfigFixpack:
    def test_defaults(self, monkeypatch, tmp_path):
        for k in ("PROP_STOP_ARBITER_DUST_R", "PROP_EDGE_BOOST_R_BASED",
                  "PROP_EDGE_BOOST_MIN_R", "PROP_EDGE_BOOST_FULL_N",
                  "PROP_EDGE_BOOST_VETO_QUARANTINE_S",
                  "PROP_EDGE_BOOST_REQUIRES_VETO",
                  "PROP_COHORT_ACCOUNT_FILTER", "PROP_COHORT_EPOCH_TS",
                  "PROP_CLOSE_RESTAMP_WINDOW_S",
                  "PROP_REBOUND_SUPPRESS_LOG_TTL_S",
                  "PROP_HL_PLANE_JUMP_FRAC", "PROP_LOSS_VETO_PATH"):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        cfg = PropConfig.from_env()
        assert cfg.stop_arbiter_dust_r == pytest.approx(0.02)
        assert cfg.edge_boost_r_based_enabled is True
        assert cfg.edge_boost_min_r == pytest.approx(0.10)
        assert cfg.edge_boost_full_n == 20
        assert cfg.edge_boost_veto_quarantine_s == pytest.approx(21600.0)
        assert cfg.edge_boost_requires_veto is True
        assert cfg.cohort_account_filter_enabled is True
        assert cfg.cohort_epoch_ts == pytest.approx(0.0)
        assert cfg.close_restamp_window_s == pytest.approx(600.0)
        assert cfg.rebound_suppress_log_ttl_s == pytest.approx(300.0)
        assert cfg.hl_plane_jump_frac == pytest.approx(0.15)

    def test_env_binds(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        monkeypatch.setenv("PROP_STOP_ARBITER_DUST_R", "0.05")
        monkeypatch.setenv("PROP_EDGE_BOOST_R_BASED", "0")
        monkeypatch.setenv("PROP_EDGE_BOOST_REQUIRES_VETO", "0")
        monkeypatch.setenv("PROP_COHORT_ACCOUNT_FILTER", "0")
        monkeypatch.setenv("PROP_COHORT_EPOCH_TS", "1700000000")
        monkeypatch.setenv("PROP_CLOSE_RESTAMP_WINDOW_S", "120")
        monkeypatch.setenv("PROP_HL_PLANE_JUMP_FRAC", "0.25")
        cfg = PropConfig.from_env()
        assert cfg.stop_arbiter_dust_r == pytest.approx(0.05)
        assert cfg.edge_boost_r_based_enabled is False
        assert cfg.edge_boost_requires_veto is False
        assert cfg.cohort_account_filter_enabled is False
        assert cfg.cohort_epoch_ts == pytest.approx(1700000000.0)
        assert cfg.close_restamp_window_s == pytest.approx(120.0)
        assert cfg.hl_plane_jump_frac == pytest.approx(0.25)

    def test_c9_write_path_ignores_shared_read_path(self, tmp_path):
        cfg = PropConfig(state_root=tmp_path,
                         loss_veto_path="/tmp/shared/loss_veto.json")
        assert str(cfg.loss_veto_artifact) == "/tmp/shared/loss_veto.json"
        assert (cfg.loss_veto_write_artifact
                == tmp_path / "program" / "loss_veto.json")


# ── C9/C10/C12 publisher: Harness.publish_loss_veto via fake self ────────


class _PubClient:
    account_id = "acctA"

    def __init__(self, trades=None):
        self._trades = trades or []

    def get_trades(self, base=None):  # noqa: ARG002 - stub
        return list(self._trades)


def _pub_harness(tmp_path: Path, client, **over):
    """Minimal Harness-shaped namespace for publish_loss_veto."""
    from prop_harness.main import Harness
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
    knobs.update(over.pop("config_over", {}))
    cfg = PropConfig(**knobs)
    (tmp_path / "journal").mkdir(exist_ok=True)
    from prop_harness.brains.budget import BudgetBrain
    from prop_harness.brains.drawdown import DrawdownBrain
    from prop_harness.executor import ProprExecutor
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=1000.0,
                       floor_usd=150.0)
    budget = BudgetBrain(tmp_path / "budget.json", hard_zero=30.0,
                         divisor=10.0, floor_budget=40.0)
    ex = ProprExecutor(cfg, client, budget, dd)
    fake = SimpleNamespace(config=cfg, executor=ex, client=client)
    return fake, Harness.publish_loss_veto


class TestPublishLossVeto:
    def _seed_registry(self, tmp_path, brackets, fake):
        path = fake.executor.registry_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"brackets": brackets}))

    def test_consumer_seat_still_writes_own_root(self, tmp_path):
        now = time.time()
        client = _PubClient()
        fake, meth = _pub_harness(
            tmp_path, client,
            config_over={"loss_veto_path": "/tmp/shared_probe/loss_veto.json"})
        self._seed_registry(tmp_path, [
            {"status": "closed_external", "asset": "BTC", "side": "buy",
             "family": "majors", "realized_pnl_final": -10.0,
             "closed_ts": now - 100.0, "account_id": "acctA"}], fake)
        meth(fake)
        own = tmp_path / "program" / "loss_veto.json"
        assert own.exists()
        payload = json.loads(own.read_text())
        assert payload["stats"]["majors|buy"]["n"] == 1
        assert not Path("/tmp/shared_probe/loss_veto.json").exists()

    def test_vetoed_ts_stamped_and_carried_forward(self, tmp_path):
        now = time.time()
        row = {"status": "closed_external", "asset": "BTC", "side": "buy",
               "family": "majors", "realized_pnl_final": -10.0,
               "closed_ts": now - 100.0, "account_id": "acctA"}
        client = _PubClient()
        fake, meth = _pub_harness(tmp_path, client)
        self._seed_registry(tmp_path, [dict(row) for _ in range(5)], fake)
        meth(fake)
        own = tmp_path / "program" / "loss_veto.json"
        p1 = json.loads(own.read_text())
        assert p1["stats"]["majors|buy"]["veto"] is True
        first_ts = p1["vetoed_ts"]["majors|buy"]
        assert first_ts > 0
        # Row leaves veto (fresh green evidence) — the entry persists so
        # the quarantine extends past the final veto observation.
        self._seed_registry(tmp_path, [
            {"status": "closed_external", "asset": "BTC", "side": "buy",
             "family": "majors", "realized_pnl_final": 500.0,
             "closed_ts": now - 10.0, "account_id": "acctA"}], fake)
        meth(fake)
        p2 = json.loads(own.read_text())
        assert p2["stats"]["majors|buy"]["veto"] is False
        assert p2["vetoed_ts"]["majors|buy"] == first_ts

    def test_account_filter_partitions_evidence(self, tmp_path):
        now = time.time()
        client = _PubClient()
        fake, meth = _pub_harness(tmp_path, client)
        self._seed_registry(tmp_path, [
            {"status": "closed_external", "asset": "BTC", "side": "buy",
             "family": "majors", "realized_pnl_final": -10.0,
             "closed_ts": now - 100.0, "account_id": "acctOTHER"}], fake)
        meth(fake)
        payload = json.loads(
            (tmp_path / "program" / "loss_veto.json").read_text())
        # The foreign account's close never votes in this seat's artifact.
        assert payload["stats"] == {}

    def test_restamp_heal_refetches_inside_window(self, tmp_path):
        now = time.time()
        # First stamp was gross 9.0 (venue lag); the heal re-reads trades
        # and the net (10.0 - 0.5 fee = 9.5) replaces it inside the window.
        trades = [{"type": "close", "executedAt": now - 35.0,
                   "realizedPnl": "10.0", "fee": "0.5"}]
        client = _PubClient(trades)
        fake, meth = _pub_harness(
            tmp_path, client, config_over={"close_restamp_window_s": 600.0})
        self._seed_registry(tmp_path, [
            {"status": "closed_external", "asset": "BTC", "side": "buy",
             "family": "majors", "realized_pnl_final": 9.0,
             "realized_pnl_source": "trades_gross",
             "closed_ts": now - 30.0, "opened_ts": now - 300.0,
             "account_id": "acctA"}], fake)
        meth(fake)
        reg = json.loads(fake.executor.registry_path.read_text())
        b = reg["brackets"][0]
        assert b["realized_pnl_final"] == pytest.approx(9.5)
        assert b["realized_pnl_restamped"] is True
        assert b["realized_pnl_source"] == "trades_net"
