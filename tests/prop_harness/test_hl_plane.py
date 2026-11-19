"""HL first-party observer plane pins (task #37, 2026-11-17).

Contract under test: OBSERVER-CLASS oracle — explicit tick(), no asyncio,
fail-OPEN-None on any network/parse error with last_good retained, HL is
REFERENCE-ONLY (divergence verdicts are telemetry, never risk math).
The HTTP layer is fully mocked — no live network in tests.
"""

from __future__ import annotations

import json
import os

import pytest

from prop_harness.io.hl_plane import (
    HLDataPlane,
    divergence_bps,
    divergence_verdict,
    parse_wallets,
)

MIDS_MAIN = {"BTC": "75000.5", "ETH": "2200.25"}
MIDS_XYZ = {"xyz:GOLD": "4064.5", "xyz:SP500": "7377.0"}

SEAT = {
    "marginSummary": {"accountValue": "5123.45", "totalRawUsd": "5000.0"},
    "assetPositions": [
        {"type": "oneWay", "position": {"coin": "BTC", "szi": "0.05",
                                        "unrealizedPnl": "123.45"}},
        {"type": "oneWay", "position": {"coin": "ETH", "szi": "-1.0",
                                        "unrealizedPnl": "-23.45"}},
    ],
}
SEAT_EMPTY = {"marginSummary": {"accountValue": "0.0"}, "assetPositions": []}

CTXS = ({"universe": [{"name": "BTC"}, {"name": "ETH"}]},
        [{"funding": "0.0001", "openInterest": "12345.5"},
         {"funding": "-0.0002", "openInterest": "999.9"}])
CTXS_XYZ = ({"universe": [{"name": "GOLD"}]},
            [{"funding": "0.0", "openInterest": "10.0"}])


class _FakeSession:
    """Routes POST bodies by HL info type; boom simulates network failure."""

    def __init__(self, mids_main=None, mids_xyz=None, seat=None,
                 ctxs=None, ctxs_xyz=None, boom=None):
        self.mids_main = mids_main if mids_main is not None else MIDS_MAIN
        self.mids_xyz = mids_xyz if mids_xyz is not None else MIDS_XYZ
        self.seat = seat if seat is not None else SEAT
        self.ctxs = ctxs if ctxs is not None else CTXS
        self.ctxs_xyz = ctxs_xyz if ctxs_xyz is not None else CTXS_XYZ
        self.boom = boom
        self.bodies: list[dict] = []

    def post(self, url, json, timeout):  # noqa: A002 - matches requests API
        self.bodies.append(dict(json))
        if self.boom:
            raise self.boom

        class _R:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self.payload

        t = json.get("type")
        if t == "allMids":
            return _R(self.mids_xyz if json.get("dex") == "xyz"
                      else self.mids_main)
        if t == "clearinghouseState":
            return _R(self.seat)
        if t == "metaAndAssetCtxs":
            return _R(list(self.ctxs_xyz) if json.get("dex") == "xyz"
                      else list(self.ctxs))
        raise AssertionError(f"unexpected info type {t}")


def _plane(session=None, **kw):
    kw["session"] = session if session is not None else _FakeSession()
    return HLDataPlane(**kw)


# ---------------------------------------------------------------- marks


class TestMarks:
    def test_allmids_parsed_marks_available(self):
        p = _plane()
        out = p.tick(now=1000.0)
        assert out["ok"] is True and out["marks"] == 4
        assert p.mark("BTC", now=1001.0) == 75000.5
        assert p.mark("ETH", now=1001.0) == 2200.25
        assert p.mark("xyz:GOLD", now=1001.0) == 4064.5
        assert p.mark("xyz:SP500", now=1001.0) == 7377.0

    def test_staleness_gate_retires_marks(self):
        p = _plane(poll_s=30.0)  # stale_s = max(90, 60) = 90
        p.tick(now=1000.0)
        assert p.mark("BTC", now=1089.0) == 75000.5
        assert p.mark("BTC", now=1091.0) is None

    def test_never_polled_abstains(self):
        p = _plane()
        assert p.mark("BTC", now=1000.0) is None
        assert p.staleness(now=1000.0)["marks_stale"] is True

    def test_bad_rows_dropped(self):
        p = _plane(session=_FakeSession(
            mids_main={"BTC": "1", "BAD": "nan?", "NEG": "-5", "ZERO": "0"},
            mids_xyz={}))
        p.tick(now=1000.0)
        assert p.mark("BTC", now=1001.0) == 1.0
        assert p.mark("BAD", now=1001.0) is None
        assert p.mark("NEG", now=1001.0) is None

    def test_mark_history_bounded(self):
        p = _plane(mark_history=96)
        for t in range(120):
            p.fetch_marks(now=1000.0 + t)
        hist = p.history("BTC")
        assert len(hist) == 96
        assert hist[-1] == (1000.0 + 119, 75000.5)
        assert hist[0][0] == 1000.0 + 24  # oldest 24 evicted

    def test_ctxs_funding_parsed(self):
        p = _plane()
        p.tick(now=1000.0)
        f = p.funding("BTC", now=1001.0)
        assert f == {"funding": 0.0001, "open_interest": 12345.5}
        assert p.funding("BTC", now=2000.0) is None  # stale


