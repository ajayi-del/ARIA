"""Pure-math pins for intelligence/pair_spread.py — the pair_meanrev
shadow gate (queue #66). No I/O beyond tmp_path ledger files."""
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.pair_spread import (  # noqa: E402
    COST_BPS_RT, append_ledger, close_record, cost_pts, entry_verdict,
    exit_verdict, open_record, pair_already_open, read_open_positions,
    slot_available, spread_now, spread_pnl_pts, z_live)

_PAIR = {"sym_a": "SOL-USD", "sym_b": "BTC-USD", "plane": "crypto",
         "hedge_ratio": 1.5, "intercept": 0.25, "spread_std": 0.02,
         "half_life_days": 2.0, "adf_p": 0.01, "status": "candidate"}


class TestSpreadMath:
    def test_spread_and_z(self):
        pa, pb = 100.0, 50.0
        s = spread_now(pa, pb, 0.25, 1.5)
        assert abs(s - (math.log(100) - 0.25 - 1.5 * math.log(50))) < 1e-12
        assert z_live(0.04, 0.02) == 2.0

    def test_degenerate_inputs_none(self):
        assert spread_now(0, 50.0, 0.25, 1.5) is None
        assert spread_now(100.0, -1.0, 0.25, 1.5) is None
        assert spread_now(None, 50.0, 0.25, 1.5) is None
        assert z_live(0.04, 0.0) is None
        assert z_live(0.04, None) is None

    def test_pnl_sign_both_directions(self):
        # short spread profits when spread falls
        assert spread_pnl_pts("short_spread", 0.05, 0.02) == 0.05 - 0.02
        assert abs(spread_pnl_pts("long_spread", -0.05, -0.02) - 0.03) < 1e-12


class TestEntryVerdict:
    def test_thresholds_both_sides(self):
        assert entry_verdict(2.0, "candidate", True) == "short_spread"
        assert entry_verdict(-2.0, "candidate", True) == "long_spread"
        assert entry_verdict(1.99, "candidate", True) == "none"
        assert entry_verdict(-1.99, "candidate", True) == "none"

    def test_dead_pair_and_dark_plane_abstain(self):
        assert entry_verdict(3.0, "dead", True) == "none"
        assert entry_verdict(3.0, "candidate", False) == "none"
        assert entry_verdict(None, "candidate", True) == "none"


class TestExitVerdict:
    def test_mean_reverted(self):
        assert exit_verdict("short_spread", 0.4, 1.0, 2.0,
                            "candidate") == "mean_reverted"
        assert exit_verdict("long_spread", -0.5, 1.0, 2.0,
                            "candidate") == "mean_reverted"

    def test_z_stop_adverse_only(self):
        # short spread entered high; z keeps rising = adverse
        assert exit_verdict("short_spread", 3.5, 1.0, 2.0,
                            "candidate") == "z_stop"
        # long spread entered low; z keeps falling = adverse
        assert exit_verdict("long_spread", -3.5, 1.0, 2.0,
                            "candidate") == "z_stop"
        # z crossing the FAVORABLE side is not a stop: for short_spread a
        # deeply negative z is way past target — not adverse, still "hold"
        # (|z| > z_exit so no mean_reverted; z < +z_stop so no z_stop).
        assert exit_verdict("short_spread", -3.9, 1.0, 2.0,
                            "candidate") == "hold"
        assert exit_verdict("short_spread", 2.9, 1.0, 2.0,
                            "candidate") == "hold"

    def test_time_stop_and_kill_outranks(self):
        assert exit_verdict("short_spread", 2.2, 100.0, 2.0,
                            "candidate") == "time_stop"
        # kill outranks even a would-be mean_reverted
        assert exit_verdict("short_spread", 0.1, 1.0, 2.0,
                            "dead") == "kill_cointegration"

    def test_hold(self):
        assert exit_verdict("short_spread", 1.2, 1.0, 2.0,
                            "candidate") == "hold"
        assert exit_verdict("short_spread", None, 1.0, 2.0,
                            "candidate") == "hold"


class TestCostAndSlots:
    def test_cost_scales_with_hedge(self):
        c = cost_pts(1.5)
        assert abs(c - (COST_BPS_RT / 1e4) * 2.5) < 1e-12
        assert cost_pts(0.0) == COST_BPS_RT / 1e4

    def test_slot_budget_and_dedup(self):
        opens = [{"sym_a": "A", "sym_b": "B"}, {"sym_a": "C", "sym_b": "D"}]
        assert slot_available(opens, 3)
        assert not slot_available(opens, 2)
        assert pair_already_open(opens, "A", "B")
        assert not pair_already_open(opens, "B", "A")
        assert not pair_already_open(opens, "X", "Y")


class TestLedger:
    def _open(self, now=1000.0):
        s = spread_now(100.0, 50.0, _PAIR["intercept"],
                       _PAIR["hedge_ratio"])
        return open_record(_PAIR, "short_spread", s, 2.5, 100.0, 50.0,
                           now=now)

    def test_open_close_roundtrip_net(self, tmp_path):
        path = str(tmp_path / "pair_shadow.jsonl")
        rec = self._open()
        append_ledger(path, rec)
        exit_s = rec["entry_spread"] - 0.03   # spread fell 0.03 = profit
        close = close_record(rec, "mean_reverted", exit_s, 0.3, now=1060.0)
        append_ledger(path, close)
        assert close["pnl_pts"] == 0.03
        assert close["net_pts"] < close["pnl_pts"]     # cost deducted
        assert close["age_hours"] == round(60.0 / 3600.0, 2)
        assert read_open_positions(path) == []         # closed

    def test_open_positions_survive_and_dedup(self, tmp_path):
        path = str(tmp_path / "pair_shadow.jsonl")
        rec = self._open()
        append_ledger(path, rec)
        opens = read_open_positions(path)
        assert len(opens) == 1 and opens[0]["id"] == rec["id"]

    def test_one_bad_line(self, tmp_path):
        path = str(tmp_path / "pair_shadow.jsonl")
        rec = self._open()
        append_ledger(path, rec)
        with open(path, "a") as f:
            f.write("{garbage not json\n")
        opens = read_open_positions(path)
        assert len(opens) == 1

    def test_missing_file_empty(self, tmp_path):
        assert read_open_positions(str(tmp_path / "nope.jsonl")) == []

    def test_close_row_json_shape(self):
        rec = self._open()
        close = close_record(rec, "z_stop", rec["entry_spread"] + 0.04, 3.6,
                             now=1000.0 + 7200)
        json.dumps(close)   # serializable
        assert close["event"] == "pair_shadow_close"
        assert close["reason"] == "z_stop"
        assert close["pnl_pts"] < 0                      # adverse = loss


class TestWiring:
    def test_loop_registered_and_knobs_present(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "main.py")).read()
        assert "_supervise(_pair_shadow_loop" in src
        assert "pair_shadow_opened" in src and "pair_shadow_closed" in src
        cfg = open(os.path.join(root, "core", "config.py")).read()
        for knob in ("pair_meanrev_shadow_enabled", "pair_z_entry",
                     "pair_z_exit", "pair_z_stop", "pair_max_open",
                     "pair_cost_bps_rt"):
            assert knob in cfg
        screen = open(os.path.join(root, "tools", "pair_screen.py")).read()
        assert '"intercept"' in screen and '"spread_std"' in screen

