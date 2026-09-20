"""P2 hedge manager pins (2026-09-19 Governor-approved ByBit hedge venue).

Behavioral pins for intelligence/hedge_manager.py (the pure brain) plus the
main.py splice surface that IS importable: the _BybitHedgeWrapper (driven
here with a mock client to pin exact client-call arguments), the
registry-aware reconcile, and the config defaults. The action-executor and
the 10s loop live inside main() closures (same guard-closure note as
tests/test_hedge_spine.py) — their contract is pinned at the brain/wrapper
level: action kinds, payloads, and the wrapper→client argument mapping.
"""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from core.config import Settings  # noqa: E402
from intelligence.hedge_manager import (  # noqa: E402
    BookCtx, HedgeKnobs, HedgeManager, LongCtx, ORDER_ACTION_KINDS,
    derive_hedge_leverage, rung_for_roe, short_limit_cap)
from risk.hedge_registry import HedgeRegistry  # noqa: E402

RUNGS = ((0.30, 0.03), (0.50, 0.06), (0.70, 0.10), (1.00, 0.15))
SYM = "BTC-USD"
BASE = 1000.0


def _long(symbol=SYM, entry=BASE, qty=1.0, lev=5.0, mark=BASE,
          opened_at_ms=1_000_000, age_s=100.0):
    return LongCtx(symbol=symbol, side="long", entry_price=entry, qty=qty,
                   leverage=lev, mark=mark, opened_at_ms=opened_at_ms,
                   age_s=age_s)


def _ctx(now=10_000.0, longs=None, hedge_mark=BASE, enabled=True, **kw):
    longs = _longs if (_longs := longs) is not None else [_long()]
    marks = {l.symbol: l.mark for l in longs}
    hedge_marks = ({l.symbol: hedge_mark for l in longs}
                   if hedge_mark else {})
    d = dict(now=now, longs=longs, marks=marks, hedge_marks=hedge_marks,
             enabled=enabled, hedge_free_equity=10_000.0)
    d.update(kw)
    return BookCtx(**d)


def _kinds(actions):
    return [a.kind for a in actions]


def _events(actions):
    return [a.data.get("name") for a in actions if a.kind == "event"]


def _one(actions, kind):
    hits = [a for a in actions if a.kind == kind]
    assert len(hits) == 1, f"expected one {kind}, got {_kinds(actions)}"
    return hits[0]


def _armed_manager(now=10_000.0, roe_mark=BASE * 1.0007, **mkw):
    """Manager with one armed profit-lock plan (ROE 0.35% at 5x on the
    default mark → rung 0.30 / ratio 0.03). The production $20 short
    floor would block the $30 fixture plan, so the fixture lifts it;
    short-limit behavior itself is pinned in TestShortLimit."""
    mkw.setdefault("short_floor_usd", 10_000.0)
    m = HedgeManager(HedgeKnobs(**mkw))
    acts = m.evaluate(_ctx(now=now, longs=[_long(mark=roe_mark)]))
    _one(acts, "place_limit_short")
    return m, acts


# ── Rung matrix ──────────────────────────────────────────────────────────────

class TestRungMatrix:
    def test_arm_on_rung_cross(self):
        m, acts = _armed_manager()
        assert "hedge_plan_armed" in _events(acts)
        place = _one(acts, "place_limit_short")
        assert place.data["qty"] == pytest.approx(0.03 * 1.0)
        assert place.data["price"] == pytest.approx(BASE * (1 + 0.0005))
        assert place.data["leverage"] == 15  # fixture free equity → lev_max
        upsert = _one(acts, "registry_upsert")
        assert upsert.data["state"] == "armed"
        assert upsert.data["side"] == "short"

    def test_below_first_rung_no_arm(self):
        m = HedgeManager()
        # ROE = 0.02% price × 5 = 0.10% < 0.30 rung
        acts = m.evaluate(_ctx(longs=[_long(mark=BASE * 1.0002)]))
        assert acts == []

    def test_up_ratchet_prefill(self):
        m, _ = _armed_manager()
        plan = m.plans()[0]
        assert plan.rung_roe == 0.30
        # ROE rises to 0.55% → rung 0.50 / ratio 0.06
        acts = m.evaluate(_ctx(longs=[_long(mark=BASE * 1.0011)]))
        assert "hedge_rung_ratchet" in _events(acts)
        assert plan.rung_roe == 0.50
        assert plan.hedge_ratio == 0.06
        assert plan.qty == pytest.approx(0.06 * 1.0)

    def test_no_down_ratchet(self):
        m, _ = _armed_manager()
        m.evaluate(_ctx(longs=[_long(mark=BASE * 1.0011)]))   # up to 0.50
        plan = m.plans()[0]
        # ROE falls back to the 0.30 rung — the ratchet must NOT follow down
        acts = m.evaluate(_ctx(longs=[_long(mark=BASE * 1.0007)]))
        assert plan.rung_roe == 0.50
        assert plan.hedge_ratio == 0.06
        assert "hedge_rung_ratchet" not in _events(acts)

    def test_highest_rung_wins(self):
        m = HedgeManager(HedgeKnobs(short_floor_usd=10_000.0))
        # ROE = 0.25% × 5 = 1.25% → top rung 1.00 / ratio 0.15
        acts = m.evaluate(_ctx(longs=[_long(mark=BASE * 1.0025)]))
        place = _one(acts, "place_limit_short")
        assert place.data["qty"] == pytest.approx(0.15)


# ── Notional floor ───────────────────────────────────────────────────────────