# ---------------------------------------------------------------- seats


class TestSeats:
    def test_clearinghouse_parsed_equity_upnl(self):
        p = _plane(wallets=["0xABC"])
        out = p.tick(now=1000.0)
        assert out["seats"] == 1
        seat = p.equity("0xABC", now=1001.0)
        # main + xyz dex merged
        assert seat["equity"] == pytest.approx(5123.45 * 2)
        assert seat["upnl"] == pytest.approx((123.45 - 23.45) * 2)
        assert seat["positions"] == 4
        assert seat["ts"] == 1000.0

    def test_seat_staleness_gate(self):
        p = _plane(wallets=["0xABC"], poll_s=30.0)
        p.tick(now=1000.0)
        assert p.equity("0xABC", now=1089.0) is not None
        assert p.equity("0xABC", now=1091.0) is None

    def test_unknown_wallet_abstains(self):
        p = _plane(wallets=["0xABC"])
        p.tick(now=1000.0)
        assert p.equity("0xNOPE", now=1001.0) is None

    def test_no_wallets_marks_only_mode(self):
        p = _plane(wallets=[])
        out = p.tick(now=1000.0)
        assert out["seats"] == 0
        assert p.mark("BTC", now=1001.0) == 75000.5

    def test_seat_unparseable_keeps_last_good(self):
        sess = _FakeSession(seat={"marginSummary": {}})
        p = _plane(session=sess, wallets=["0xABC"])
        p.tick(now=1000.0)
        assert p.equity("0xABC", now=1001.0) is None  # never had a good read
        sess.seat = SEAT
        p.tick(now=1010.0)
        assert p.equity("0xABC", now=1011.0)["equity"] == pytest.approx(
            5123.45 * 2)


# ------------------------------------------------------------- fail-open


class TestFailOpen:
    def test_network_error_tick_never_raises(self):
        p = _plane(session=_FakeSession(boom=RuntimeError("HL down")),
                   wallets=["0xABC"])
        out = p.tick(now=1000.0)  # must not raise
        assert out["ok"] is False
        assert out["marks"] is None and out["seats"] is None
        assert p.mark("BTC", now=1001.0) is None
        assert p.equity("0xABC", now=1001.0) is None
        assert "HL down" in (p.last_error or "")

    def test_last_good_retained_after_failure(self):
        sess = _FakeSession()
        p = _plane(session=sess, poll_s=30.0)
        p.tick(now=1000.0)
        sess.boom = RuntimeError("HL 5xx")
        out = p.tick(now=1030.0)
        assert out["marks"] is None
        # last_good served until the staleness gate retires it
        assert p.mark("BTC", now=1031.0) == 75000.5
        assert p.mark("BTC", now=1091.0) is None
        assert "HL 5xx" in (p.last_error or "")  # last failing leg wins

    def test_recovery_clears_error(self):
        sess = _FakeSession(boom=RuntimeError("HL down"))
        p = _plane(session=sess)
        p.tick(now=1000.0)
        sess.boom = None
        p.tick(now=1010.0)
        assert p.last_error is None
        assert p.mark("BTC", now=1011.0) == 75000.5


# ------------------------------------------------------------ divergence


