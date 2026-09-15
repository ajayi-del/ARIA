"""Tests for tools/fee_ratio_census.py — the retrospective per-class
fee-drag census. Pure functions only; fixture dicts, no network."""
import importlib.util
import json
import os

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "tools", "fee_ratio_census.py")
_spec = importlib.util.spec_from_file_location("fee_ratio_census", _PATH)
frc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(frc)

TAKER = 0.00038   # tier-0 taker with 5% SOSO discount
MAKER = 0.000114  # tier-0 maker with 5% SOSO discount


def _close(symbol="ETH-USD", personality="FLOW", venue="sodex",
           pnl_usd=1.0, entry_id="e1", closed_at_ms=1755400000000,
           position_size=0.05, entry_price=4000.0,
           order_type_used="market", **kw):
    r = {"symbol": symbol, "personality": personality, "venue": venue,
         "pnl_usd": pnl_usd, "entry_id": entry_id,
         "closed_at_ms": closed_at_ms, "position_size": position_size,
         "entry_price": entry_price, "order_type_used": order_type_used,
         "outcome": "win" if (pnl_usd or 0) > 0 else "loss"}
    r.update(kw)
    return r


# ── dedup ────────────────────────────────────────────────────────────────────

def test_dedup_collapses_day_file_overlap():
    a = _close(entry_id="e1", closed_at_ms=1000)
    b = _close(entry_id="e1", closed_at_ms=1000)   # overlap dupe
    c = _close(entry_id="e1", closed_at_ms=2000)   # same trade, second close
    d = _close(entry_id="e2", closed_at_ms=1000)
    out, dupes = frc.dedup_closes([a, b, c, d])
    assert dupes == 1 and len(out) == 3


def test_dedup_none_key_collapses_safely():
    # Records missing entry_id share one (None, ts) key — first wins, the
    # rest count as dupes rather than inflating the census.
    a = _close(entry_id=None, closed_at_ms=1000)
    b = _close(entry_id=None, closed_at_ms=1000)
    out, dupes = frc.dedup_closes([a, b])
    assert len(out) == 1 and dupes == 1


# ── cost math ────────────────────────────────────────────────────────────────

def test_round_trip_rate_taker_entry():
    rt = frc.round_trip_rate("sodex", "ETH-USD", "market", TAKER, MAKER)
    assert rt == pytest.approx(2 * TAKER)


def test_round_trip_rate_maker_entry_exit_taker():
    rt = frc.round_trip_rate("sodex", "ETH-USD", "gtx", TAKER, MAKER)
    assert rt == pytest.approx(MAKER + TAKER)


def test_round_trip_rate_aster_crypto_and_tradfi():
    rt_c = frc.round_trip_rate("aster", "UNI-USD", "market", TAKER, MAKER)
    assert rt_c == pytest.approx(2 * 0.00040)
    rt_m = frc.round_trip_rate("aster", "UNI-USD", "gtx", TAKER, MAKER)
    assert rt_m == pytest.approx(0.00040)  # maker entry free on Aster
    rt_t = frc.round_trip_rate("aster", "SPCX-USD", "market", TAKER, MAKER,
                               aster_tradfi_symbols={"SPCX-USD"})
    assert rt_t == pytest.approx(2 * 0.00009)


def test_close_cost_includes_spread_leg_when_present():
    r = _close(slippage_expected_usd=0.05)
    cost, applied = frc.close_cost_usd(r, 2 * TAKER)
    assert applied is True
    assert cost == pytest.approx(200.0 * 2 * TAKER + 0.05)  # $200 notional


def test_close_cost_skips_spread_leg_when_absent():
    r = _close(slippage_expected_usd=0.0)
    cost, applied = frc.close_cost_usd(r, 2 * TAKER)
    assert applied is False
    assert cost == pytest.approx(200.0 * 2 * TAKER)


# ── census cells: ratio, n-thin, verdicts ────────────────────────────────────

