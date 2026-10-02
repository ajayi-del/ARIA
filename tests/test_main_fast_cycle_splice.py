"""tests/test_main_fast_cycle_splice.py — pins for the M1 fast-cycle splice
(2026-09-26, splice 1 of 2: main.py wiring of the $40 fast_cycle pool).

Conventions follow tests/test_live_offense_wiring.py: behavioral pins on the
zero-I/O brains (FastCycleEngine / VolumeLedger — the parts that can be
booted without main()) plus source pins on main.py for the closure wiring
(the parts that cannot). Every kill-switch off-state must reproduce the
pre-splice system bit-for-bit.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = os.path.join(_REPO, "main.py")


def _main_src() -> str:
    with open(_MAIN_SRC) as fh:
        return fh.read()


def _cfg(**over):
    base = dict(
        fast_cycle_enabled=True,
        fast_cycle_fee_budget_per_100k=7.00,
        fast_cycle_cage_min=3.0,
        fast_cycle_maker_fee_rate=0.000114,
        fast_cycle_taker_fee_rate=0.00038,
        fast_cycle_stop_fee_floor_mult=3.0,
        fast_cycle_fee_min_volume_usd=1000.0,
        fast_cycle_max_concurrent=6,
        fast_cycle_margin_per_trade=8.0,
        fast_cycle_pool_usd=40.0,
        fast_cycle_taker_exit_frac=0.75,
    )
    base.update(over)
    return SimpleNamespace(**base)


# ── (a) Kill switch False → no new code paths execute ────────────────────────

class TestKillSwitchOff:
    def test_engine_none_when_flag_false(self):
        src = _main_src()
        assert ("_fast_cycle = (_FastCycleEngine()\n"
                "                   if bool(getattr(config, \"fast_cycle_enabled\", False))\n"
                "                   else None)") in src

    def test_loop_self_gates_and_returns(self):
        src = _main_src()
        assert "if _fast_cycle is None or not bool(" in src

    def test_volume_ledger_none_when_flag_false(self):
        src = _main_src()
        assert "_volume_ledger = None" in src
        assert 'if bool(getattr(config, "volume_engine_enabled", False)):' in src

    def test_engine_verdict_disabled_mutates_nothing(self):
        from intelligence.fast_cycle_engine import FastCycleEngine
        eng = FastCycleEngine()
        v = eng.entry_verdict(
            _cfg(fast_cycle_enabled=False), symbol="SOL-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=[], now_ts=0.0)
        assert v.action == "standdown" and v.reason == "disabled"
        assert eng.debited == 0.0  # read-only contract: zero mutation

    def test_main_parses(self):
        ast.parse(_main_src())


# ── (b) ant- fill → on_entry(margin=8.0) + personality CAMPAIGN ─────────────

class TestEntryFillAttribution:
    def test_engine_debits_margin_once(self):
        from intelligence.fast_cycle_engine import FastCycleEngine
        eng = FastCycleEngine()
        eng.on_entry("SOL-USD", 8.0)
        assert eng.debited == 8.0

    def test_fill_site_stamps_pool_and_personality(self):
        src = _main_src()
        assert '_tgt_fc.pool = "fast_cycle"' in src
        assert '_tgt_fc.entry_personality = "CAMPAIGN"' in src
        assert "_fast_cycle.on_entry(sym, _fc_margin)" in src

    def test_fill_site_opens_journal_intent(self):
        src = _main_src()
        assert 'outcome="open")' in src

    def test_default_margin_from_config(self):
        # The config is the code of record; the splice reads every dollar
        # figure from cfg via getattr, never hardcodes. Re-encoded
        # 2026-10-01 for the Governor's critical resize (55→30, "work with
        # a 150 usd budget").
        from core.config import Settings
        assert Settings().fast_cycle_margin_per_trade == 30.0

    def test_boot_stamp_marks_campaign_positions(self):
        src = _main_src()
        assert '_tgt.pool = "fast_cycle"' in src
        assert '_tgt.entry_personality = "CAMPAIGN"' in src


# ── (c) ECS exemption: record_trade skipped for ant- fills only ─────────────

class TestECSExemption:
    def test_record_trade_guarded(self):
        src = _main_src()
        assert "if not _is_fc_close:\n                ecs_engine.record_trade(" in src

    def test_record_trade_still_called_for_non_ant(self):
        # Exactly one call site, preserved for the main book.
        src = _main_src()
        assert src.count("ecs_engine.record_trade(") == 1


# ── (d) Duplicate-owner guard skips placement ────────────────────────────────

class TestDuplicateOwnerGuard:
    def test_pending_entry_guard(self):
        src = _main_src()
        assert ("if _sym in _pending_entry_symbols:\n"
                "                            continue") in src

    def test_live_position_guard(self):
        src = _main_src()
        assert ("if position_manager.get(_sym):\n"
                "                            continue") in src

    def test_engine_concurrency_counts_only_fast_cycle_pool(self):
        # A main-book position (no pool attr) must not consume campaign
        # concurrency slots; a fast_cycle position must.
        from intelligence.fast_cycle_engine import FastCycleEngine
        eng = FastCycleEngine()
        main_book = [SimpleNamespace() for _ in range(10)]
        v = eng.entry_verdict(
            _cfg(), symbol="SOL-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=main_book, now_ts=0.0)
        assert v.action == "approve"
        fc_book = [SimpleNamespace(pool="fast_cycle") for _ in range(6)]
        v2 = eng.entry_verdict(
            _cfg(), symbol="SOL-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=fc_book, now_ts=0.0)
        assert v2.action == "standdown" and v2.reason == "concurrency_cap"


# ── (e) Prune path: journal rejected on confirmed cancel; R7 documented ─────

class TestPrunePath:
    def test_prune_journals_rejected_after_cancel(self):
        src = _main_src()
        assert 'exit_reason="anticipator_pruned")' in src
        assert 'outcome="rejected", pnl_usd=0.0,' in src

    def test_eviction_journals_rejected(self):
        src = _main_src()
        assert '"anticipator_evicted"' in src

    def test_r7_kant_slot_documented_absent(self):
        # The ant- path never consumes a Kant slot (no _exec_guardian
        # reservation) — documented in the loop header; nothing to release.
        src = _main_src()
        assert "never consumes a Kant slot" in src


# ── (f) VolumeLedger pool string is exactly "fast_cycle" ────────────────────

class TestPoolString:
    def test_record_fill_pool_literal(self):
        src = _main_src()
        assert src.count('pool="fast_cycle"') == 2  # entry leg + exit leg

    def test_engine_pool_name(self):
        from intelligence.fast_cycle_engine import POOL_NAME
        assert POOL_NAME == "fast_cycle"

    def test_ledger_buckets_fast_cycle(self, tmp_path):
        from intelligence.volume_engine import VolumeLedger
        vl = VolumeLedger(ledger_path=str(tmp_path / "vl.jsonl"),
                          snapshot_path=str(tmp_path / "vg.json"))
        ok = vl.record_fill(ts=1000.0, symbol="SOL-USD", side="long",
                            notional_usd=304.0, fee_usd=0.0347,
                            pool="fast_cycle", realized_pnl_usd=0.0,
                            maker=True)
        assert ok
        bucket = vl._pools.get("fast_cycle")
        assert bucket is not None
        assert bucket["volume"] == 304.0
        assert bucket["fees"] == 0.0347

    def test_restore_counters_reads_pool_bucket(self, tmp_path):
        # Boot path: engine counters restore from the persisted bucket so
        # the fee governor survives restarts (cross-review P0).
        from intelligence.fast_cycle_engine import FastCycleEngine
        from intelligence.volume_engine import VolumeLedger
        vl = VolumeLedger(ledger_path=str(tmp_path / "vl.jsonl"),
                          snapshot_path=str(tmp_path / "vg.json"))
        vl.record_fill(ts=1000.0, symbol="SOL-USD", side="long",
                       notional_usd=304.0, fee_usd=0.0347,
                       pool="fast_cycle", realized_pnl_usd=0.0, maker=True)
        vl.record_fill(ts=1001.0, symbol="SOL-USD", side="long",
                       notional_usd=304.0, fee_usd=0.1155,
                       pool="fast_cycle", realized_pnl_usd=-2.0, maker=False)
        bucket = vl._pools["fast_cycle"]
        eng = FastCycleEngine()
        eng.restore_counters(volume_usd=bucket["volume"],
                             fees_usd=bucket["fees"],
                             realized_pnl_usd=bucket["realized_pnl"])
        gauge = eng.fee_gauge(_cfg())
        assert gauge["cumulative_volume_usd"] == 608.0
        assert gauge["cumulative_realized_pnl_usd"] == -2.0
        # Raw ratio: (0.1502 − (−2.0)) × 100k / 608 = 353.65 > 7.00 — a tiny
        # denominator prints a huge ratio. The gauge still REPORTS the raw
        # ratio in net_cost_per_100k, but as of 2026-10-02 budget_ok honors
        # the same abstain floor as the verdict path (the $608 sample is
        # noise-grade; live wound: a $1,598 window printed 226/100k and
        # self-locked the fleet for up to 7d).
        assert gauge["net_cost_per_100k"] > 7.00
        assert gauge["budget_ok"] is True
        v = eng.entry_verdict(
            _cfg(), symbol="SOL-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=[], now_ts=0.0)
        assert v.action == "approve"  # min-volume abstain holds
        # Above the floor the same ratio stands the campaign down.
        eng.restore_counters(volume_usd=200000.0, fees_usd=20.0,
                             realized_pnl_usd=0.0)
        v2 = eng.entry_verdict(
            _cfg(), symbol="SOL-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=[], now_ts=0.0)
        assert v2.action == "standdown" and v2.reason == "fee_budget_breach"


# ── Wiring: boot rebuild, immune-purge exemption, R1/R9 close path ──────────

class TestWiring:
    def test_loop_defined_and_registered(self):
        src = _main_src()
        assert "async def _anticipator_loop" in src
        assert '_supervise(_anticipator_loop, "anticipator")' in src

    def test_boot_rebuild_from_venue_orders(self):
        src = _main_src()
        assert 'if not _tag.startswith("ant-"):' in src
        assert "anticipator_fleet_rebuilt" in src
        assert "_fast_cycle.rebuild(_fc_boot_margins)" in src
        assert "_fast_cycle.restore_counters(" in src

    def test_immune_purge_exempts_ant_tags(self):
        src = _main_src()
        # Re-encoded 2026-09-26 (M2): the exemption grew to the tuple
        # ("ant-", "xpr-") — cross-side probes share the designed-long-lived
        # contract and the prune pass owns their lifecycle too. The ant-
        # exemption itself is unchanged (same `continue`, same flag gate).
        assert '.startswith(\n                                ("ant-", "xpr-"))):\n                        continue' in src

    def test_intent_journaled_at_placement(self):
        src = _main_src()
        assert 'strategy_tag="anticipator"' in src
        assert 'personality="CAMPAIGN")' in src

    def test_r1_close_personality_forced(self):
        src = _main_src()
        assert 'if _is_fc_close:\n                    _pers_close = "CAMPAIGN"' in src
        assert 'if _is_fc_close:\n                _agent_name = "CAMPAIGN"' in src

    def test_r9_comment_and_unconditional_chancellor(self):
        src = _main_src()
        assert ("# R9: campaign PnL counts toward daily-loss veto "
                "(Governor review") in src
        # The chancellor feed must NOT be gated on _is_fc_close.
        assert "if not _is_fc_close:\n                chancellor.record_close" not in src

    def test_close_accounting_contract(self):
        # GROSS pnl + both-leg fees + 2x volume is the on_close contract;
        # the splice passes _pnl_gross_total, never net.
        src = _main_src()
        assert "_fast_cycle.on_close(\n                    sym, _fc_margin_close, _pnl_gross_total," in src

    def test_flag_defaults(self):
        from core.config import Settings
        s = Settings()
        assert s.fast_cycle_enabled is True
        assert s.anticipator_enabled is True
        assert s.volume_engine_enabled is True
        assert s.fast_cycle_pool_usd == 150.0   # Governor 2026-10-01 critical resize: 300→150 ("work with a 150 usd budget")
        assert s.fast_cycle_max_concurrent == 6


# ── (g) Windowed fee governor (2026-10-02 Governor directive) ───────────────
# "aria has been down becuase of capital constraint" — the lifetime net-cost
# ratchet ($89.09/100k from the pre-09-28 conveyor fleet's −$21.44) stood
# down all 8,027 candidates on 2026-10-01. Rolling window + epoch anchor +
# kill switch (window_s <= 0 = legacy lifetime bit-for-bit).

class TestWindowedFeeGovernor:
    def test_config_knob_defaults(self):
        from core.config import Settings
        s = Settings()
        assert s.fast_cycle_fee_governor_window_s == 604800.0
        assert s.fast_cycle_fee_epoch_ts == 1790899200.0  # 2026-10-02T00:00Z

    def test_seed_counts_inside_window(self):
        from intelligence.fast_cycle_engine import FastCycleEngine
        eng = FastCycleEngine()
        eng.restore_counters_windowed(
            volume_usd=37264.63, fees_usd=11.759,
            realized_pnl_usd=-21.4385, window_s=604800.0, now_ts=100000.0)
        v, f, p = eng._windowed_totals(100100.0)
        assert (v, f, p) == (37264.63, 11.759, -21.4385)

    def test_seed_expires_after_window(self):
        from intelligence.fast_cycle_engine import FastCycleEngine
        eng = FastCycleEngine()
        eng.restore_counters_windowed(
            volume_usd=37264.63, fees_usd=11.759,
            realized_pnl_usd=-21.4385, window_s=604800.0, now_ts=100000.0)
        v, f, p = eng._windowed_totals(100000.0 + 604800.0 + 1.0)
        assert (v, f, p) == (0.0, 0.0, 0.0)

    def test_on_close_appends_window_row(self):
        from intelligence.fast_cycle_engine import FastCycleEngine
        eng = FastCycleEngine()
        eng.restore_counters_windowed(
            volume_usd=0.0, fees_usd=0.0, realized_pnl_usd=0.0,
            window_s=604800.0, now_ts=100000.0)
        eng.on_close("SOL-USD", 30.0, 1.5, 0.10, 600.0, now_ts=100100.0)
        v, f, p = eng._windowed_totals(100200.0)
        assert v == 1200.0 and f == 0.10 and p == 1.5
        # Row pruned once it falls out of the window.
        v2, f2, p2 = eng._windowed_totals(100100.0 + 604800.0 + 1.0)
        assert (v2, f2, p2) == (0.0, 0.0, 0.0)

    def test_dead_era_cannot_lock_the_fleet(self):
        # The down-day state: lifetime bucket breached (net cost 100/100k vs
        # budget 7). In-window the standdown binds; past the window the
        # governor abstains on an empty sample and the campaign approves.
        from intelligence.fast_cycle_engine import FastCycleEngine
        eng = FastCycleEngine()
        eng.restore_counters_windowed(
            volume_usd=20000.0, fees_usd=20.0, realized_pnl_usd=0.0,
            window_s=604800.0, now_ts=100000.0)
        cfg = _cfg(fast_cycle_fee_budget_per_100k=7.00)
        v1 = eng.entry_verdict(
            cfg, symbol="SOL-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=[], now_ts=100000.0 + 86400.0)
        assert v1.action == "standdown" and v1.reason == "fee_budget_breach"
        v2 = eng.entry_verdict(
            cfg, symbol="SOL-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=[], now_ts=100000.0 + 604800.0 + 1.0)
        assert v2.action == "approve"

    def test_boundary_strictly_greater(self):
        # net cost exactly == budget is OK (multiply-before-divide keeps the
        # IEEE boundary exact: 14*100000/200000 == 7.0).
        from intelligence.fast_cycle_engine import FastCycleEngine
        eng = FastCycleEngine()
        eng.restore_counters_windowed(
            volume_usd=200000.0, fees_usd=14.0, realized_pnl_usd=0.0,
            window_s=604800.0, now_ts=100000.0)
        cfg = _cfg(fast_cycle_fee_budget_per_100k=7.00)
        v = eng.entry_verdict(
            cfg, symbol="SOL-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=[], now_ts=100100.0)
        assert v.action == "approve"
        g = eng.fee_gauge(cfg, now_ts=100100.0)
        assert g["net_cost_per_100k"] == 7.0 and g["budget_ok"] is True
        assert g["window_volume_usd"] == 200000.0

    def test_kill_switch_window_zero_legacy_bit_for_bit(self):
        # window_s == 0: restore_counters path, lifetime counters, no window
        # fields on the gauge — pre-module behavior exactly.
        from intelligence.fast_cycle_engine import FastCycleEngine
        eng = FastCycleEngine()
        assert eng._window_s == 0.0
        eng.restore_counters(volume_usd=20000.0, fees_usd=20.0,
                             realized_pnl_usd=0.0)
        cfg = _cfg(fast_cycle_fee_budget_per_100k=7.00)
        v = eng.entry_verdict(
            cfg, symbol="SOL-USD", side="long",
            entry_price=100.0, stop_price=99.0, tp_price=104.0,
            open_positions=[], now_ts=9_999_999_999.0)
        assert v.action == "standdown" and v.reason == "fee_budget_breach"
        g = eng.fee_gauge(cfg, now_ts=9_999_999_999.0)
        assert "window_s" not in g

    def test_pool_window_sums_epoch_floor(self, tmp_path):
        # Epoch excludes rows at/before the anchor even inside the window;
        # cutoff is strict (row AT the anchor is excluded).
        from intelligence.volume_engine import VolumeLedger
        vl = VolumeLedger(ledger_path=str(tmp_path / "vl.jsonl"),
                          snapshot_path=str(tmp_path / "vg.json"))
        vl.record_fill(ts=1000.0, symbol="SOL-USD", side="long",
                       notional_usd=500.0, fee_usd=0.05,
                       pool="fast_cycle", realized_pnl_usd=-1.0, maker=True)
        vl.record_fill(ts=2000.0, symbol="SOL-USD", side="long",
                       notional_usd=700.0, fee_usd=0.07,
                       pool="fast_cycle", realized_pnl_usd=-2.0, maker=True)
        vl.record_fill(ts=3000.0, symbol="SOL-USD", side="long",
                       notional_usd=900.0, fee_usd=0.09,
                       pool="fast_cycle", realized_pnl_usd=3.0, maker=True)
        s = vl.pool_window_sums("fast_cycle", 4000.0, 604800.0, 2000.0)
        assert s == {"volume": 900.0, "fees": 0.09, "realized_pnl": 3.0}
        # span <= 0 = lifetime still epoch-floored.
        s2 = vl.pool_window_sums("fast_cycle", 4000.0, 0.0, 2000.0)
        assert s2 == {"volume": 900.0, "fees": 0.09, "realized_pnl": 3.0}
        # No epoch: both in-window rows count.
        s3 = vl.pool_window_sums("fast_cycle", 4000.0, 604800.0, 0.0)
        assert s3["volume"] == 2100.0
        assert abs(s3["fees"] - 0.21) < 1e-9
        assert abs(s3["realized_pnl"]) < 1e-9

    def test_main_source_pins(self):
        src = _main_src()
        assert "_fast_cycle.restore_counters_windowed(" in src
        assert '_volume_ledger.pool_window_sums(\n                        "fast_cycle"' in src
        assert '"fast_cycle_fee_governor_window_s", 0.0) or 0.0)' in src
        assert '"fast_cycle_fee_epoch_ts", 0.0) or 0.0)' in src
        assert ("_fc_notional_close, now_ts=time.time())" in src)


# ── (b) SoDEX membership: router semantics, not the explicit registry ────────
# 2026-09-27 campaign-killer fix: venue.symbols_for("sodex") scans the
# explicit _venue_by_symbol registry, which is ONLY populated by
# assign_symbols calls for aster/bybit — SoDEX is the implicit default
# (venue.py _DEFAULT_VENUE), so the registry read returned [] and the
# anticipator eligibility filter rejected every campaign symbol every 60s
# tick (zero compass verdicts, zero placements, zero errors). The filter
# must ask the ROUTER (venue_for) over config.assets instead.

class TestSodexOwnershipSemantics:
    def test_unregistered_symbol_defaults_to_sodex(self):
        from execution import venue
        # assign_symbols only registers when the venue has an executor.
        venue.register_executor("aster", object())
        venue.assign_symbols(["KAITO-USD"], "aster")
        try:
            assert venue.venue_for("BTC-USD") == "sodex"
            assert venue.venue_for("KAITO-USD") == "aster"
            # The old code read the explicit registry — SoDEX set is empty
            # by construction. This pin documents WHY that read was wrong.
            assert "BTC-USD" not in venue.symbols_for("sodex")
        finally:
            venue._venue_by_symbol.pop("KAITO-USD", None)
            venue._executors.pop("aster", None)

    def test_main_asks_router_not_registry(self):
        src = _main_src()
        assert ("_sodex_owned = {\n"
                "                        _s for _s in config.assets\n"
                "                        if venue.venue_for(_s) == \"sodex\"}") in src
        assert '_sodex_owned = set(venue.symbols_for("sodex"))' not in src