class TestNotionalFloor:
    def test_below_min_notional_no_arm(self):
        m = HedgeManager()
        # ratio 0.03 × qty 1.0 × mark 100 = $3 < $6 floor
        acts = m.evaluate(_ctx(longs=[_long(entry=100.0, mark=100.07)],
                               hedge_mark=100.0))
        assert acts == []
        assert m.plans() == []

    def test_at_min_notional_arms(self):
        m = HedgeManager()
        # 0.15 × 1.0 × 100 = $15 ≥ $6
        acts = m.evaluate(_ctx(longs=[_long(entry=100.0, mark=100.25)],
                               hedge_mark=100.0))
        assert "place_limit_short" in _kinds(acts)


# ── Basis gate ───────────────────────────────────────────────────────────────

class TestBasisGate:
    def test_stressed_blocks(self):
        m = HedgeManager()
        acts = m.evaluate(_ctx(longs=[_long(mark=BASE * 1.0007)],
                               basis_stressed={SYM: True}))
        assert "place_limit_short" not in _kinds(acts)
        assert "hedge_basis_blocked" in _events(acts)

    def test_basis_below_min_blocks(self):
        m = HedgeManager()
        acts = m.evaluate(_ctx(longs=[_long(mark=BASE * 1.0007)],
                               basis_bp={SYM: -5.0}))
        assert "place_limit_short" not in _kinds(acts)
        ev = [a for a in acts if a.kind == "event"
              and a.data.get("name") == "hedge_basis_blocked"]
        assert ev and ev[0].data["fields"]["reason"] == "basis_below_min"

    def test_open_gate_arms(self):
        m = HedgeManager(HedgeKnobs(short_floor_usd=10_000.0))
        acts = m.evaluate(_ctx(longs=[_long(mark=BASE * 1.0007)],
                               basis_bp={SYM: 3.0},
                               basis_stressed={SYM: False}))
        assert "place_limit_short" in _kinds(acts)


# ── Leverage derivation ──────────────────────────────────────────────────────

class TestLeverageDerivation:
    def test_math_isolated(self):
        """No cross buffer (free_equity unknown/0) → the 18% stop is
        unhedgeable under the clamp[5,15] floor; tighter stops derive."""
        assert derive_hedge_leverage(0.18) == 0    # int(1/0.235)=4 < 5
        assert derive_hedge_leverage(0.16) == 0    # int(1/0.21)=4 < 5
        assert derive_hedge_leverage(0.15) == 5    # int(1/0.1975)=5 (floor)
        assert derive_hedge_leverage(0.10) == 7    # int(1/0.135)
        assert derive_hedge_leverage(0.05) == 13   # int(1/0.0725)
        assert derive_hedge_leverage(0.02) == 15   # int(1/0.035)=28 → clamp
        assert derive_hedge_leverage(0.0) == 0
        assert derive_hedge_leverage("bad") == 0

    def test_math_cross_buffer(self):
        # Governor's corrected BOME example: $240 free on $20 notional
        # absorbs a 1200% adverse move → the buffer alone covers → 15×.
        assert derive_hedge_leverage(0.18, notional=20.0,
                                     free_equity=240.0) == 15
        # cb = 10/100 = 0.10 → mc = 0.135 → int(7.4) = 7
        assert derive_hedge_leverage(0.18, notional=100.0,
                                     free_equity=10.0) == 7
        # cb = 0.135 → mc = 0.10 → int(10) = 10
        assert derive_hedge_leverage(0.18, notional=100.0,
                                     free_equity=13.5) == 10
        # boundary: cb exactly = required+mmr → mc ≤ 0 → lev_max
        assert derive_hedge_leverage(0.18, notional=100.0,
                                     free_equity=23.5) == 15
        # unknown equity on a real notional = isolated (fail-closed)
        assert derive_hedge_leverage(0.18, notional=100.0,
                                     free_equity=0.0) == 0
        # knob mirrors still bind
        assert derive_hedge_leverage(0.18, notional=100.0,
                                     free_equity=13.5, lev_min=2) == 10
        assert derive_hedge_leverage(0.10, notional=100.0,
                                     free_equity=10.0, lev_max=10) == 10

    def test_unhedgeable_skip_event(self):
        m = HedgeManager(HedgeKnobs(catastrophic_stop_pct=0.45))
        acts = m.evaluate(_ctx(longs=[_long(mark=BASE * 1.0007)],
                               hedge_free_equity=0.0))
        assert "place_limit_short" not in _kinds(acts)
        assert "hedge_unhedgeable_skip" in _events(acts)
        assert m.plans() == []

    def test_isolated_18pct_stop_unhedgeable_live_path(self):
        """hedge_free_equity=0 (unknown) + the default 18% stop → the
        arming path skips as unhedgeable (the cross buffer is what makes
        the 18% stop viable)."""
        m = HedgeManager()
        acts = m.evaluate(_ctx(longs=[_long(mark=BASE * 1.0007)],
                               hedge_free_equity=0.0))
        assert "place_limit_short" not in _kinds(acts)
        assert "hedge_unhedgeable_skip" in _events(acts)


# ── Chase state machine ──────────────────────────────────────────────────────

