"""Pins for tools/bq12_sim_bias.py — pure join + bias math, no I/O."""

from tools.bq12_sim_bias import (
    ADMITTED_BIAS_BP,
    MIN_N,
    bias_stats,
    join_refusals_to_fills,
    realized_pnl_pct,
    transfer_verdict,
)


def _ref(ts, sym="BTC-USD", direction="long", gate="quant_filter"):
    return {"id": f"{ts}_{sym}_{direction}_{gate}", "ts": ts, "symbol": sym,
            "direction": direction, "gate": gate, "entry": 100.0,
            "hyp_stop": 99.0}


def _fill(open_s, sym="BTC-USD", side="long", net=1.0, notional=100.0):
    return {"trade_id": f"{sym}_{int(open_s*1000)}", "symbol": sym,
            "side": side, "timestamp_open_ms": int(open_s * 1000),
            "net_pnl": net, "notional_usd": notional}


class TestJoin:
    def test_nearest_later_fill_within_24h(self):
        rows = join_refusals_to_fills([_ref(1000.0)],
                                      [_fill(2000.0), _fill(5000.0)])
        assert len(rows) == 1
        assert rows[0]["fill"]["timestamp_open_ms"] == 2_000_000

    def test_fill_before_refusal_never_matches(self):
        assert join_refusals_to_fills([_ref(5000.0)], [_fill(2000.0)]) == []

    def test_fill_beyond_24h_never_matches(self):
        assert join_refusals_to_fills([_ref(1000.0)],
                                      [_fill(1000.0 + 86401)]) == []

    def test_direction_must_match(self):
        rows = join_refusals_to_fills([_ref(1000.0, direction="short")],
                                      [_fill(2000.0, side="long")])
        assert rows == []

    def test_symbol_must_match(self):
        rows = join_refusals_to_fills([_ref(1000.0, sym="ETH-USD")],
                                      [_fill(2000.0, sym="BTC-USD")])
        assert rows == []

    def test_every_refusal_can_join(self):
        refs = [_ref(1000.0), _ref(1500.0)]
        rows = join_refusals_to_fills(refs, [_fill(2000.0)])
        assert len(rows) == 2  # refusal-level estimand; fill dedup is arm B's

    def test_bad_rows_skipped(self):
        rows = join_refusals_to_fills([{"ts": "x"}, {}], [{"symbol": None}, {}])
        assert rows == []


class TestRealizedPct:
    def test_fee_inclusive_pct(self):
        assert realized_pnl_pct(_fill(1.0, net=-2.5, notional=50.0)) == -5.0

    def test_zero_notional_abstains(self):
        assert realized_pnl_pct(_fill(1.0, notional=0.0)) is None

    def test_missing_pnl_abstains(self):
        f = _fill(1.0)
        del f["net_pnl"]
        assert realized_pnl_pct(f) is None


class TestVerdict:
    def test_thin_below_bar(self):
        assert transfer_verdict(MIN_N - 1, 0.0) == "thin"

    def test_transferable_within_2x_benchmark(self):
        assert transfer_verdict(MIN_N, ADMITTED_BIAS_BP) == "transferable"
        assert transfer_verdict(MIN_N, 2 * ADMITTED_BIAS_BP) == "transferable"

    def test_gap_named_with_direction(self):
        assert transfer_verdict(MIN_N, 5.0) == "gap_optimistic"
        assert transfer_verdict(MIN_N, -5.0) == "gap_pessimistic"

    def test_bias_stats_shape(self):
        s = bias_stats([1.0, 2.0, 3.0, 4.0])
        assert s["n"] == 4 and s["median_bp"] == 2.5
        assert bias_stats([]) == {"n": 0}
