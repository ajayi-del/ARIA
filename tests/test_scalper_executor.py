"""
tests/test_scalper_executor.py — pins for intelligence/scalper_executor.py
(Governor order 2026-10-10: scalper LIVE alongside campaign trades).

All venue/clock/config handles are fakes injected through the constructor —
the module under test carries zero hidden I/O. Async tests use the repo's
unittest.IsolatedAsyncioTestCase idiom (test_aster_client.py).
"""
import unittest
from types import SimpleNamespace

from intelligence.scalper_executor import (
    CLORD_PREFIX,
    STRATEGY_TYPE,
    ScalperExecutor,
)


# ── fakes ────────────────────────────────────────────────────────────────

class FakeResult:
    """OrderResult-shaped: success == (error is None), schemas.py:83."""

    def __init__(self, error=None, order_id="V1"):
        self.order_id = order_id
        self.error = error
        self.status = "rejected" if error else "accepted"

    @property
    def success(self):
        return self.error is None


class FakeClient:
    """Venue client double: records place_order params; scripts results
    or a raised exception. Carries NO _build_order_item so the module's
    canonical fallback builder is exercised."""

    def __init__(self, results=None, exc=None):
        self.calls = []
        self._results = list(results or [])
        self._exc = exc

    async def place_order(self, params):
        # Snapshot: the executor mutates order items in place on the
        # nonce-retry path — storing the reference would alias call 1 to
        # the retry's rewritten clOrdIDs.
        self.calls.append({**params,
                           "orders": [dict(o) for o in params["orders"]]})
        if self._exc is not None:
            raise self._exc
        if self._results:
            return self._results.pop(0)
        return FakeResult()

    def get_tick_step(self, symbol, symbol_id=0):
        return (0.01, 0.001)


class Clock:
    """Mutable ms clock; step=0 keeps a fixed ts, step>0 yields fresh
    nonce ids on every read (the nonce-retry pin)."""

    def __init__(self, start=1_760_000_000_000, step=0):
        self._t = start
        self._step = step

    def __call__(self):
        t = self._t
        self._t += self._step
        return t


def _cfg(**over):
    base = dict(
        scalper_enabled=True,
        scalper_min_score_live=70,
        scalper_max_concurrent=3,
        scalper_min_notional_usd=5.0,
        scalper_max_notional_usd=15.0,
        scalper_margin_budget_usd=8.0,
        scalper_max_leverage=8,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _intent(symbol="BTC-USD", direction="long", grade="A", score=75,
            leverage=8, margin_per_position=1.0, tp_pct=0.015,
            stop_pct=0.008, entry_price=100.0, source="loss_reversal"):
    """Object-form intent (the getattr path — ScalpSignalGrade shape)."""
    return SimpleNamespace(
        grade=grade, direction=direction, source=source,
        primary_symbol=symbol, ecosystem_symbols=(symbol,),
        prediction_score=score, leverage=leverage,
        margin_per_position=margin_per_position,
        tp_pct=tp_pct, stop_pct=stop_pct, entry_price=entry_price)


def _intent_dict(**kw):
    """Dict-form intent (the mapping path)."""
    d = dict(symbol="BTC-USD", direction="long", grade="A", score=75,
             leverage=8, margin_per_position=1.0, tp_pct=0.015,
             stop_pct=0.008, entry_price=100.0)
    d.update(kw)
    return d


def _exec(cfg=None, client=None, clock=None, registry=None):
    return ScalperExecutor(
        cfg=cfg or _cfg(), client=client or FakeClient(),
        registry=(registry if registry is not None
                  else {"BTC-USD": 7, "ETH-USD": 8}),
        now_ms=clock or Clock())


ACCT = {"account_id": 42, "marks": {"BTC-USD": 100.0, "ETH-USD": 50.0}}


# ── kill switch / grade gate ─────────────────────────────────────────────

class TestLiveGate(unittest.IsolatedAsyncioTestCase):

    async def test_kill_switch_off_zero_orders_drop_rows(self):
        client = FakeClient()
        ex = _exec(cfg=_cfg(scalper_enabled=False), client=client)
        rows = await ex.execute_intents(
            [_intent(), _intent(symbol="ETH-USD")], [], ACCT)
        self.assertEqual(client.calls, [])
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r["event"], "scalper_exec_dropped")
            self.assertEqual(r["reason"], "disabled")
            self.assertEqual(r["strategy_type"], STRATEGY_TYPE)

    async def test_grade_b_never_executes_even_live(self):
        client = FakeClient()
        ex = _exec(client=client)
        rows = await ex.execute_intents(
            [_intent(grade="B", score=80)], [], ACCT)
        self.assertEqual(client.calls, [])
        self.assertEqual(rows[0]["event"], "scalper_exec_dropped")
        self.assertEqual(rows[0]["reason"], "grade")

    async def test_score_below_live_floor_dropped(self):
        client = FakeClient()
        ex = _exec(client=client)  # floor 70
        rows = await ex.execute_intents([_intent(score=69)], [], ACCT)
        self.assertEqual(client.calls, [])
        self.assertEqual(rows[0]["reason"], "score")