class TestChase:
    def _chasing(self, now=10_000.0):
        m, acts = _armed_manager(now=now)
        plan = m.plans()[0]
        m.on_order_placed(plan.plan_id, "ord-1")
        assert plan.state == "chasing"
        return m, plan

    def test_small_fall_no_amend(self):
        m, plan = self._chasing()
        # order price = 1000.5; fall 0.1% < max(0.25%, 2×0.05%) → hold
        acts = m.evaluate(_ctx(now=10_004.0, longs=[_long(mark=BASE * 1.0007)],
                               hedge_mark=plan.limit_price * 0.999))
        assert "amend_short" not in _kinds(acts)

    def test_qualifying_fall_amends_down(self):
        m, plan = self._chasing()
        new_mark = plan.limit_price * 0.997   # −0.3% ≥ 0.25%
        acts = m.evaluate(_ctx(now=10_020.0, longs=[_long(mark=BASE * 1.0007)],
                               hedge_mark=new_mark))
        amend = _one(acts, "amend_short")
        assert amend.data["order_id"] == "ord-1"
        assert amend.data["new_price"] == pytest.approx(new_mark * 1.0005)
        assert plan.chase_steps == 1
        assert "hedge_chase_amend" in _events(acts)

    def test_min_amend_interval(self):
        m, plan = self._chasing()
        new_mark = plan.limit_price * 0.997
        m.evaluate(_ctx(now=10_020.0, longs=[_long(mark=BASE * 1.0007)],
                        hedge_mark=new_mark))
        # second qualifying fall but only 5s later (< 15s budget) → held
        acts = m.evaluate(_ctx(now=10_025.0, longs=[_long(mark=BASE * 1.0007)],
                               hedge_mark=plan.limit_price * 0.997))
        assert "amend_short" not in _kinds(acts)

    def test_step_cap_then_market_conversion(self):
        m, plan = self._chasing()
        now = 10_020.0
        for i in range(3):
            acts = m.evaluate(_ctx(now=now, longs=[_long(mark=BASE * 1.0007)],
                                   hedge_mark=plan.limit_price * 0.997))
            assert "amend_short" in _kinds(acts), f"step {i + 1}"
            now += 16.0
        assert plan.chase_steps == 3
        acts = m.evaluate(_ctx(now=now, longs=[_long(mark=BASE * 1.0007)],
                               hedge_mark=plan.limit_price * 0.997))
        conv = _one(acts, "convert_market_short")
        assert conv.data["qty"] == pytest.approx(plan.qty)
        assert plan.state == "converting"
        assert "hedge_chase_market_conversion" in _events(acts)

    def test_market_conversion_fill_within_cap(self):
        m, plan = self._chasing()
        plan.state = "converting"
        fill = BASE * 0.9995   # 0.05% below mark 1000 — inside the 0.1% cap
        acts = m.evaluate(_ctx(
            now=10_020.0, longs=[_long(mark=BASE * 1.0007)], hedge_mark=BASE,
            hedge_positions={SYM: {"qty": plan.qty, "entry": fill}}))
        assert "hedge_aborted" not in _events(acts)
        assert plan.state == "filled"
        assert "set_protection" in _kinds(acts)

    def test_market_conversion_slippage_abort(self):
        m, plan = self._chasing()
        plan.state = "converting"
        fill = BASE * 0.997   # 0.3% below mark — beyond the 0.1% cap
        acts = m.evaluate(_ctx(
            now=10_020.0, longs=[_long(mark=BASE * 1.0007)], hedge_mark=BASE,
            hedge_positions={SYM: {"qty": plan.qty, "entry": fill}}))
        assert "hedge_aborted" in _events(acts)
        close = _one(acts, "close_short_market")
        assert close.data["reason"] == "abort_slippage"
        assert plan.state == "closing"

    def test_market_conversion_rejected_aborts(self):
        m, plan = self._chasing()
        plan.state = "converting"
        acts = m.on_place_failed(plan.plan_id, "market_conversion_rejected")
        assert plan.state == "aborted"
        assert "hedge_aborted" in _events(acts)


# ── Chase ATR abandonment (2026-09-19 Governor budget) ──────────────────────

class TestChaseAtrAbandon:
    def _atr_chasing(self):
        m = HedgeManager(HedgeKnobs(short_floor_usd=10_000.0))
        m.evaluate(_ctx(now=10_000.0, longs=[_long(mark=BASE * 1.0007)],
                        atr_abs={SYM: 10.0}))
        plan = m.plans()[0]
        assert plan.arm_mark == pytest.approx(BASE)
        assert plan.arm_atr == pytest.approx(10.0)
        m.on_order_placed(plan.plan_id, "ord-1")
        return m, plan

    def test_drift_beyond_1atr_abandons(self):
        m, plan = self._atr_chasing()
        # mark 989 < arm_mark 1000 − 1.0 × 10 → thesis changed → abandon
        acts = m.evaluate(_ctx(now=10_020.0, longs=[_long(mark=BASE * 1.0007)],
                               hedge_mark=BASE - 11.0))
        assert plan.state == "aborted"
        assert _one(acts, "registry_update").data["fields"]["state"] == "aborted"
        ev = [a for a in acts if a.kind == "event"
              and a.data.get("name") == "hedge_aborted"]
        assert ev and ev[0].data["fields"]["reason"] == "chase_atr_drift"
        cancel = _one(acts, "cancel_order")
        assert cancel.data["order_id"] == "ord-1"
        # a dead plan never converts — no market order follows
        assert "convert_market_short" not in _kinds(acts)

    def test_drift_within_1atr_chases(self):
        m, plan = self._atr_chasing()
        # mark 992 ≥ 1000 − 10 AND ≥ 0.3% below the order price → amend
        acts = m.evaluate(_ctx(now=10_020.0, longs=[_long(mark=BASE * 1.0007)],
                               hedge_mark=BASE * 0.992))
        assert plan.state == "chasing"
        assert "amend_short" in _kinds(acts)
        assert "hedge_aborted" not in _events(acts)

    def test_no_atr_guard_inert(self):
        """arm_atr=0 (no ATR plane at arming) → the abandon branch is inert
        and the chase machinery runs exactly as before."""
        m, acts = _armed_manager()
        plan = m.plans()[0]
        assert plan.arm_atr == 0.0
        m.on_order_placed(plan.plan_id, "ord-1")
        acts = m.evaluate(_ctx(now=10_020.0, longs=[_long(mark=BASE * 1.0007)],
                               hedge_mark=BASE * 0.90))   # −10% crash
        assert "hedge_aborted" not in _events(acts)
        assert plan.state in ("chasing", "converting")


