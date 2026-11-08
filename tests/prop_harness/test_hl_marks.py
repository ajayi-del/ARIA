"""HL reference mark plane pins (spec §3, GO 2026-11-08).

Design law under test: HL marks are REFERENCE-ONLY. The plane must be
fail-OPEN (no data / stale / bad shapes -> mark() returns None so callers
run the legacy path bit-for-bit) and must never throw out of poll-shaped
inputs the caller cannot supervise.
"""

from __future__ import annotations

import pytest

from prop_harness.io.hl_marks import HLMarkPlane, hl_name


class _FakeSession:
    def __init__(self, mains=None, xyz=None, boom=None):
        self.mains = mains or {}
        self.xyz = xyz or {}
        self.boom = boom
        self.bodies: list[dict] = []

    def post(self, url, json, timeout):
        self.bodies.append(json)
        if self.boom:
            raise self.boom

        class _R:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                pass

            def json(self):
                return self.payload

        return _R(self.xyz if json.get("dex") == "xyz" else self.mains)


def _plane(mains=None, xyz=None, boom=None, stale_s=60.0):
    return HLMarkPlane(stale_s=stale_s,
                       session=_FakeSession(mains, xyz, boom))


class TestPoll:
    def test_merges_both_dexes(self):
        p = _plane(mains={"BTC": "75000.5"}, xyz={"xyz:GOLD": "4064.5"})
        n = p.poll(now=1000.0)
        assert n == 2
        assert p.mark("BTC", now=1001.0) == 75000.5
        assert p.mark("xyz:GOLD", now=1001.0) == 4064.5

    def test_two_dex_calls(self):
        p = _plane(mains={"BTC": "1"}, xyz={"xyz:EUR": "1.1"})
        p.poll(now=1000.0)
        assert p._session.bodies == [{"type": "allMids"},
                                     {"type": "allMids", "dex": "xyz"}]

    def test_bad_rows_dropped(self):
        p = _plane(mains={"BTC": "75000", "BAD": "nan?", "NEG": "-5",
                          "ZERO": "0"})
        p.poll(now=1000.0)
        assert p.mark("BTC", now=1001.0) == 75000.0
        assert p.mark("BAD", now=1001.0) is None
        assert p.mark("NEG", now=1001.0) is None
        assert p.mark("ZERO", now=1001.0) is None

    def test_empty_both_dexes_raises(self):
        p = _plane()
        with pytest.raises(ValueError):
            p.poll(now=1000.0)

    def test_failed_poll_keeps_last_marks(self):
        p = _plane(mains={"BTC": "75000"})
        p.poll(now=1000.0)
        p._session.boom = RuntimeError("HL 5xx")
        with pytest.raises(RuntimeError):
            p.poll()
        # old marks survive until the staleness gate retires them
        assert p.mark("BTC", now=1030.0) == 75000.0
        assert p.last_error == "HL 5xx"


class TestStalenessAbstain:
    def test_never_polled_abstains(self):
        assert _plane().mark("BTC", now=1000.0) is None

    def test_stale_abstains(self):
        p = _plane(mains={"BTC": "75000"}, stale_s=60.0)
        p.poll(now=1000.0)
        assert p.mark("BTC", now=1059.0) == 75000.0
        assert p.mark("BTC", now=1061.0) is None

    def test_unknown_asset_abstains(self):
        p = _plane(mains={"BTC": "75000"})
        p.poll(now=1000.0)
        assert p.mark("NOSUCHASSET", now=1001.0) is None

    def test_xyz_and_crypto_same_gate(self):
        p = _plane(mains={"ETH": "2200"}, xyz={"xyz:SP500": "7377"})
        p.poll(now=1000.0)
        assert p.mark("ETH", now=1001.0) == 2200.0
        assert p.mark("xyz:SP500", now=1001.0) == 7377.0
        assert p.mark("ETH", now=1200.0) is None
        assert p.mark("xyz:SP500", now=1200.0) is None


class TestNameMap:
    def test_identity_default(self):
        assert hl_name("BTC") == "BTC"
        assert hl_name("xyz:GOLD") == "xyz:GOLD"


class TestSnapshot:
    def test_snapshot_shape(self):
        p = _plane(mains={"BTC": "75000"})
        s = p.snapshot(now=1000.0)
        assert s["stale"] is True and s["n"] == 0
        p.poll(now=1000.0)
        s = p.snapshot(now=1010.0)
        assert s == {"ts": 1000.0, "n": 1, "age_s": 10.0,
                     "stale": False, "last_error": None}
