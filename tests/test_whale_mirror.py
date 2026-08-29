"""Whale-mirror pins (Deploy 5, 2026-08-29).

Diff classification (open/add/trim/close/flip), Aster Δpnl×Δprice
direction inference + churn abstain, consensus quorum, aged-bag filter,
SoDEX payload parser, campaign-dark flip, write-failure swallow.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.whale_mirror import WhaleMirror  # noqa: E402
from data.whale_feed import WhaleFeed, sodex_positions_from_payload  # noqa: E402

_probe_bracket = WhaleMirror.whale_probe_bracket

A1 = "0xaaaa"
A2 = "0xbbbb"


def _mirror(px_move=0.5, now=1_000_000.0):
    clock = {"t": now}
    m = WhaleMirror(lambda sym, win: px_move,
                    min_pnl_delta_usd=50.0, min_price_move_pct=0.05,
                    consensus_window_s=1800, time_fn=lambda: clock["t"])
    return m, clock


# ── SoDEX direct diffs ───────────────────────────────────────────────────────

def test_sodex_opened_and_closed():
    m, _ = _mirror()
    flows = m.diff_sodex(A1, {"ETH-USD": 2.0})
    assert len(flows) == 1
    f = flows[0]
    assert f["kind"] == "opened" and f["direction"] == "long" and f["quality"] == "direct"
    flows = m.diff_sodex(A1, {})
    assert flows[0]["kind"] == "closed" and flows[0]["direction"] == "long"


def test_sodex_negative_size_is_short():
    m, _ = _mirror()
    flows = m.diff_sodex(A1, {"ETH-USD": -2.0})
    assert flows[0]["direction"] == "short"
    assert flows[0]["size"] == 2.0


def test_sodex_added_trimmed_flipped():
    m, _ = _mirror()
    m.diff_sodex(A1, {"BTC-USD": 1.0})
    assert m.diff_sodex(A1, {"BTC-USD": 2.5})[0]["kind"] == "added"
    assert m.diff_sodex(A1, {"BTC-USD": 1.5})[0]["kind"] == "trimmed"
    f = m.diff_sodex(A1, {"BTC-USD": -0.5})[0]
    assert f["kind"] == "flipped" and f["direction"] == "short"


def test_sodex_static_position_is_silent():
    m, _ = _mirror()
    m.diff_sodex(A1, {"ETH-USD": 2.0})
    assert m.diff_sodex(A1, {"ETH-USD": 2.0}) == []   # aged bag: no flow


# ── Aster inferred diffs ─────────────────────────────────────────────────────

def test_aster_first_sighting_is_baseline_only():
    m, _ = _mirror()
    assert m.diff_aster_rank(A1, "BTCUSDT", pnl=5000.0, volume=1e6) is None


def test_aster_pnl_up_price_up_is_long():
    m, _ = _mirror(px_move=0.4)
    m.diff_aster_rank(A1, "BTCUSDT", pnl=5000.0, volume=1e6)
    ev = m.diff_aster_rank(A1, "BTCUSDT", pnl=5300.0, volume=1.1e6)
    assert ev["direction"] == "long" and ev["kind"] == "added"
    assert ev["quality"] == "inferred"


def test_aster_pnl_up_price_down_is_short():
    m, _ = _mirror(px_move=-0.4)
    m.diff_aster_rank(A1, "BTCUSDT", pnl=5000.0, volume=1e6)
    ev = m.diff_aster_rank(A1, "BTCUSDT", pnl=5300.0, volume=1.1e6)
    assert ev["direction"] == "short"


def test_aster_churn_abstains():
    m, _ = _mirror(px_move=0.4)
    m.diff_aster_rank(A1, "BTCUSDT", pnl=5000.0, volume=1e6)
    # volume up but |Δpnl| below the $50 noise floor → market-making, no flow
    assert m.diff_aster_rank(A1, "BTCUSDT", pnl=5020.0, volume=1.2e6) is None


def test_aster_flat_price_abstains():
    m, _ = _mirror(px_move=0.01)   # below min_price_move_pct
    m.diff_aster_rank(A1, "BTCUSDT", pnl=5000.0, volume=1e6)
    assert m.diff_aster_rank(A1, "BTCUSDT", pnl=5300.0, volume=1.1e6) is None


# ── Consensus + candidates ───────────────────────────────────────────────────

def test_consensus_requires_distinct_addresses():
    m, clock = _mirror()
    m.diff_sodex(A1, {"SOL-USD": 5.0})
    cons = m.consensus("SOL-USD", "long")
    assert cons["n_whales"] == 1
    m.diff_sodex(A2, {"SOL-USD": 3.0})
    cons = m.consensus("SOL-USD", "long")
    assert cons["n_whales"] == 2 and cons["freshness_s"] is not None
    # same whale ADDING again doesn't inflate breadth
    m.diff_sodex(A1, {"SOL-USD": 7.0})
    assert m.consensus("SOL-USD", "long")["n_whales"] == 2


def test_consensus_window_expires():
    m, clock = _mirror()
    m.diff_sodex(A1, {"SOL-USD": 5.0})
    clock["t"] += 1801
    assert m.consensus("SOL-USD", "long")["n_whales"] == 0


def test_candidates_only_opening_class_with_consensus():
    m, _ = _mirror()
    flows = m.diff_sodex(A1, {"SOL-USD": 5.0})
    cands = m.candidates(flows)
    assert len(cands) == 1 and cands[0]["n_whales"] == 1
    flows = m.diff_sodex(A1, {})           # closed — not a candidate
    assert m.candidates(flows) == []


def test_has_direct_flow_distinguishes_legs():
    m, _ = _mirror(px_move=0.4)
    # inferred-only flow (Aster) → no direct flow
    m.diff_aster_rank(A1, "BTC-USD", pnl=5000.0, volume=1e6)
    ev = m.diff_aster_rank(A1, "BTC-USD", pnl=5300.0, volume=1.1e6)
    assert ev is not None
    assert m.has_direct_flow("BTC-USD", "long") is False
    assert m.consensus("BTC-USD", "long")["n_whales"] == 1
    # direct flow (SoDEX) → direct flag
    m.diff_sodex(A2, {"ETH-USD": 1.0})
    assert m.has_direct_flow("ETH-USD", "long") is True
    assert m.has_direct_flow("ETH-USD", "short") is False


# ── Reversal flows (exit side, O'Hara PIN) ───────────────────────────────────

def test_reversal_closed_long_against_held_long():
    m, _ = _mirror()
    m.diff_sodex(A1, {"ETH-USD": 2.0})       # opened long
    m.diff_sodex(A1, {})                     # CLOSED long → thesis exit
    rev = m.reversal_flows("ETH-USD", "long")
    assert len(rev) == 1 and rev[0]["kind"] == "closed"
    assert m.reversal_flows("ETH-USD", "short") == []


def test_reversal_flip_new_side_semantics():
    m, _ = _mirror()
    m.diff_sodex(A1, {"ETH-USD": 2.0})
    m.diff_sodex(A1, {"ETH-USD": -1.0})      # FLIPPED, new side = short
    # held LONG: the flip to short is a reversal of our side
    assert len(m.reversal_flows("ETH-USD", "long")) == 1
    # held SHORT: the flip JOINED our side — not a reversal
    assert m.reversal_flows("ETH-USD", "short") == []


def test_reversal_excludes_trims_and_inferred():
    m, _ = _mirror(px_move=0.4)
    m.diff_sodex(A1, {"ETH-USD": 2.0})
    m.diff_sodex(A1, {"ETH-USD": 1.0})       # TRIMMED — profit-taking, not exit
    assert m.reversal_flows("ETH-USD", "long") == []
    # inferred-leg close never triggers (noise class)
    m.diff_aster_rank(A2, "BTC-USD", pnl=5000.0, volume=1e6)
    ev = m.diff_aster_rank(A2, "BTC-USD", pnl=5300.0, volume=0.9e6)
    assert ev is not None and ev["kind"] == "closed"
    assert m.reversal_flows("BTC-USD", "long") == []


def test_reversal_window_expires():
    m, clock = _mirror()
    m.diff_sodex(A1, {"ETH-USD": 2.0})
    m.diff_sodex(A1, {})
    clock["t"] += 1801
    assert m.reversal_flows("ETH-USD", "long") == []


# ── Probe bracket (Thorp/Vince: risk = notional × stop) ─────────────────────

def test_probe_bracket_long_levels():
    br = _probe_bracket(100.0, "long", margin_usd=30.0, leverage=50.0,
                        stop_pct=0.6, tp1_pct=0.8, tp2_pct=1.2,
                        step=0.001, min_qty=0.001)
    assert br["qty"] == 15.0                  # 30×50/100
    assert br["stop"] < 100.0 < br["tp1"] < br["tp2"]
    assert abs(br["stop"] - 99.4) < 1e-9
    assert abs(br["risk_usd"] - 15.0 * 100.0 * 0.006) < 1e-6   # $9.00
    assert abs(br["notional"] - 1500.0) < 1e-9


def test_probe_bracket_short_mirrored():
    br = _probe_bracket(100.0, "short", margin_usd=30.0, leverage=50.0,
                        stop_pct=0.6, tp1_pct=0.8, tp2_pct=1.2,
                        step=0.001, min_qty=0.001)
    assert br["tp2"] < br["tp1"] < 100.0 < br["stop"]
    assert abs(br["stop"] - 100.6) < 1e-9


def test_probe_bracket_step_floor_and_minimums():
    br = _probe_bracket(100.0, "long", margin_usd=30.0, leverage=50.0,
                        stop_pct=0.6, tp1_pct=0.8, tp2_pct=1.2,
                        step=0.3, min_qty=0.001)
    assert br["qty"] == 15.0 - (15.0 % 0.3)   # floored to step
    # below min_qty → None
    assert _probe_bracket(100.0, "long", margin_usd=0.001, leverage=1.0,
                          stop_pct=0.6, tp1_pct=0.8, tp2_pct=1.2,
                          step=0.001, min_qty=0.001) is None
    # degenerate inputs → None
    assert _probe_bracket(0.0, "long", margin_usd=30.0, leverage=50.0,
                          stop_pct=0.6, tp1_pct=0.8, tp2_pct=1.2,
                          step=0.001, min_qty=0.001) is None


# ── Feed parser + journal ────────────────────────────────────────────────────

def test_sodex_payload_parser_contract():
    payload = {"code": 0, "data": {"positions": [
        {"symbol": "ETH-USD", "size": "2.5", "avgEntryPrice": "2500"},
        {"symbol": "BTC-USD", "size": "-0.1", "avgEntryPrice": "80000"},
        {"symbol": "DUST-USD", "size": "0"},
        "garbage-row",
    ]}}
    assert sodex_positions_from_payload(payload) == {"ETH-USD": 2.5, "BTC-USD": -0.1}


def test_sodex_payload_parser_empty():
    assert sodex_positions_from_payload({"code": 0, "data": {"positions": []}}) == {}
    assert sodex_positions_from_payload({}) == {}


def test_journal_write_failure_swallowed(tmp_path):
    feed = WhaleFeed([{"address": A1, "venue": "sodex", "label": "t"}],
                     ["BTCUSDT"], log_dir=str(tmp_path / "nonexistent" / "deep"))
    feed._journal({"ts": 1.0})   # directory doesn't exist — must not raise


def test_journal_appends(tmp_path):
    feed = WhaleFeed([], [], log_dir=str(tmp_path))
    feed._journal({"ts": 1.0, "venue": "sodex"})
    feed._journal({"ts": 2.0, "venue": "aster"})
    lines = (tmp_path / "whale_snapshots.jsonl").read_text().strip().split("\n")
    assert len(lines) == 2