# ── Protection on fill (exact args through the wrapper) ─────────────────────

class _MockClient:
    """Recording mock of the BybitClient surface the wrapper touches."""
    hedge_mode = True

    def __init__(self, positions=None):
        self.calls = []
        self._positions = positions or []

    async def place_order(self, od):
        self.calls.append(("place_order", od))
        return SimpleNamespace(success=True, order_id="ord-x")

    async def amend_order(self, symbol, order_id, new_price=None, new_qty=None):
        self.calls.append(("amend_order", symbol, order_id, new_price))
        return True

    async def cancel_order(self, order_id, symbol=""):
        self.calls.append(("cancel_order", order_id, symbol))
        return True

    async def update_leverage(self, symbol, leverage):
        self.calls.append(("update_leverage", symbol, leverage))
        return True

    async def set_trailing_stop(self, symbol, position_idx, trailing_stop_abs,
                                active_price=None, tpsl_mode="Full"):
        self.calls.append(("set_trailing_stop", symbol, position_idx,
                           trailing_stop_abs, active_price, tpsl_mode))
        return True

    async def replace_stop_order(self, **kw):
        self.calls.append(("replace_stop_order", kw))
        return SimpleNamespace(success=True, order_id="posstop-x")

    async def close_position_market(self, **kw):
        self.calls.append(("close_position_market", kw))
        return SimpleNamespace(success=True)

    async def get_positions(self):
        return list(self._positions)

    async def get_account_balance(self):
        self.calls.append(("get_account_balance",))
        return 500.0


def _run(coro):
    return asyncio.run(coro)


class TestProtectionOnFill:
    def test_fill_emits_exact_protection_payload(self):
        m, _ = _armed_manager()
        plan = m.plans()[0]
        m.on_order_placed(plan.plan_id, "ord-1")
        fill = BASE * 1.0004
        acts = m.evaluate(_ctx(
            now=10_005.0, longs=[_long(mark=BASE * 1.0007)],
            hedge_positions={SYM: {"qty": 0.03, "entry": fill}}))
        prot = _one(acts, "set_protection")
        assert prot.data["trail_abs"] == pytest.approx(0.08 * fill)
        assert prot.data["active_price"] == pytest.approx(fill)
        assert prot.data["catastrophic_stop"] == pytest.approx(fill * 1.18)
        assert "hedge_filled" in _events(acts)
        assert plan.state == "filled"

    def test_wrapper_maps_exact_client_args(self):
        client = _MockClient()
        w = main._BybitHedgeWrapper(client)
        fill = 1000.4
        trail_abs = 0.08 * fill
        cat = fill * 1.18
        assert _run(w.set_trailing_stop(SYM, trail_abs, active_price=fill)) is True
        assert _run(w.place_catastrophic_stop(SYM, cat)) is True
        # Re-encoded 2026-09-19 (D52 S1): the mock has no get_spec → the
        # wrapper's unknown-tick fallback fires → active = fill × 0.9995,
        # strictly below entry BY CONSTRUCTION (the pre-S1 pass-through of
        # the fill price itself was the 314-rejection defect — Bybit demands
        # a Sell trail activation strictly below session average price).
        assert client.calls[0] == ("set_trailing_stop", SYM, 2,
                                   pytest.approx(trail_abs),
                                   pytest.approx(fill * 0.9995), "Full")
        kw = client.calls[1][1]
        assert kw["new_stop_price"] == pytest.approx(cat)
        assert kw["side"] == "short"

    def test_wrapper_hedge_mode_idx_and_oneway(self):
        client = _MockClient()
        assert main._BybitHedgeWrapper(client)._short_pos_idx() == 2
        client.hedge_mode = False
        assert main._BybitHedgeWrapper(client)._short_pos_idx() == 0

    def test_wrapper_limit_short_is_postonly(self):
        client = _MockClient()
        w = main._BybitHedgeWrapper(client)
        oid = _run(w.place_limit_short(SYM, 0.03, 1000.5))
        assert oid == "ord-x"
        _, od = client.calls[0]
        assert od["side"] == "short"
        assert od["order_type"] == "Limit"
        assert od["time_in_force"] == "PostOnly"
        assert od["qty"] == 0.03 and od["price"] == 1000.5

    def test_wrapper_close_short_market(self):
        client = _MockClient()
        w = main._BybitHedgeWrapper(client)
        assert _run(w.close_short_market(SYM, 0.03)) is True
        kw = client.calls[0][1]
        assert kw["side"] == "short" and kw["size"] == 0.03

    def test_wrapper_get_account_equity(self):
        client = _MockClient()
        w = main._BybitHedgeWrapper(client)
        assert _run(w.get_account_equity()) == 500.0
        client.get_account_balance = None
        assert _run(w.get_account_equity()) == 0.0   # failure → fail-closed

    def test_protection_confirmed_transitions(self):
        m, _ = _armed_manager()
        plan = m.plans()[0]
        m.on_order_placed(plan.plan_id, "ord-1")
        m.evaluate(_ctx(now=10_005.0, longs=[_long(mark=BASE * 1.0007)],
                        hedge_positions={SYM: {"qty": 0.03, "entry": BASE}}))
        acts = m.on_protection_ok(plan.plan_id)
        assert plan.state == "protected"
        assert "hedge_protected" in _events(acts)


# ── Unwind on long close ─────────────────────────────────────────────────────