def test_cost_ratio_math_realized_gross_win():
    # 10 wins of $1.00 gross at $200 notional each, taker-taker.
    closes = [_close(entry_id=f"e{i}", closed_at_ms=1000 + i)
              for i in range(10)]
    cells = frc.census_cells(closes, TAKER, MAKER)
    c = cells["ETH-USD|FLOW|sodex"]
    expected_cost = 200.0 * 2 * TAKER          # 0.152
    assert c["n"] == 10 and c["n_wins"] == 10
    assert c["win_rate"] == pytest.approx(1.0)
    assert c["avg_gross_win_usd"] == pytest.approx(1.0)
    assert c["avg_round_trip_cost_usd"] == pytest.approx(expected_cost)
    assert c["cost_ratio"] == pytest.approx(round(expected_cost / 1.0, 4))
    assert c["verdict"] == "viable"            # 0.152 < 0.30


def test_retire_candidate_over_threshold():
    # avg gross win $0.04 vs cost $0.152 -> ratio 3.8 -> retire_candidate.
    closes = [_close(entry_id=f"e{i}", closed_at_ms=1000 + i, pnl_usd=0.04)
              for i in range(10)]
    cells = frc.census_cells(closes, TAKER, MAKER)
    c = cells["ETH-USD|FLOW|sodex"]
    assert c["cost_ratio"] == pytest.approx(3.8)
    assert c["verdict"] == "retire_candidate"


def test_n_thin_cells_never_verdicted():
    closes = [_close(entry_id=f"e{i}", closed_at_ms=1000 + i, pnl_usd=0.01)
              for i in range(9)]   # ratio would be huge, but n=9
    cells = frc.census_cells(closes, TAKER, MAKER)
    c = cells["ETH-USD|FLOW|sodex"]
    assert c["n"] == 9 and c["verdict"] == "n_thin"
    assert c["cost_ratio"] is not None   # ratio still reported, never verdicted


def test_advisory_threshold_boundary():
    assert frc.verdict_for(10, 0.30) == "viable"        # > not >=
    assert frc.verdict_for(10, 0.3001) == "retire_candidate"
    assert frc.verdict_for(9, 0.99) == "n_thin"
    assert frc.verdict_for(10, None) == "no_gross_wins"


def test_cell_key_includes_symbol_personality_venue():
    a = _close(personality="FLOW", venue="sodex")
    b = _close(personality="FLOW", venue="aster")
    c = _close(personality=None, venue=None)
    assert frc.cell_key(a) == "ETH-USD|FLOW|sodex"
    assert frc.cell_key(b) == "ETH-USD|FLOW|aster"
    assert frc.cell_key(c) == "ETH-USD|unknown|sodex"  # pre-venue era = sodex


def test_gross_win_uses_winners_only_not_nominal_tp():
    # 5 wins $2.00 + 5 losses -$0.50: avg gross win must be $2.00, and a
    # tp1_price 10x further away must not enter the math anywhere.
    closes = []
    for i in range(5):
        closes.append(_close(entry_id=f"w{i}", closed_at_ms=1000 + i,
                             pnl_usd=2.0, tp1_price=4400.0))
    for i in range(5):
        closes.append(_close(entry_id=f"l{i}", closed_at_ms=2000 + i,
                             pnl_usd=-0.5, tp1_price=4400.0))
    cells = frc.census_cells(closes, TAKER, MAKER)
    c = cells["ETH-USD|FLOW|sodex"]
    assert c["n_wins"] == 5
    assert c["win_rate"] == pytest.approx(0.5)  # WR explicit beside the ratio
    assert c["avg_gross_win_usd"] == pytest.approx(2.0)
    assert c["cost_ratio"] == pytest.approx(round(200.0 * 2 * TAKER / 2.0, 4))


def test_no_gross_wins_cell():
    closes = [_close(entry_id=f"e{i}", closed_at_ms=1000 + i, pnl_usd=-0.1)
              for i in range(10)]
    cells = frc.census_cells(closes, TAKER, MAKER)
    c = cells["ETH-USD|FLOW|sodex"]
    assert c["avg_gross_win_usd"] is None
    assert c["cost_ratio"] is None
    assert c["verdict"] == "no_gross_wins"


# ── one-bad-line JSONL ───────────────────────────────────────────────────────

