"""tests/test_bybit_lens.py — pins for intelligence/bybit_lens.py.

Covers: V5 parsing (fail-closed per-row), symbol mapping both directions,
all three verdict kinds + silence, basis notes both directions, abstain
legs, the kill switch, and the READ-ONLY structural guarantee (meta-test:
no public-API callable parameter named executor/order/place/cancel).
"""
import inspect
from types import SimpleNamespace

import pytest

from intelligence.bybit_lens import (
    BASIS_WINDOW_S,
    AccountSnapshot,
    BasisNote,
    BybitLens,
    EVENT_BASIS_NOTE,
    EVENT_CONFIRM,
    EVENT_CONFLICT,
    EVENT_CROWD_EXTREME,
    EVENT_POLL_ERROR,
    LensVerdict,
    PublicPlane,
    poll_error_event,
    to_bybit,
    to_canonical,
)

NOW = 1_760_000_000.0


def cfg(**kw):
    base = dict(bybit_lens_enabled=True,
                bybit_lens_min_operator_notional=50.0,
                bybit_lens_funding_extreme=0.0005,
                bybit_lens_oi_spike_pct=15.0,
                bybit_lens_basis_note_pct=0.0015)
    base.update(kw)
    return SimpleNamespace(**base)


def pos_row(symbol="BTCUSDT", side="Buy", size="0.5", avg="100000.0",
            mark="100500.0", upnl="250.0", lev="10"):
    return {"symbol": symbol, "side": side, "size": size, "avgPrice": avg,
            "markPrice": mark, "unrealisedPnl": upnl, "leverage": lev}


def account_with(*rows, equity=1000.0, margin=200.0):
    lens = BybitLens()
    return lens.poll_account(list(rows),
                             {"totalEquity": str(equity),
                              "totalInitialMargin": str(margin)}, NOW)


def book(symbol="BTC-USD", side="short", margin=100.0, roe_pct=-2.0):
    return [{"symbol": symbol, "side": side, "margin": margin,
             "roe_pct": roe_pct}]


# ── Symbol mapping ───────────────────────────────────────────────────────────

class TestMapping:
    def test_wire_to_canonical(self):
        assert to_canonical("BTCUSDT") == "BTC-USD"

    def test_wire_1000_prefix(self):
        assert to_canonical("1000PEPEUSDT") == "1000PEPE-USD"

    def test_canonical_passthrough(self):
        assert to_canonical("BTC-USD") == "BTC-USD"
        assert to_canonical("1000PEPE-USD") == "1000PEPE-USD"

    def test_canonical_to_wire(self):
        assert to_bybit("BTC-USD") == "BTCUSDT"
        assert to_bybit("1000PEPE-USD") == "1000PEPEUSDT"

    def test_wire_passthrough(self):
        assert to_bybit("BTCUSDT") == "BTCUSDT"

    def test_roundtrip(self):
        assert to_bybit(to_canonical("ETHUSDT")) == "ETHUSDT"
        assert to_canonical(to_bybit("ETH-USD")) == "ETH-USD"

    def test_empty_safe(self):
        assert to_canonical("") == ""
        assert to_bybit("") == ""
        assert to_canonical(None) == ""


# ── Account parsing ──────────────────────────────────────────────────────────