class TestDivergence:
    def test_math_both_signs(self):
        # HL higher -> positive; HL lower -> negative
        assert divergence_bps(101.0, 100.0) == pytest.approx(100.0)
        assert divergence_bps(99.0, 100.0) == pytest.approx(-100.0)
        assert divergence_bps(100.0, 100.0) == pytest.approx(0.0)

    def test_invalid_inputs_abstain(self):
        assert divergence_bps(None, 100.0) is None
        assert divergence_bps(100.0, None) is None
        assert divergence_bps(0.0, 100.0) is None
        assert divergence_bps(100.0, -1.0) is None
        assert divergence_bps("x", 100.0) is None

    def test_verdict_threshold_both_directions(self):
        v = divergence_verdict(100.6, 100.0, threshold_bps=50.0)
        assert v["suspect"] is True and v["divergence_bps"] == pytest.approx(60.0)
        v = divergence_verdict(99.4, 100.0, threshold_bps=50.0)
        assert v["suspect"] is True  # negative side trips too
        v = divergence_verdict(100.4, 100.0, threshold_bps=50.0)
        assert v["suspect"] is False
        v = divergence_verdict(None, 100.0, threshold_bps=50.0)
        assert v == {"divergence_bps": None, "suspect": False}

    def test_plane_check_divergence(self):
        p = _plane(diverge_bps=10.0)
        p.tick(now=1000.0)
        v = p.check_divergence("BTC", 75000.5, now=1001.0)
        assert v["suspect"] is False
        v = p.check_divergence("BTC", 70000.0, now=1001.0)
        assert v["suspect"] is True
        # stale plane abstains (mark None -> suspect False)
        v = p.check_divergence("BTC", 70000.0, now=2000.0)
        assert v["suspect"] is False


# -------------------------------------------------------------- snapshot


class TestSnapshot:
    def test_dump_is_atomic_valid_json(self, tmp_path):
        p = _plane(wallets=["0xABC"])
        p.tick(now=1000.0)
        path = tmp_path / "hl_plane" / "snapshot.json"
        assert p.dump_snapshot(path, now=1001.0) is True
        data = json.loads(path.read_text())  # valid JSON
        assert data["marks"]["BTC"] == 75000.5
        assert data["seats"]["0xABC"]["equity"] == pytest.approx(5123.45 * 2)
        assert data["staleness"]["marks_stale"] is False
        # atomic idiom: tmp file replaced, no .tmp left behind
        assert not path.with_suffix(".json.tmp").exists()

    def test_dump_fail_open(self, tmp_path):
        p = _plane()
        blocker = tmp_path / "occupied"
        blocker.mkdir()  # path is a directory -> write fails
        assert p.dump_snapshot(blocker, now=1000.0) is False

    def test_snapshot_before_any_poll(self, tmp_path):
        p = _plane()
        path = tmp_path / "s.json"
        assert p.dump_snapshot(path, now=1000.0) is True
        data = json.loads(path.read_text())
        assert data["n_marks"] == 0 and data["staleness"]["marks_stale"] is True


# ----------------------------------------------------------------- knobs


class TestKnobs:
    @pytest.fixture(autouse=True)
    def _clean_prop_env(self, monkeypatch, tmp_path):
        for k in list(os.environ):
            if k.startswith("PROP_"):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        monkeypatch.setenv("PROP_ENABLED", "false")
        yield

    def test_defaults(self):
        from prop_harness.config import PropConfig
        cfg = PropConfig.from_env()
        assert cfg.hl_plane_enabled is True
        assert cfg.hl_plane_wallets == ""
        assert cfg.hl_plane_poll_s == 30.0
        assert cfg.hl_plane_diverge_bps == 50.0

    def test_env_override(self, monkeypatch):
        from prop_harness.config import PropConfig
        monkeypatch.setenv("PROP_HL_PLANE", "0")
        monkeypatch.setenv("PROP_HL_PLANE_WALLETS", "0xA, 0xB")
        monkeypatch.setenv("PROP_HL_PLANE_POLL_S", "15")
        monkeypatch.setenv("PROP_HL_PLANE_DIVERGE_BPS", "25")
        cfg = PropConfig.from_env()
        assert cfg.hl_plane_enabled is False
        assert cfg.hl_plane_wallets == "0xA, 0xB"
        assert cfg.hl_plane_poll_s == 15.0
        assert cfg.hl_plane_diverge_bps == 25.0
        assert parse_wallets(cfg.hl_plane_wallets) == ["0xA", "0xB"]