def test_read_jsonl_tolerates_bad_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\nNOT JSON\n{"b": 2}\n\n{"broken": \n[1,2]\n')
    out = frc.read_jsonl(str(p))
    assert out == [{"a": 1}, {"b": 2}]


def test_read_jsonl_missing_file_returns_empty():
    assert frc.read_jsonl("/nonexistent/shadow_scored.jsonl") == []


# ── shadow census ────────────────────────────────────────────────────────────

def _shadow(gate="dispersion", pnl_24h=0.5, stopped=False, sid=None, ts=1.0):
    return {"id": sid or f"{int(ts)}_ETH-USD_long_{gate}",
            "ts": ts, "symbol": "ETH-USD", "gate": gate,
            "direction": "long", "stopped": stopped,
            "scored": {"1h": pnl_24h / 2, "4h": pnl_24h / 1.5,
                       "24h": pnl_24h},
            "pnl_24h": pnl_24h}


def test_shadow_census_math():
    rt_bps = 7.6
    recs = [_shadow(pnl_24h=0.5, ts=1000 + i) for i in range(10)]
    out = frc.shadow_census(recs, rt_bps)
    g = out["dispersion"]
    # avg win 0.5% = 50bps; cost_ratio = 7.6 / 50
    assert g["n"] == 10 and g["avg_win_bps"] == pytest.approx(50.0)
    assert g["cost_ratio"] == pytest.approx(round(7.6 / 50.0, 4))
    assert g["net_of_fees_profitable_share"] == pytest.approx(1.0)
    assert g["verdict"] == "viable"
    assert "_all" in out and out["_all"]["n"] == 10


def test_shadow_census_stopped_records_not_wins():
    rt_bps = 7.6
    recs = [_shadow(pnl_24h=0.5, stopped=True, ts=1000 + i) for i in range(10)]
    out = frc.shadow_census(recs, rt_bps)
    g = out["dispersion"]
    assert g["n_wins"] == 0 and g["cost_ratio"] is None
    assert g["verdict"] == "no_gross_wins"
    # a stopped shadow realized the stop loss — it can never read as
    # net-of-fees profitable even when its 24h mark is above the RT cost
    assert g["net_of_fees_profitable_share"] == 0.0


def test_shadow_census_stopped_positive_marks_do_not_inflate_net_share():
    # 5 clean wins above cost + 5 stopped records whose 24h mark is also
    # above cost: net share must count only the clean cohort (5/10).
    recs = ([_shadow(pnl_24h=0.5, stopped=False, ts=1000 + i) for i in range(5)]
            + [_shadow(pnl_24h=0.5, stopped=True, ts=2000 + i) for i in range(5)])
    out = frc.shadow_census(recs, 7.6)
    g = out["dispersion"]
    assert g["n_wins"] == 5
    assert g["net_of_fees_profitable_share"] == pytest.approx(0.5)


def test_shadow_census_n_thin_gate():
    recs = [_shadow(pnl_24h=0.01, ts=1000 + i) for i in range(3)]
    out = frc.shadow_census(recs, 7.6)
    assert out["dispersion"]["verdict"] == "n_thin"
    assert out["_all"]["verdict"] == "n_thin"


def test_shadow_pnl_pct_falls_back_to_scored_dict():
    rec = {"scored": {"24h": 0.42}}
    assert frc.shadow_pnl_pct(rec) == pytest.approx(0.42)
    assert frc.shadow_pnl_pct({"scored": {"1h": 0.1}}) is None
    assert frc.shadow_pnl_pct({}) is None


# ── atomic write ─────────────────────────────────────────────────────────────

def test_atomic_write_json(tmp_path):
    p = str(tmp_path / "out.json")
    frc.atomic_write_json(p, {"a": 1})
    assert json.load(open(p)) == {"a": 1}
    assert not os.path.exists(p + ".tmp")


def test_is_realized_close():
    assert frc.is_realized_close(_close()) is True
    assert frc.is_realized_close(_close(closed_at_ms=None)) is False
    assert frc.is_realized_close(_close(pnl_usd=None, pnl_net_usd=None)) is False