class TestPollAccount:
    def test_happy_long(self):
        snap = account_with(pos_row())
        assert len(snap.positions) == 1
        p = snap.positions[0]
        assert p.symbol == "BTC-USD" and p.bybit_symbol == "BTCUSDT"
        assert p.side == "long" and p.qty == 0.5
        assert p.entry == 100000.0 and p.mark == 100500.0
        assert p.upnl == 250.0 and p.leverage == 10.0
        assert p.notional == pytest.approx(0.5 * 100500.0)

    def test_happy_short(self):
        snap = account_with(pos_row(symbol="ETHUSDT", side="Sell",
                                    size="2.0", avg="4000", mark="3990"))
        assert snap.positions[0].side == "short"
        assert snap.positions[0].symbol == "ETH-USD"

    def test_zero_size_skipped_not_malformed(self):
        snap = account_with(pos_row(size="0"), pos_row(symbol="ETHUSDT"))
        assert len(snap.positions) == 1
        assert snap.skipped_rows == 0

    def test_malformed_row_skipped_rest_parsed(self):
        snap = account_with(pos_row(size="not-a-number"),
                            {"symbol": "SOLUSDT", "side": "Buy"},
                            pos_row(symbol="ETHUSDT"))
        assert len(snap.positions) == 1
        assert snap.positions[0].symbol == "ETH-USD"
        assert snap.skipped_rows == 2

    def test_unknown_side_skipped(self):
        snap = account_with(pos_row(side="HOLD"), pos_row(symbol="ETHUSDT"))
        assert len(snap.positions) == 1
        assert snap.skipped_rows == 1

    def test_missing_symbol_skipped(self):
        snap = account_with({"side": "Buy", "size": "1"}, pos_row())
        assert len(snap.positions) == 1
        assert snap.skipped_rows == 1

    def test_non_dict_row_skipped(self):
        snap = account_with("garbage", pos_row())
        assert len(snap.positions) == 1
        assert snap.skipped_rows == 1

    def test_balance_flat(self):
        snap = account_with(pos_row(), equity=1234.5, margin=111.0)
        assert snap.equity == 1234.5 and snap.margin_used == 111.0

    def test_balance_nested_list(self):
        lens = BybitLens()
        snap = lens.poll_account([pos_row()],
                                 {"list": [{"totalEquity": "777.0",
                                            "totalInitialMargin": "88.0"}]},
                                 NOW)
        assert snap.equity == 777.0 and snap.margin_used == 88.0

    def test_balance_missing_no_crash(self):
        lens = BybitLens()
        snap = lens.poll_account([pos_row()], {}, NOW)
        assert snap.equity == 0.0 and snap.margin_used == 0.0
        assert len(snap.positions) == 1

    def test_empty_account(self):
        lens = BybitLens()
        snap = lens.poll_account([], {"totalEquity": "500"}, NOW)
        assert snap.positions == () and snap.equity == 500.0

    def test_position_for_lookup(self):
        snap = account_with(pos_row(), pos_row(symbol="ETHUSDT"))
        assert snap.position_for("ETH-USD").side == "long"
        assert snap.position_for("ETHUSDT").symbol == "ETH-USD"
        assert snap.position_for("SOL-USD") is None

    def test_notional_falls_back_to_entry_when_mark_missing(self):
        row = pos_row(mark="")
        snap = account_with(row)
        assert snap.positions[0].notional == pytest.approx(0.5 * 100000.0)

    def test_last_account_stored(self):
        lens = BybitLens()
        assert lens.last_account is None
        snap = lens.poll_account([pos_row()], {}, NOW)
        assert lens.last_account is snap


# ── Public plane parsing ─────────────────────────────────────────────────────

class TestPollPublic:
    def test_raw_v5_keys(self):
        lens = BybitLens()
        plane = lens.poll_public({"BTCUSDT": {
            "openInterest": "45000.0", "fundingRate": "0.0001",
            "turnover24h": "9.5e8", "price24hPcnt": "0.023",
            "markPrice": "100500.0"}}, NOW)
        p = plane.plane_for("BTC-USD")
        assert p.open_interest == 45000.0
        assert p.funding_rate == 0.0001
        assert p.turnover_24h == 9.5e8
        assert p.price_24h_change_pct == pytest.approx(2.3)
        assert p.mark_price == 100500.0

    def test_normalized_feed_keys_derive_oi_change(self):
        lens = BybitLens()
        plane = lens.poll_public({"ETH-USD": {
            "open_interest": 120.0, "prev_open_interest": 100.0,
            "funding_rate": -0.0008, "mark_price": 3990.0}}, NOW)
        p = plane.plane_for("ETH-USD")
        assert p.oi_24h_change_pct == pytest.approx(20.0)
        assert p.funding_rate == -0.0008

    def test_explicit_oi_change_key_wins(self):
        lens = BybitLens()
        plane = lens.poll_public({"BTCUSDT": {
            "openInterest": "110", "prev_open_interest": 100,
            "oi_24h_change_pct": 25.0, "fundingRate": "0.001"}}, NOW)
        assert plane.plane_for("BTC-USD").oi_24h_change_pct == 25.0

    def test_missing_oi_evidence_abstains(self):
        lens = BybitLens()
        plane = lens.poll_public({"BTCUSDT": {"fundingRate": "0.001",
                                              "openInterest": "100"}}, NOW)
        assert plane.plane_for("BTC-USD").oi_24h_change_pct is None

    def test_malformed_ticker_skipped(self):
        lens = BybitLens()
        plane = lens.poll_public({"BTCUSDT": "garbage",
                                  "ETHUSDT": {"fundingRate": "0.0001"}}, NOW)
        assert plane.plane_for("BTC-USD") is None
        assert plane.plane_for("ETH-USD") is not None