# ── admission envelope ───────────────────────────────────────────────────

class TestEnvelope(unittest.IsolatedAsyncioTestCase):

    async def test_fourth_concurrent_intent_dropped(self):
        client = FakeClient()
        ex = _exec(client=client)  # max_concurrent 3
        rows = await ex.execute_intents([_intent() for _ in range(4)],
                                        [], ACCT)
        placed = [r for r in rows if r["event"] == "scalper_exec_placed"]
        dropped = [r for r in rows if r["event"] == "scalper_exec_dropped"]
        self.assertEqual(len(placed), 3)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(dropped[0]["reason"], "concurrency")
        self.assertEqual(len(client.calls), 3)

    async def test_open_positions_count_toward_concurrency(self):
        client = FakeClient()
        ex = _exec(client=client)
        open_scalps = [{"margin_usd": 0.5}, {"margin_usd": 0.5},
                       {"margin_usd": 0.5}]
        rows = await ex.execute_intents([_intent()], open_scalps, ACCT)
        self.assertEqual(client.calls, [])
        self.assertEqual(rows[0]["reason"], "concurrency")

    async def test_margin_budget_hard_clamp(self):
        client = FakeClient()
        ex = _exec(client=client)  # budget 8.0
        # $7.5 already committed; intent margin 15/8 = 1.875 -> 9.375 > 8.
        rows = await ex.execute_intents(
            [_intent(margin_per_position=100.0)],  # notional clamps to 15
            [{"margin_usd": 7.5}], ACCT)
        self.assertEqual(client.calls, [])
        self.assertEqual(rows[0]["reason"], "margin_budget")
        self.assertEqual(rows[0]["budget"], 8.0)

    async def test_margin_budget_counts_admitted_this_call(self):
        client = FakeClient()
        ex = _exec(cfg=_cfg(scalper_margin_budget_usd=1.5,
                            scalper_max_concurrent=10),
                   client=client)
        # Each intent: notional 15 / lev 8 = 1.875 margin — first fits the
        # $1.5 budget? No: 1.875 > 1.5 -> both drop. Use smaller leverage
        # so the first fits and the second exceeds.
        rows = await ex.execute_intents(
            [_intent_dict(margin_per_position=0.1, leverage=8),   # notional 5 (clamped) -> margin 0.625
             _intent_dict(margin_per_position=0.1, leverage=8)],  # second 0.625 -> 1.25 <= 1.5 fits too
            [], ACCT)
        self.assertEqual(
            [r["event"] for r in rows],
            ["scalper_exec_placed", "scalper_exec_placed"])
        # Tighten: budget 1.0 -> first 0.625 fits, second 1.25 > 1.0 drops.
        client2 = FakeClient()
        ex2 = _exec(cfg=_cfg(scalper_margin_budget_usd=1.0,
                             scalper_max_concurrent=10),
                    client=client2)
        rows2 = await ex2.execute_intents(
            [_intent_dict(margin_per_position=0.1, leverage=8),
             _intent_dict(margin_per_position=0.1, leverage=8)],
            [], ACCT)
        self.assertEqual(rows2[0]["event"], "scalper_exec_placed")
        self.assertEqual(rows2[1]["reason"], "margin_budget")
        self.assertEqual(len(client2.calls), 1)

    async def test_notional_clamped_into_band(self):
        client = FakeClient()
        ex = _exec(client=client)
        # 100 margin x 8 = $800 raw -> clamps to $15.
        rows = await ex.execute_intents(
            [_intent(margin_per_position=100.0)], [], ACCT)
        self.assertEqual(rows[0]["event"], "scalper_exec_placed")
        self.assertEqual(rows[0]["notional_usd"], 15.0)
        qty = float(client.calls[0]["orders"][0]["quantity"])
        self.assertAlmostEqual(qty, 15.0 / 100.0, places=6)
        # Dust margin -> clamps UP to the $5 floor.
        client2 = FakeClient()
        ex2 = _exec(client=client2)
        rows2 = await ex2.execute_intents(
            [_intent(margin_per_position=0.01)], [], ACCT)
        self.assertEqual(rows2[0]["notional_usd"], 5.0)
        self.assertEqual(rows2[0]["margin_usd"], round(5.0 / 8, 4))