class TestUnwind:
    def test_primary_close_closes_leg_at_market(self):
        m, _ = _armed_manager()
        plan = m.plans()[0]
        m.on_order_placed(plan.plan_id, "ord-1")
        m.evaluate(_ctx(now=10_005.0, longs=[_long(mark=BASE * 1.0007)],
                        hedge_positions={SYM: {"qty": 0.03, "entry": BASE}}))
        m.on_protection_ok(plan.plan_id)
        # Long vanishes (ANY reason) → immediate market close of the leg
        acts = m.evaluate(_ctx(now=10_010.0, longs=[], hedge_mark=0,
                               hedge_positions={SYM: {"qty": 0.03,
                                                      "entry": BASE}}))
        close = _one(acts, "close_short_market")
        assert close.data["reason"] == "primary_closed"
        assert close.data["qty"] == pytest.approx(0.03)
        assert "hedge_cover_primary_closed" in _events(acts)
        assert plan.state == "closing"
        # confirmed → covered
        acts2 = m.on_close_confirmed(plan.plan_id)
        assert plan.state == "covered"
        assert _one(acts2, "registry_update").data["fields"]["state"] == "covered"

    def test_primary_close_while_chasing_cancels(self):
        m, _ = _armed_manager()
        plan = m.plans()[0]
        m.on_order_placed(plan.plan_id, "ord-1")
        acts = m.evaluate(_ctx(now=10_005.0, longs=[], hedge_mark=0))
        cancel = _one(acts, "cancel_order")
        assert cancel.data["order_id"] == "ord-1"
        assert plan.state == "covered"

    def test_unknown_snapshot_never_reads_as_gone(self):
        """A failed position poll (None) must NOT trigger fill/cover."""
        m, _ = _armed_manager()
        plan = m.plans()[0]
        m.on_order_placed(plan.plan_id, "ord-1")
        m.evaluate(_ctx(now=10_005.0, longs=[_long(mark=BASE * 1.0007)],
                        hedge_positions={SYM: {"qty": 0.03, "entry": BASE}}))
        m.on_protection_ok(plan.plan_id)
        acts = m.evaluate(_ctx(now=10_010.0, longs=[_long(mark=BASE * 1.0007)],
                               hedge_positions=None))
        assert plan.state == "protected"
        assert "close_short_market" not in _kinds(acts)


# ── Whipsaw cooldown ─────────────────────────────────────────────────────────

class TestWhipsawCooldown:
    def test_trail_cover_arms_cooldown(self):
        m, _ = _armed_manager()
        plan = m.plans()[0]
        m.on_order_placed(plan.plan_id, "ord-1")
        m.evaluate(_ctx(now=10_005.0, longs=[_long(mark=BASE * 1.0007)],
                        hedge_positions={SYM: {"qty": 0.03, "entry": BASE}}))
        m.on_protection_ok(plan.plan_id)
        # Exchange-side cover: the short is gone
        acts = m.evaluate(_ctx(now=10_100.0, longs=[_long(mark=BASE * 1.0007)],
                               hedge_positions={}))
        assert "hedge_leg_covered" in _events(acts)
        assert plan.state == "covered"
        # Re-hedge attempt inside the cooloff → nothing
        acts2 = m.evaluate(_ctx(now=10_200.0, longs=[_long(mark=BASE * 1.0007)],
                                hedge_positions={}))
        assert "place_limit_short" not in _kinds(acts2)
        # After the cooloff the symbol may arm again
        acts3 = m.evaluate(_ctx(now=10_100.0 + 7201.0,
                                longs=[_long(mark=BASE * 1.0007)],
                                hedge_positions={}))
        assert "place_limit_short" in _kinds(acts3)


# ── Dynamic short limit ──────────────────────────────────────────────────────

class TestShortLimit:
    def test_cap_math(self):
        assert short_limit_cap(20.0, 0.5, []) == 20.0
        assert short_limit_cap(20.0, 0.5, [-50.0, 10.0]) == 20.0
        assert short_limit_cap(20.0, 0.5, [100.0]) == 50.0

    def test_room_exhausted_blocks(self):
        m = HedgeManager()
        # Long A arms: rung 1.00 → 0.15 × 1.0 × 100 = $15 of the $20 floor
        acts = m.evaluate(_ctx(
            longs=[_long(symbol="AAA-USD", entry=100.0, mark=100.25)],
            hedge_mark=100.0))
        assert "place_limit_short" in _kinds(acts)
        # Long B: another $15 plan, but S_max 20 − current 15 = $5 room
        acts2 = m.evaluate(_ctx(
            longs=[_long(symbol="AAA-USD", entry=100.0, mark=100.25),
                   _long(symbol="BBB-USD", entry=100.0, mark=100.25,
                         opened_at_ms=2_000_000)],
            hedge_mark=100.0))
        assert "hedge_short_limit_blocked" in _events(acts2)
        assert len([a for a in acts2 if a.kind == "place_limit_short"]) == 0

    def test_upnl_raises_cap(self):
        m = HedgeManager()
        # upnl = (100.25−100)×1 = 0.25 on A — cap stays at the $20 floor
        # but a second $15 plan fits only if the cap rises: give B big upnl.
        m.evaluate(_ctx(
            longs=[_long(symbol="AAA-USD", entry=100.0, mark=100.25)],
            hedge_mark=100.0))
        acts = m.evaluate(_ctx(
            longs=[_long(symbol="AAA-USD", entry=100.0, mark=100.25),
                   _long(symbol="BBB-USD", entry=100.0, mark=170.0,
                         opened_at_ms=2_000_000)],   # upnl $70 → cap $35.1
            hedge_mark=100.0))
        # B's own rung: ROE = 70% × 5 → top rung, ratio 0.15 → $15 ≤ 35.1−15
        assert len([a for a in acts if a.kind == "place_limit_short"]) == 1

    def test_shrinking_upnl_never_force_unwinds(self):
        """S_max is an ENTRY gate only (2026-09-19 Governor item 5): when U
        collapses below the deployed short notional, the armed plan is NOT
        force-unwound — no cancel, no close, no abort."""
        m = HedgeManager(HedgeKnobs(short_floor_usd=10_000.0))
        m.evaluate(_ctx(longs=[_long(mark=BASE * 1.0025)],   # top rung, $150
                        atr_abs={SYM: 10.0}))
        plan = m.plans()[0]
        m.on_order_placed(plan.plan_id, "ord-1")
        # U collapses: mark back to entry → upnl 0 → S_max hits the $20 floor,
        # far below the deployed $150 short notional.
        acts = m.evaluate(_ctx(now=10_020.0, longs=[_long(mark=BASE)],
                               hedge_mark=BASE))
        assert plan.state == "chasing"
        assert "cancel_order" not in _kinds(acts)
        assert "close_short_market" not in _kinds(acts)
        assert "hedge_aborted" not in _events(acts)