# ── Verdicts: conflict / confirm / silence ───────────────────────────────────

class TestHedgeVerdictAccount:
    def _public(self):
        return PublicPlane(ts=NOW, planes={})

    def test_conflict_fires_at_size(self):
        acct = account_with(pos_row(side="Buy", size="0.01",
                                    mark="100000"))  # $1000 notional
        out = BybitLens().hedge_verdict(
            cfg(), account=acct, sodex_book=book(side="short"),
            public=self._public(), now_ts=NOW)
        assert len(out) == 1
        v = out[0]
        assert v.kind == "conflict" and v.event == EVENT_CONFLICT
        assert v.symbol == "BTC-USD" and v.side == "short"
        assert v.payload["bybit_side"] == "long"
        assert v.payload["bybit_notional"] == pytest.approx(1000.0)

    def test_conflict_suppressed_below_min_notional(self):
        acct = account_with(pos_row(side="Buy", size="0.0003",
                                    mark="100000"))  # $30 < $50
        out = BybitLens().hedge_verdict(
            cfg(), account=acct, sodex_book=book(side="short"),
            public=self._public(), now_ts=NOW)
        assert out == []

    def test_conflict_at_exactly_min_notional_fires(self):
        acct = account_with(pos_row(side="Buy", size="0.0005",
                                    mark="100000"))  # exactly $50
        out = BybitLens().hedge_verdict(
            cfg(), account=acct, sodex_book=book(side="short"),
            public=self._public(), now_ts=NOW)
        assert [v.kind for v in out] == ["conflict"]

    def test_confirm_same_side(self):
        acct = account_with(pos_row(side="Buy"))
        out = BybitLens().hedge_verdict(
            cfg(), account=acct, sodex_book=book(side="long"),
            public=self._public(), now_ts=NOW)
        assert len(out) == 1
        assert out[0].kind == "confirm" and out[0].event == EVENT_CONFIRM

    def test_empty_account_no_verdicts_no_crash(self):
        acct = account_with()  # no positions
        out = BybitLens().hedge_verdict(
            cfg(), account=acct, sodex_book=book(), public=self._public(),
            now_ts=NOW)
        assert out == []

    def test_empty_sodex_book_empty_verdicts(self):
        acct = account_with(pos_row())
        out = BybitLens().hedge_verdict(
            cfg(), account=acct, sodex_book=[], public=self._public(),
            now_ts=NOW)
        assert out == []

    def test_1000pepe_mapping_conflict(self):
        acct = account_with(pos_row(symbol="1000PEPEUSDT", side="Sell",
                                    size="1000000", mark="0.01"))  # $10k
        out = BybitLens().hedge_verdict(
            cfg(), account=acct,
            sodex_book=book(symbol="1000PEPE-USD", side="long"),
            public=self._public(), now_ts=NOW)
        assert [v.kind for v in out] == ["conflict"]

    def test_malformed_book_row_skipped(self):
        acct = account_with(pos_row())
        rows = book(side="short") + [{"side": "long"}, "garbage"]
        out = BybitLens().hedge_verdict(
            cfg(), account=acct, sodex_book=rows, public=self._public(),
            now_ts=NOW)
        assert len(out) == 1 and out[0].kind == "conflict"


# ── Verdicts: crowd extreme ──────────────────────────────────────────────────