# ── happy path: bracket shape both directions ────────────────────────────

class TestHappyPath(unittest.IsolatedAsyncioTestCase):

    async def test_long_bracket(self):
        client = FakeClient()
        ex = _exec(client=client)
        rows = await ex.execute_intents([_intent()], [], ACCT)
        self.assertEqual(len(client.calls), 1)
        row = rows[0]
        self.assertEqual(row["event"], "scalper_exec_placed")
        self.assertEqual(row["strategy_type"], STRATEGY_TYPE)
        self.assertEqual(row["source"], "loss_reversal")
        self.assertTrue(row["cl_ord_id"].startswith(CLORD_PREFIX))
        self.assertLessEqual(len(row["cl_ord_id"]), 36)
        self.assertEqual(row["order_id"], "V1")
        call = client.calls[0]
        self.assertEqual(call["accountID"], 42)
        self.assertEqual(call["symbolID"], 7)
        entry, tp, sl = call["orders"]
        # Entry: maker-first resting LIMIT, never market, never reduce-only.
        self.assertEqual(entry["type"], 1)
        self.assertEqual(entry["timeInForce"], 1)
        self.assertEqual(entry["side"], 1)           # long = buy
        self.assertFalse(entry["reduceOnly"])
        self.assertAlmostEqual(float(entry["price"]), 100.0)
        # TP: reduce-only limit at entry x 1.015, opposite side.
        self.assertEqual(tp["side"], 2)
        self.assertTrue(tp["reduceOnly"])
        self.assertAlmostEqual(float(tp["price"]), 101.5)
        # SL: reduce-only native stop at entry x 0.992, MARK_PRICE trigger.
        self.assertEqual(sl["side"], 2)
        self.assertTrue(sl["reduceOnly"])
        self.assertAlmostEqual(float(sl["stopPrice"]), 99.2)
        self.assertEqual(sl["stopType"], 1)
        self.assertEqual(sl["triggerType"], 2)

    async def test_short_bracket(self):
        client = FakeClient()
        ex = _exec(client=client)
        rows = await ex.execute_intents(
            [_intent_dict(symbol="ETH-USD", direction="short",
                          entry_price=50.0)], [], ACCT)
        self.assertEqual(len(client.calls), 1)
        entry, tp, sl = client.calls[0]["orders"]
        self.assertEqual(entry["side"], 2)           # short = sell
        self.assertFalse(entry["reduceOnly"])
        self.assertAlmostEqual(float(entry["price"]), 50.0)
        self.assertEqual(tp["side"], 1)
        self.assertTrue(tp["reduceOnly"])
        self.assertAlmostEqual(float(tp["price"]), 50.0 * (1 - 0.015))
        self.assertEqual(sl["side"], 1)
        self.assertTrue(sl["reduceOnly"])
        self.assertAlmostEqual(float(sl["stopPrice"]), 50.0 * (1 + 0.008))
        self.assertEqual(sl["stopType"], 1)
        self.assertEqual(sl["triggerType"], 2)
        self.assertEqual(rows[0]["event"], "scalper_exec_placed")

    async def test_dict_intent_marks_plane_entry(self):
        # No entry_price on the intent -> the injected marks plane prices it.
        client = FakeClient()
        ex = _exec(client=client)
        d = _intent_dict()
        del d["entry_price"]
        rows = await ex.execute_intents([d], [], ACCT)
        self.assertEqual(rows[0]["event"], "scalper_exec_placed")
        self.assertAlmostEqual(rows[0]["entry"], 100.0)


# ── fail-closed venue behavior ───────────────────────────────────────────