# ── Rescue shadow ────────────────────────────────────────────────────────────

class TestRescueShadow:
    def _rescue_long(self, mark=BASE * 0.975):
        # ROE = −2.5% × 5 = −12.5% ≤ −10%, aged past the 1h floor
        return _long(mark=mark, age_s=3700.0)

    def test_would_arm_emits_zero_orders(self):
        m = HedgeManager()
        acts = m.evaluate(_ctx(longs=[self._rescue_long()],
                               mover_relief={SYM}))
        assert "hedge_rescue_would_arm" in _events(acts)
        upsert = _one(acts, "registry_upsert")
        assert upsert.data["state"] == "shadow"
        # ZERO order-carrying actions — the refuted mode never trades
        assert not (set(_kinds(acts)) & ORDER_ACTION_KINDS)

    def test_no_trigger_without_drift_confirm(self):
        m = HedgeManager()
        acts = m.evaluate(_ctx(longs=[self._rescue_long()]))
        assert acts == []

    def test_drift_confirm_any_leg(self):
        for kw in ({"mover_relief": {SYM}},
                   {"funding_carry_adverse": {SYM: True}},
                   {"oi_rising": {SYM: True}}):
            m = HedgeManager()
            acts = m.evaluate(_ctx(longs=[self._rescue_long()], **kw))
            assert "hedge_rescue_would_arm" in _events(acts), kw

    def test_too_young_no_arm(self):
        m = HedgeManager()
        acts = m.evaluate(_ctx(longs=[self._rescue_long().__class__(
            **{**self._rescue_long().__dict__, "age_s": 1800.0})],
            mover_relief={SYM}))
        assert acts == []

    def test_would_cover_pair_net(self):
        """The pair_net leg is arithmetically frozen once the shadow entry
        equals the mark at arm time (a hedge locks pair PnL) — pin the leg
        with the entry a better-filled hedge would have carried."""
        m = HedgeManager()
        m.evaluate(_ctx(longs=[self._rescue_long()], hedge_mark=975.0,
                        mover_relief={SYM}))
        plan = m.plans()[0]
        plan.entry_price = 1005.0   # filled above the underwater mark
        acts = m.evaluate(_ctx(now=10_100.0, hedge_mark=1001.0,
                               longs=[self._rescue_long(mark=1001.0)]))
        ev = [a for a in acts if a.kind == "event"
              and a.data.get("name") == "hedge_rescue_would_cover"]
        assert ev and ev[0].data["fields"]["reason"] == "pair_net_nonnegative"
        assert not (set(_kinds(acts)) & ORDER_ACTION_KINDS)

    def test_would_cover_giveback(self):
        m = HedgeManager()
        # Arm at the CURRENT mark (975) — a real hedge enters at the hedge
        # venue mark, below the underwater primary entry (1000).
        m.evaluate(_ctx(longs=[self._rescue_long()], hedge_mark=975.0,
                        mover_relief={SYM}))
        # Drop further: short profit peaks at +3.59% (mark 940 vs entry 975)
        m.evaluate(_ctx(now=10_050.0, hedge_mark=940.0,
                        longs=[self._rescue_long(mark=940.0)]))
        # Recover to 960: short gain 1.54% → giveback ≥ 50% of the peak
        acts = m.evaluate(_ctx(now=10_100.0, hedge_mark=960.0,
                               longs=[self._rescue_long(mark=960.0)]))
        ev = [a for a in acts if a.kind == "event"
              and a.data.get("name") == "hedge_rescue_would_cover"]
        assert ev, _kinds(acts)
        assert ev[0].data["fields"]["reason"] == "short_profit_giveback"
        assert not (set(_kinds(acts)) & ORDER_ACTION_KINDS)

    def test_shadow_plan_removed_with_primary(self):
        m = HedgeManager()
        m.evaluate(_ctx(longs=[self._rescue_long()], mover_relief={SYM}))
        acts = m.evaluate(_ctx(now=10_100.0, longs=[], hedge_mark=0))
        assert "registry_remove" in _kinds(acts)
        assert m.plans() == []


# ── Master / knob kills ──────────────────────────────────────────────────────