class TestCrowdExtreme:
    def _acct(self):
        return account_with()  # flat operator account

    def _public(self, symbol, fr, oi_chg):
        lens = BybitLens()
        return lens.poll_public({symbol: {
            "fundingRate": str(fr), "oi_24h_change_pct": oi_chg,
            "openInterest": "1000"}}, NOW)

    def test_short_squeeze_risk(self):
        pub = self._public("BTCUSDT", 0.001, 20.0)
        out = BybitLens().hedge_verdict(
            cfg(), account=self._acct(), sodex_book=book(side="short"),
            public=pub, now_ts=NOW)
        assert len(out) == 1
        v = out[0]
        assert v.kind == "crowd_extreme" and v.event == EVENT_CROWD_EXTREME
        assert v.payload["funding_rate"] == 0.001
        assert v.payload["oi_change_pct"] == 20.0

    def test_long_crowd_against(self):
        pub = self._public("BTCUSDT", -0.001, 20.0)
        out = BybitLens().hedge_verdict(
            cfg(), account=self._acct(), sodex_book=book(side="long"),
            public=pub, now_ts=NOW)
        assert [v.kind for v in out] == ["crowd_extreme"]

    def test_funding_exactly_threshold_not_extreme(self):
        pub = self._public("BTCUSDT", 0.0005, 20.0)  # exactly ±0.05%
        out = BybitLens().hedge_verdict(
            cfg(), account=self._acct(), sodex_book=book(side="short"),
            public=pub, now_ts=NOW)
        assert out == []

    def test_oi_exactly_threshold_not_extreme(self):
        pub = self._public("BTCUSDT", 0.001, 15.0)  # exactly +15%
        out = BybitLens().hedge_verdict(
            cfg(), account=self._acct(), sodex_book=book(side="short"),
            public=pub, now_ts=NOW)
        assert out == []

    def test_funding_aligned_with_side_no_verdict(self):
        pub = self._public("BTCUSDT", 0.001, 20.0)  # crowd long, we are long
        out = BybitLens().hedge_verdict(
            cfg(), account=self._acct(), sodex_book=book(side="long"),
            public=pub, now_ts=NOW)
        assert out == []

    def test_missing_oi_evidence_abstains(self):
        lens = BybitLens()
        pub = lens.poll_public({"BTCUSDT": {"fundingRate": "0.001",
                                            "openInterest": "100"}}, NOW)
        out = lens.hedge_verdict(cfg(), account=self._acct(),
                                 sodex_book=book(side="short"),
                                 public=pub, now_ts=NOW)
        assert out == []

    def test_conflict_and_crowd_both_fire(self):
        acct = account_with(pos_row(side="Buy"))
        pub = self._public("BTCUSDT", 0.001, 20.0)
        out = BybitLens().hedge_verdict(
            cfg(), account=acct, sodex_book=book(side="short"),
            public=pub, now_ts=NOW)
        assert {v.kind for v in out} == {"conflict", "crowd_extreme"}


# ── Kill switch ──────────────────────────────────────────────────────────────

class TestKillSwitch:
    def test_disabled_no_verdicts(self):
        acct = account_with(pos_row(side="Buy"))
        lens = BybitLens()
        pub = lens.poll_public({"BTCUSDT": {"fundingRate": "0.001",
                                            "oi_24h_change_pct": 20.0}}, NOW)
        out = lens.hedge_verdict(cfg(bybit_lens_enabled=False),
                                 account=acct, sodex_book=book(side="short"),
                                 public=pub, now_ts=NOW)
        assert out == []

    def test_disabled_no_basis_note(self):
        lens = BybitLens()
        note = lens.price_discovery_note(cfg(bybit_lens_enabled=False),
                                         symbol="BTC-USD", bybit_mark=101000.0,
                                         sodex_mark=100000.0, now_ts=NOW)
        assert note is None

    def test_default_off_cfg_missing_attrs(self):
        bare = SimpleNamespace()  # no attrs at all → all defaults
        acct = account_with(pos_row(side="Buy"))
        out = BybitLens().hedge_verdict(bare, account=acct,
                                        sodex_book=book(side="short"),
                                        public=PublicPlane(ts=NOW),
                                        now_ts=NOW)
        assert out == []  # bybit_lens_enabled default False


# ── Basis notes ──────────────────────────────────────────────────────────────