class TestFailClosed(unittest.IsolatedAsyncioTestCase):

    async def test_venue_raises_drop_no_propagation_no_retry(self):
        client = FakeClient(exc=RuntimeError("venue exploded"))
        ex = _exec(client=client)
        rows = await ex.execute_intents([_intent()], [], ACCT)
        self.assertEqual(rows[0]["event"], "scalper_exec_dropped")
        self.assertEqual(rows[0]["reason"], "venue_exception")
        self.assertEqual(len(client.calls), 1)       # no retry storm

    async def test_venue_rejection_no_retry(self):
        client = FakeClient(results=[FakeResult(error="insufficient margin")])
        ex = _exec(client=client)
        rows = await ex.execute_intents([_intent()], [], ACCT)
        self.assertEqual(rows[0]["reason"], "venue_rejected")
        self.assertIn("insufficient margin", rows[0]["error"])
        self.assertEqual(len(client.calls), 1)

    async def test_nonce_stale_single_retry_fresh_id(self):
        client = FakeClient(results=[FakeResult(error="nonce too old"),
                                     FakeResult(order_id="V2")])
        clock = Clock(step=7)                        # fresh ts every read
        ex = _exec(client=client, clock=clock)
        rows = await ex.execute_intents([_intent()], [], ACCT)
        self.assertEqual(rows[0]["event"], "scalper_exec_placed")
        self.assertEqual(rows[0]["order_id"], "V2")
        self.assertEqual(len(client.calls), 2)       # exactly one retry
        ids = {o["clOrdID"] for o in client.calls[0]["orders"]}
        ids2 = {o["clOrdID"] for o in client.calls[1]["orders"]}
        self.assertTrue(ids.isdisjoint(ids2))        # fresh nonce ids

    async def test_none_cfg_reads_disabled_never_raises(self):
        # Missing config = the getattr default binds: kill switch OFF —
        # the pre-build safe state (brain doctrine: missing cfg is never
        # an error, it is the dark path).
        client = FakeClient()
        ex = ScalperExecutor(None, client, None, Clock())
        rows = await ex.execute_intents([_intent_dict()], [], ACCT)
        self.assertEqual(client.calls, [])
        self.assertEqual(rows[0]["event"], "scalper_exec_dropped")
        self.assertEqual(rows[0]["reason"], "disabled")

    async def test_poisonous_intent_costs_itself_never_the_batch(self):
        class _Poison(dict):
            def get(self, *a, **k):
                raise RuntimeError("poison")
        client = FakeClient()
        ex = _exec(client=client)
        rows = await ex.execute_intents(
            [_Poison(), _intent_dict()], [], ACCT)
        self.assertEqual(rows[0]["event"], "scalper_exec_dropped")
        self.assertEqual(rows[0]["reason"], "internal_error")
        self.assertEqual(rows[1]["event"], "scalper_exec_placed")
        self.assertEqual(len(client.calls), 1)


# ── reconcile (pure startup-sync classification) ─────────────────────────

class TestReconcile(unittest.TestCase):

    def test_tags_scalper_vs_campaign(self):
        ex = _exec()
        positions = [
            {"symbol": "BTC-USD", "side": "long", "size": 0.001,
             "avgEntryPrice": 100.0, "cl_ord_id": "scalper_BTC-USD_123"},
            {"symbol": "ETH-USD", "side": "short", "size": 0.01,
             "avgEntryPrice": 50.0, "strategy_tag": "parasite_scalper"},
            {"symbol": "SOL-USD", "side": "long", "size": 1.0,
             "avgEntryPrice": 20.0, "cl_ord_id": "ant-SOL-USD-long-9",
             "strategy_tag": "campaign"},
        ]
        rows = ex.reconcile(positions)
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0]["scalper_owned"])
        self.assertEqual(rows[0]["basis"], "cl_ord_id")
        self.assertEqual(rows[0]["strategy_type"], STRATEGY_TYPE)
        self.assertTrue(rows[1]["scalper_owned"])
        self.assertEqual(rows[1]["basis"], "strategy_type")
        self.assertFalse(rows[2]["scalper_owned"])
        self.assertEqual(rows[2]["basis"], "")
        self.assertEqual(rows[2]["strategy_type"], "")

    def test_object_positions_and_garbage(self):
        ex = _exec()
        pos = SimpleNamespace(symbol="BTC-USD", side="long", size=0.5,
                              entry=100.0, clOrdID="scalper_BTC-USD_7")
        rows = ex.reconcile([pos, None, "garbage", {}])
        owned = [r for r in rows if r["scalper_owned"]]
        self.assertEqual(len(owned), 1)
        self.assertEqual(owned[0]["symbol"], "BTC-USD")
        self.assertEqual(ex.reconcile(None), [])


if __name__ == "__main__":
    unittest.main()