class TestKillSwitches:
    def test_master_off_zero_actions(self):
        m = HedgeManager()
        acts = m.evaluate(_ctx(enabled=False,
                               longs=[_long(mark=BASE * 1.0025)],
                               mover_relief={SYM}))
        assert acts == []
        assert m.plans() == []

    def test_profit_lock_knob_off(self):
        m = HedgeManager()
        acts = m.evaluate(_ctx(profit_lock_enabled=False,
                               longs=[_long(mark=BASE * 1.0025)]))
        assert "place_limit_short" not in _kinds(acts)

    def test_rescue_knob_off(self):
        m = HedgeManager()
        longs = [_long(mark=BASE * 0.975, age_s=3700.0)]
        acts = m.evaluate(_ctx(rescue_shadow_enabled=False, longs=longs,
                               mover_relief={SYM}))
        assert "hedge_rescue_would_arm" not in _events(acts)

    def test_no_dual_mark_no_arm(self):
        m = HedgeManager()
        acts = m.evaluate(_ctx(longs=[_long(mark=BASE * 1.0007)],
                               hedge_mark=0))
        assert acts == []


# ── Registry-aware reconcile (main.py splice) ───────────────────────────────

class TestReconcile:
    def setup_method(self):
        main._HEDGE_RECONCILE_STATE["sig"] = None
        main._HEDGE_RECONCILE_STATE["ts"] = 0.0

    def test_disabled_noop(self):
        reg = HedgeRegistry()
        rows = [{"symbol": SYM, "side": "short", "size": 0.03, "venue": "bybit"}]
        assert main._hedge_reconcile_registry(rows, reg, False) == []

    def test_exchange_row_without_leg(self):
        reg = HedgeRegistry()
        rows = [{"symbol": SYM, "side": "short", "size": 0.03, "venue": "bybit"}]
        mm = main._hedge_reconcile_registry(rows, reg, True)
        assert mm == [("exchange_row_without_leg", (SYM, "short"))]

    def test_open_leg_without_row(self):
        reg = HedgeRegistry()
        reg.upsert("hpl-1", symbol=SYM, venue="bybit", side="short",
                   qty=0.03, entry_price=BASE, state="open")
        mm = main._hedge_reconcile_registry([], reg, True)
        assert mm == [("registry_leg_without_exchange_row", (SYM, "short"))]

    def test_shadow_leg_never_expected_on_exchange(self):
        reg = HedgeRegistry()
        reg.upsert("hrc-1", symbol=SYM, venue="bybit", side="short",
                   qty=1.0, entry_price=BASE, state="shadow")
        assert main._hedge_reconcile_registry([], reg, True) == []

    def test_matched_row_and_leg_clean(self):
        reg = HedgeRegistry()
        reg.upsert("hpl-1", symbol=SYM, venue="bybit", side="short",
                   qty=0.03, entry_price=BASE, state="open")
        rows = [{"symbol": SYM, "side": "Sell", "size": 0.03, "venue": "bybit"}]
        assert main._hedge_reconcile_registry(rows, reg, True) == []


# ── Config defaults ──────────────────────────────────────────────────────────

class TestConfig:
    def test_p2_defaults(self):
        s = Settings()
        assert s.hedge_enabled is False
        assert s.hedge_rescue_live is False            # refuted — never live
        assert s.hedge_rescue_shadow_enabled is True
        assert s.hedge_profit_lock_rungs == [(0.30, 0.03), (0.50, 0.06),
                                             (0.70, 0.10), (1.00, 0.15)]
        assert s.hedge_min_notional == 6.0
        assert s.hedge_min_basis_bp == -2.0
        assert s.hedge_leverage_min == 5
        # Re-encoded 2026-09-19 (A1, Governor directive): 15 → 7 — the
        # cross-buffer derivation at 15x left the catastrophic stop as the
        # only protection on a 6.7% adverse move; 7x halves that tail.
        assert s.hedge_leverage_max == 7
        assert s.hedge_trail_pct == 0.08
        assert s.hedge_catastrophic_stop_pct == 0.18
        assert s.hedge_chase_max_steps == 3
        assert s.hedge_chase_min_amend_s == 15.0
        assert s.hedge_chase_abandon_atr_mult == 1.0
        assert s.hedge_slippage_cap == 0.001
        assert s.hedge_whipsaw_cooloff_s == 7200.0
        assert s.hedge_short_floor_usd == 20.0
        assert s.hedge_short_upnl_frac == 0.5
        assert s.hedge_rescue_min_time_s == 3600.0
        assert s.hedge_rescue_roe_trigger_pct == -10.0


# ── Pure helpers ─────────────────────────────────────────────────────────────

class TestHelpers:
    def test_rung_for_roe(self):
        assert rung_for_roe(RUNGS, 0.1) is None
        assert rung_for_roe(RUNGS, 0.30) == (0.30, 0.03)
        assert rung_for_roe(RUNGS, 0.55) == (0.50, 0.06)
        assert rung_for_roe(RUNGS, 99.0) == (1.00, 0.15)


# ── Hedge sub-account (2026-09-19 Governor directive) ───────────────────────

class TestHedgeSubAccount:
    def test_client_credential_override(self):
        from execution.bybit_client import BybitClient
        c = BybitClient(Settings(), api_key="sub-k", api_secret="sub-s")
        assert c.api_key == "sub-k"
        assert c.api_secret == "sub-s"

    def test_client_config_fallback(self):
        from execution.bybit_client import BybitClient
        c = BybitClient(SimpleNamespace(
            bybit_api_key="main-k", bybit_api_secret="main-s"))
        assert c.api_key == "main-k"
        assert c.api_secret == "main-s"

    def test_hedge_account_defaults(self):
        s = Settings()
        assert s.bybit_hedge_api_key == ""
        assert s.bybit_hedge_api_secret == ""
        assert s.hedge_account_low_margin_usd == 30.0


# ── D52 S1/S3 (2026-09-19, hedge-trail-3-site + consult answer) ─────────────