class TestBasisNote:
    def test_bybit_rich(self):
        lens = BybitLens()
        note = lens.price_discovery_note(
            cfg(), symbol="BTC-USD", bybit_mark=100200.0,
            sodex_mark=100000.0, now_ts=NOW)  # ~0.2% > 0.15%
        assert note is not None
        assert note.direction == "bybit_rich"
        assert note.magnitude_bps == pytest.approx(
            abs(100200.0 - 100000.0) / ((100200.0 + 100000.0) / 2) * 1e4)
        assert note.event == EVENT_BASIS_NOTE
        assert note.symbol == "BTC-USD"

    def test_sodex_rich(self):
        lens = BybitLens()
        note = lens.price_discovery_note(
            cfg(), symbol="BTC-USD", bybit_mark=99800.0,
            sodex_mark=100000.0, now_ts=NOW)
        assert note is not None and note.direction == "sodex_rich"

    def test_exactly_threshold_no_note(self):
        # construct a diff whose float value lands EXACTLY on the threshold
        bb, ss = 100075.0, 99925.0
        exact = abs(bb - ss) / ((bb + ss) / 2)
        lens = BybitLens()
        note = lens.price_discovery_note(
            cfg(bybit_lens_basis_note_pct=exact), symbol="BTC-USD",
            bybit_mark=bb, sodex_mark=ss, now_ts=NOW)
        assert note is None  # strict > : diff == threshold → silence

    def test_one_ulp_above_threshold_notes(self):
        bb, ss = 100075.0, 99925.0
        exact = abs(bb - ss) / ((bb + ss) / 2)
        import math
        lens = BybitLens()
        note = lens.price_discovery_note(
            cfg(bybit_lens_basis_note_pct=math.nextafter(exact, 0.0)),
            symbol="BTC-USD", bybit_mark=bb, sodex_mark=ss, now_ts=NOW)
        assert note is not None

    def test_below_threshold_no_note(self):
        lens = BybitLens()
        note = lens.price_discovery_note(
            cfg(), symbol="BTC-USD", bybit_mark=100100.0,
            sodex_mark=100000.0, now_ts=NOW)  # ~0.1%
        assert note is None

    def test_zero_or_negative_marks_none(self):
        lens = BybitLens()
        for bb, ss in [(0.0, 100.0), (100.0, 0.0), (-5.0, 100.0)]:
            assert lens.price_discovery_note(
                cfg(), symbol="BTC-USD", bybit_mark=bb, sodex_mark=ss,
                now_ts=NOW) is None

    def test_bad_marks_none(self):
        lens = BybitLens()
        assert lens.price_discovery_note(
            cfg(), symbol="BTC-USD", bybit_mark="garbage",
            sodex_mark=100.0, now_ts=NOW) is None

    def test_boot_abstain_empty_window(self):
        lens = BybitLens()
        assert lens.basis_window("BTC-USD", NOW) == ()

    def test_observations_recorded_and_windowed(self):
        lens = BybitLens()
        lens.price_discovery_note(cfg(), symbol="BTC-USD",
                                  bybit_mark=100100.0, sodex_mark=100000.0,
                                  now_ts=NOW - BASIS_WINDOW_S - 10)  # stale
        lens.price_discovery_note(cfg(), symbol="BTC-USD",
                                  bybit_mark=100100.0, sodex_mark=100000.0,
                                  now_ts=NOW)
        w = lens.basis_window("BTC-USD", NOW)
        assert len(w) == 1  # the >1h observation pruned
        assert w[0][1] == pytest.approx((100100.0 - 100000.0)
                                        / ((100100.0 + 100000.0) / 2))


# ── Poll-error telemetry helper ──────────────────────────────────────────────

class TestPollErrorEvent:
    def test_shape(self):
        row = poll_error_event("account", "boom", NOW)
        assert row["event"] == EVENT_POLL_ERROR
        assert row["where"] == "account" and row["ts"] == NOW
        assert "boom" in row["error"]


# ── READ-ONLY structural guarantee (meta-test) ───────────────────────────────

class TestReadOnlyStructural:
    FORBIDDEN = ("executor", "order", "place", "cancel")

    def test_no_order_capable_parameters_in_public_api(self):
        import intelligence.bybit_lens as mod

        def check_callable(fn, qualname):
            try:
                sig = inspect.signature(fn)
            except (TypeError, ValueError):
                return
            for pname in sig.parameters:
                low = pname.lower()
                for bad in self.FORBIDDEN:
                    assert bad not in low, (
                        f"READ-ONLY VIOLATION: {qualname} has parameter "
                        f"{pname!r} containing {bad!r}")

        for name in dir(mod):
            if name.startswith("_"):
                continue
            obj = getattr(mod, name)
            if inspect.isfunction(obj):
                if obj.__module__ != mod.__name__:
                    continue  # imports (e.g. dataclass) are not our API
                check_callable(obj, name)
            elif inspect.isclass(obj) and obj.__module__ == mod.__name__:
                for meth_name in dir(obj):
                    if meth_name.startswith("__"):
                        continue
                    meth = getattr(obj, meth_name)
                    if callable(meth):
                        check_callable(meth, f"{name}.{meth_name}")

    def test_lens_constructs_with_no_dependencies(self):
        lens = BybitLens()
        assert lens.last_account is None and lens.last_public is None

    def test_module_has_no_network_imports(self):
        import intelligence.bybit_lens as mod
        src = inspect.getsource(mod)
        for banned in ("httpx", "requests", "aiohttp", "websocket",
                       "socket", "import main", "bybit_client"):
            assert banned not in src, f"module references {banned!r}"
