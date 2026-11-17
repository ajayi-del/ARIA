"""Pins: stop arbiter (WS-B Build 1, 2026-11-17).

The wound (CEO audit S4-C16): SIX exit-stack writers each mutated
bracket["current_stop"] per tick and each fired its OWN venue resize —
a runner crossing three rungs in one tick paid three place+cancel
storms. The arbiter turns the tick into a ballot: inside
begin_stop_tick ... flush_stop_arbiter each writer VOTES its floor;
the flush picks the tightest vote (max-floor), dust-gates vs the last
issued floor, and fires AT MOST one venue resize + one current_stop
write. Kill PROP_STOP_ARBITER=0 → writers resize directly, legacy
bit-for-bit.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from prop_harness.brains import stop_arbiter as sa
from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor


class TestIsTighter:
    def test_long_higher_is_tighter(self):
        assert sa.is_tighter("buy", 95, 92) is True
        assert sa.is_tighter("buy", 92, 95) is False
        assert sa.is_tighter("buy", 95, 95) is False  # strict

    def test_short_mirrored(self):
        assert sa.is_tighter("sell", 88, 92) is True
        assert sa.is_tighter("sell", 92, 88) is False
        assert sa.is_tighter("sell", 88, 88) is False

    def test_junk_abstains_false(self):
        assert sa.is_tighter("buy", None, 92) is False
        assert sa.is_tighter("buy", 95, None) is False
        assert sa.is_tighter("buy", "junk", 92) is False
        assert sa.is_tighter("buy", float("nan"), 92) is False
        assert sa.is_tighter(None, 95, 92) is False
        assert sa.is_tighter("hold", 95, 92) is False

    def test_string_inputs_parse(self):
        assert sa.is_tighter("buy", "95.5", "92") is True


class TestPropose:
    def test_append_and_stringify(self):
        ballot = sa.propose([], stage="trail", target=96.5)
        assert ballot == [{"stage": "trail", "target": "96.5"}]

    def test_same_stage_revote_replaces(self):
        ballot = sa.propose([], stage="trail", target=95)
        ballot = sa.propose(ballot, stage="bank", target=94)
        ballot = sa.propose(ballot, stage="trail", target=97)
        assert ballot == [{"stage": "trail", "target": "97"},
                          {"stage": "bank", "target": "94"}]

    def test_unparseable_never_enters(self):
        ballot = sa.propose([], stage="trail", target=None)
        ballot = sa.propose(ballot, stage="trail", target="junk")
        ballot = sa.propose(ballot, stage="trail", target=float("nan"))
        assert ballot == []


class TestWinner:
    def test_empty_none(self):
        assert sa.winner([], "buy") is None
        assert sa.winner(None, "buy") is None

    def test_long_max_floor(self):
        ballot = [{"stage": "bank", "target": "95"},
                  {"stage": "trail", "target": "96"},
                  {"stage": "mfe_sharpen", "target": "94"}]
        assert sa.winner(ballot, "buy")["stage"] == "trail"

    def test_short_min_floor(self):
        ballot = [{"stage": "bank", "target": "95"},
                  {"stage": "trail", "target": "96"},
                  {"stage": "mfe_sharpen", "target": "94"}]
        assert sa.winner(ballot, "sell")["stage"] == "mfe_sharpen"

    def test_tie_keeps_first_stage(self):
        ballot = [{"stage": "bank", "target": "95"},
                  {"stage": "trail", "target": "95"}]
        assert sa.winner(ballot, "buy")["stage"] == "bank"


class TestPastDust:
    def test_first_issue_fires(self):
        assert sa.past_dust("buy", 95, None, 0.0005) is True
        assert sa.past_dust("buy", 95, "junk", 0.0005) is True

    def test_unparseable_target_never_fires(self):
        assert sa.past_dust("buy", "junk", 92, 0.0005) is False
        assert sa.past_dust("buy", None, None, 0.0005) is False

    def test_long_epsilon(self):
        # eps = 92 × 0.0005 = 0.046
        assert sa.past_dust("buy", 92.03, 92, 0.0005) is False
        assert sa.past_dust("buy", 92.046, 92, 0.0005) is False  # strict >
        assert sa.past_dust("buy", 92.05, 92, 0.0005) is True

    def test_short_mirrored(self):
        assert sa.past_dust("sell", 91.97, 92, 0.0005) is False
        assert sa.past_dust("sell", 91.95, 92, 0.0005) is True

    def test_zero_frac_legacy_sensitivity(self):
        assert sa.past_dust("buy", 92.0001, 92, 0.0) is True
        assert sa.past_dust("buy", 92, 92, 0.0) is False
        assert sa.past_dust("buy", 93, 92, -1.0) is True  # clamped to 0

    def test_unknown_side_false(self):
        assert sa.past_dust(None, 95, 92, 0.0005) is False


class _ArbClient:
    """Minimal venue: accepts the resize stop, reports no open orders."""

    account_id = "acct1"

    def __init__(self):
        self.placed = []

    def place_orders(self, body):
        self.placed.append(body)
        return [{"orderId": "sl%d" % len(self.placed)}]

    def get_open_orders(self, base=None):
        return []


def _exec(tmp_path: Path, client, **over) -> ProprExecutor:
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
    return ProprExecutor(cfg, client, budget, dd)


def _bracket(**over) -> dict:
    b = {
        "semantic_id": "s1",
        "asset": "BTC",
        "side": "buy",
        "entry_ref": "100",
        "stop_price": "92",
        "current_stop": "92",
        "risk_usd": 14.0,
        "opened_ts": 1_700_000_000.0,
        "status": "open",
    }
    b.update(over)
    return b


def _events(tmp_path: Path) -> list[dict]:
    out = []
    with open(tmp_path / "journal" / "events.jsonl") as fh:
        for line in fh:
            out.append(json.loads(line))
    return out


_POS = {"quantity": "1.5", "positionId": "p1"}


class TestExecutorBallot:
    def test_window_open_votes_hold_no_venue_call(self, tmp_path):
        client = _ArbClient()
        ex = _exec(tmp_path, client)
        b = _bracket()
        ex.begin_stop_tick(b)
        assert ex._stop_resize(b, Decimal("1.5"), "p1", "95",
                               stage="trail") == "held"
        assert client.placed == []
        assert b["current_stop"] == "92"  # writer never touched it

    def test_flush_commits_winner_once(self, tmp_path):
        client = _ArbClient()
        ex = _exec(tmp_path, client)
        b = _bracket()
        ex.begin_stop_tick(b)
        ex._stop_resize(b, Decimal("1.5"), "p1", "95", stage="bank")
        ex._stop_resize(b, Decimal("1.5"), "p1", "96", stage="trail")
        assert ex.flush_stop_arbiter(b, dict(_POS)) is True
        assert len(client.placed) == 1            # ONE venue resize
        order = client.placed[0]["orders"][0]
        assert order["triggerPrice"] == "96"      # max-floor won
        assert b["current_stop"] == "96"          # ONE write
        evs = _events(tmp_path)
        commit = [e for e in evs if e["type"] == "stop_arbiter_commit"][0]
        assert commit["winner_stage"] == "trail"
        assert commit["coalesced"] == ["bank"]
        assert commit["n"] == 2
        assert commit["previous_stop"] == "92"
        assert commit["stages"] == ["bank", "trail"]

    def test_flush_suppressed_not_tighter(self, tmp_path):
        client = _ArbClient()
        ex = _exec(tmp_path, client)
        b = _bracket(current_stop="97")
        ex.begin_stop_tick(b)
        ex._stop_resize(b, Decimal("1.5"), "p1", "95", stage="bank")
        ex._stop_resize(b, Decimal("1.5"), "p1", "96", stage="trail")
        assert ex.flush_stop_arbiter(b, dict(_POS)) is False
        assert client.placed == []
        assert b["current_stop"] == "97"
        ev = [e for e in _events(tmp_path)
              if e["type"] == "stop_arbiter_suppressed"][0]
        assert ev["reason"] == "not_tighter"
        assert ev["winner_stage"] == "trail"

    def test_flush_suppressed_dust(self, tmp_path):
        client = _ArbClient()
        # dust_r=0.0 pins the LEGACY frac epsilon (the per-R eps_abs path
        # — default 0.02 since the 2026-11-17 fix pack — replaces frac
        # when set; covered by TestPastDustEpsAbs in test_fixpack_s5)
        ex = _exec(tmp_path, client, stop_arbiter_dust_r=0.0)
        b = _bracket()
        ex.begin_stop_tick(b)
        # eps = 92 × 0.0005 = 0.046 → sub-epsilon move suppressed
        ex._stop_resize(b, Decimal("1.5"), "p1", "92.03", stage="trail")
        assert ex.flush_stop_arbiter(b, dict(_POS)) is False
        assert client.placed == []
        ev = [e for e in _events(tmp_path)
              if e["type"] == "stop_arbiter_suppressed"][0]
        assert ev["reason"] == "dust"
        assert ev["dust_frac"] == pytest.approx(0.0005)
        # past-epsilon move commits
        ex.begin_stop_tick(b)
        ex._stop_resize(b, Decimal("1.5"), "p1", "92.05", stage="trail")
        assert ex.flush_stop_arbiter(b, dict(_POS)) is True
        assert len(client.placed) == 1

    def test_empty_ballot_flush_false(self, tmp_path):
        client = _ArbClient()
        ex = _exec(tmp_path, client)
        b = _bracket()
        ex.begin_stop_tick(b)
        assert ex.flush_stop_arbiter(b, dict(_POS)) is False
        assert client.placed == []

    def test_per_position_ballot_isolation(self, tmp_path):
        client = _ArbClient()
        ex = _exec(tmp_path, client)
        b1 = _bracket(semantic_id="s1")
        b2 = _bracket(semantic_id="s2")
        ex.begin_stop_tick(b1)
        ex._stop_resize(b1, Decimal("1.5"), "p1", "95", stage="bank")
        ex._stop_resize(b2, Decimal("1.5"), "p2", "96", stage="trail")
        assert ex.flush_stop_arbiter(b1, dict(_POS)) is True
        assert b1["current_stop"] == "95"
        # s2's ballot survived s1's flush
        assert ex.flush_stop_arbiter(
            b2, {"quantity": "1.5", "positionId": "p2"}) is True
        assert b2["current_stop"] == "96"
        assert len(client.placed) == 2

    def test_begin_drops_stale_ballot(self, tmp_path):
        client = _ArbClient()
        ex = _exec(tmp_path, client)
        b = _bracket()
        ex.begin_stop_tick(b)
        ex._stop_resize(b, Decimal("1.5"), "p1", "95", stage="trail")
        # crashed tick: a fresh begin must not carry the old vote
        ex.begin_stop_tick(b)
        assert ex.flush_stop_arbiter(b, dict(_POS)) is False
        assert client.placed == []

    def test_kill_off_legacy_direct_resize(self, tmp_path):
        client = _ArbClient()
        ex = _exec(tmp_path, client, stop_arbiter_enabled=False)
        b = _bracket()
        ex.begin_stop_tick(b)  # no-op under kill
        ok = ex._stop_resize(b, Decimal("1.5"), "p1", "95", stage="trail")
        assert ok is True               # direct resize, not "held"
        assert len(client.placed) == 1
        assert client.placed[0]["orders"][0]["triggerPrice"] == "95"
        # flush clears strays, never touches the venue
        assert ex.flush_stop_arbiter(b, dict(_POS)) is False
        assert len(client.placed) == 1

    def test_stamps_follow_winner(self, tmp_path):
        client = _ArbClient()
        ex = _exec(tmp_path, client)
        b = _bracket()
        ex.begin_stop_tick(b)
        ex._stop_resize(b, Decimal("1.5"), "p1", "99", stage="bank")
        ex._stop_resize(b, Decimal("1.5"), "p1", "101",
                        stage="mfe_sharpen")
        assert ex.flush_stop_arbiter(b, dict(_POS)) is True
        assert b["current_stop"] == "101"
        assert b["bank_protected"] is True    # any bank vote covered
        assert b["mfe_floor_armed"] is True   # floor-stage won
        assert b["be_armed"] is True          # 101 locks through 100

    def test_short_side_winner_is_lowest(self, tmp_path):
        client = _ArbClient()
        ex = _exec(tmp_path, client)
        b = _bracket(side="sell", stop_price="108", current_stop="108")
        ex.begin_stop_tick(b)
        ex._stop_resize(b, Decimal("1.5"), "p1", "105", stage="bank")
        ex._stop_resize(b, Decimal("1.5"), "p1", "103", stage="trail")
        assert ex.flush_stop_arbiter(b, dict(_POS)) is True
        assert b["current_stop"] == "103"     # tighter = lower for shorts
        commit = [e for e in _events(tmp_path)
                  if e["type"] == "stop_arbiter_commit"][0]
        assert commit["winner_stage"] == "trail"