class _SpecMockClient(_MockClient):
    """_MockClient + the spec surface the S1 adjustment reads."""
    def __init__(self, tick=0.0, **kw):
        super().__init__(**kw)
        self._tick = tick

    def get_spec(self, symbol):
        return {"tick": self._tick, "step": 0.0, "min_qty": 0.0,
                "min_notional": 5.0}


class TestTrailActivePriceS1:
    """Bybit venue rule: a Sell-position trailing activation must be STRICTLY
    below session average price — the wrapper enforces it by construction
    (round to the real tick, one tick below when the fill sits on it)."""

    def _active(self, client, fill):
        w = main._BybitHedgeWrapper(client)
        assert _run(w.set_trailing_stop(SYM, 0.08 * fill,
                                        active_price=fill)) is True
        return client.calls[0][4]

    def test_on_tick_fill_lands_one_tick_below(self):
        # The live rejection: fill 42780.0 == session average → "should be
        # less than session_average_price" (314 lifetime rejects).
        ap = self._active(_SpecMockClient(tick=1.0), 42780.0)
        assert ap == pytest.approx(42779.0)

    def test_off_tick_fill_rounds_down_to_tick(self):
        ap = self._active(_SpecMockClient(tick=1.0), 42780.5)
        assert ap == pytest.approx(42780.0)

    def test_fractional_tick(self):
        ap = self._active(_SpecMockClient(tick=0.25), 100.13)
        assert ap == pytest.approx(100.0)

    def test_unknown_tick_falls_back_five_bp(self):
        ap = self._active(_SpecMockClient(tick=0.0), 1000.4)
        assert ap == pytest.approx(1000.4 * 0.9995)

    def test_always_strictly_below_entry(self):
        for tick, fill in ((1.0, 42780.0), (1.0, 42780.5), (0.25, 100.13),
                           (0.0001, 0.5123), (0.0, 999.9)):
            assert self._active(_SpecMockClient(tick=tick), fill) < fill


class TestProtectionFailureLatchS3:
    """D52 S3: venue-rejected protection used to re-emit every 20s forever
    (hedge_protected 0 lifetime, 662 combined rejects). The latch stops it;
    a latched trail with a confirmed catastrophic stop still completes."""

    def _filled_manager(self, **mkw):
        m, _ = _armed_manager(**mkw)
        plan = m.plans()[0]
        m.on_order_placed(plan.plan_id, "ord-1")
        fill = BASE * 1.0004
        m.evaluate(_ctx(now=10_005.0, longs=[_long(mark=BASE * 1.0007)],
                        hedge_positions={SYM: {"qty": 0.03, "entry": fill}}))
        assert plan.state == "filled"
        return m, plan

    def test_below_max_no_events(self):
        m, plan = self._filled_manager()
        assert m.on_protection_failed(plan.plan_id, "trail_rejected") == []
        assert m.on_protection_failed(plan.plan_id, "trail_rejected") == []
        assert plan.protection_fail_count == 2
        assert plan.protection_unavailable is False
        assert plan.state == "filled"

    def test_latch_with_confirmed_stop_completes_protection(self):
        m, plan = self._filled_manager()
        m.on_protection_failed(plan.plan_id, "trail_rejected",
                               stop_confirmed=True)
        m.on_protection_failed(plan.plan_id, "trail_rejected",
                               stop_confirmed=True)
        acts = m.on_protection_failed(plan.plan_id, "trail_rejected",
                                      stop_confirmed=True)
        names = _events(acts)
        assert "hedge_trail_unavailable" in names
        assert "hedge_protected" in names
        assert plan.protection_unavailable is True
        assert plan.state == "protected"

    def test_latch_without_stop_stays_filled_and_silent(self):
        m, plan = self._filled_manager()
        for _ in range(2):
            assert m.on_protection_failed(plan.plan_id, "both_rejected") == []
        acts = m.on_protection_failed(plan.plan_id, "both_rejected")
        assert _events(acts) == ["hedge_trail_unavailable"]
        assert plan.protection_unavailable is True
        assert plan.state == "filled"
        # Re-emit guard: the latched plan never emits set_protection again.
        follow = m.evaluate(_ctx(now=10_030.0,
                                 longs=[_long(mark=BASE * 1.0007)],
                                 hedge_positions={SYM: {"qty": 0.03,
                                                        "entry": BASE}}))
        assert "set_protection" not in _kinds(follow)

    def test_knob_zero_never_latches(self):
        m, plan = self._filled_manager(protection_fail_max=0)
        for _ in range(10):
            assert m.on_protection_failed(plan.plan_id, "trail_rejected") == []
        assert plan.protection_unavailable is False
        # Legacy re-emit continues while FILLED and past the 20s throttle.
        follow = m.evaluate(_ctx(now=10_030.0,
                                 longs=[_long(mark=BASE * 1.0007)],
                                 hedge_positions={SYM: {"qty": 0.03,
                                                        "entry": BASE}}))
        assert "set_protection" in _kinds(follow)

    def test_config_knob_default(self):
        assert Settings().hedge_protection_fail_max == 3


class TestLeverageMaxClampA1:
    """A1 (2026-09-19 Governor): hedge leverage_max 15 → 7. The 0.02-stop
    derivation (min_clearance 0.035 → raw lev 28) clamped at 15 before the
    change; HedgeKnobs(leverage_max=7) must clamp it at 7."""

    def test_legacy_default_yields_15(self):
        assert derive_hedge_leverage(0.02) == 15

    def test_knob_seven_clamps_at_seven(self):
        k = HedgeKnobs(leverage_max=7)
        assert derive_hedge_leverage(
            0.02, lev_min=k.leverage_min, lev_max=k.leverage_max) == 7

    def test_config_default_is_seven(self):
        assert Settings().hedge_leverage_max == 7
