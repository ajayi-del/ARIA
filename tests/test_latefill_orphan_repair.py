"""tests/test_latefill_orphan_repair.py — pins for the 2026-09-28 L1+L2
late-fill orphan repair (.claude/skills/late-fill-orphan-class.md).

The defect chain: anticipator evicts a resting order → venue cancel
CONFIRMED, fleet row popped (OrderSpec geometry destroyed), intent stamped
"rejected" → the cancel races a fill (cancel_hole_late_fill) →
reconciliation adopts SIZE ONLY → no stop, no ownership, and the operator
firewall misclassifies it as a manual position (live victim: ARB long
271.7 @0.22583, adopted 01:09Z 2026-09-28, rode to −31% ROE naked).

  L1  provenance registry + journal re-arm at the reconciliation adoption
      site — ownership + ATR protective stop + intent with
      provenance=anticipator_late_fill registered BEFORE the operator
      firewall classification reads.
  L2  stop-less adopted positions get synthesized stop geometry
      (max(2%, 1.0×ATR15/mark, venue min) from the fill mark) so the
      fork/budget RED pain-harvest can arm (no_stop fail-closed today).

Behavioral pins cover the three pure brains (_latefill_provenance,
_latefill_stop_geometry, _latefill_journal_provenance_row); the wiring is
source-pinned per the tests/test_anticipator_resilience.py convention.
Kill-switch off-state (latefill_provenance_repair_enabled False +
latefill_stop_synth_enabled False) must reproduce the pre-repair system
bit-for-bit.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = os.path.join(_REPO, "main.py")
_CONFIG_SRC = os.path.join(_REPO, "core", "config.py")

NOW = 1_800_000_000.0        # fixed epoch seconds
NOW_MS = NOW * 1000.0


def _main_src() -> str:
    with open(_MAIN_SRC) as fh:
        return fh.read()


def _config_src() -> str:
    with open(_CONFIG_SRC) as fh:
        return fh.read()


def _ev(side="buy", ts=NOW - 30.0, **over):
    row = {"side": side, "entry_id": "eid-1", "tag": "ant-x",
           "reason": "anticipator_evicted", "stop_price": 95.0,
           "tp_price": 110.0, "limit_price": 100.0,
           "margin_usd": 55.0, "ts": ts}
    row.update(over)
    return row


# ── _latefill_provenance ─────────────────────────────────────────────────

def test_provenance_long_buy_match():
    assert main._latefill_provenance("long", _ev(side="buy"), NOW, 900.0)


def test_provenance_short_sell_match():
    assert main._latefill_provenance(
        "short", _ev(side="sell"), NOW, 900.0)


def test_provenance_side_mismatch():
    assert not main._latefill_provenance("short", _ev(side="buy"), NOW, 900.0)
    assert not main._latefill_provenance("long", _ev(side="sell"), NOW, 900.0)


def test_provenance_expired_ttl():
    assert not main._latefill_provenance(
        "long", _ev(ts=NOW - 901.0), NOW, 900.0)


def test_provenance_none_and_bad_rows():
    assert not main._latefill_provenance("long", None, NOW, 900.0)
    assert not main._latefill_provenance("long", "junk", NOW, 900.0)
    assert not main._latefill_provenance("long", {}, NOW, 900.0)
    assert not main._latefill_provenance(
        "long", _ev(side="garbage"), NOW, 900.0)


def test_provenance_future_ts_rejected():
    assert not main._latefill_provenance(
        "long", _ev(ts=NOW + 60.0), NOW, 900.0)


# ── _latefill_stop_geometry ──────────────────────────────────────────────

def test_stop_geometry_long_floor_atr_dark():
    # ATR None (15m ruler dark at boot) → 2% floor binds, venue min 1.0%.
    stop = main._latefill_stop_geometry("long", 100.0, 100.0, None,
                                        min_stop_pct=1.0)
    assert abs(stop - 98.0) < 1e-9


def test_stop_geometry_short_atr_dominant():
    # ATR15/mark 3% > 2% floor → ATR distance, correct (above) side.
    stop = main._latefill_stop_geometry("short", 100.0, 100.0, 0.03,
                                        min_stop_pct=1.0)
    assert abs(stop - 103.0) < 1e-9


def test_stop_geometry_venue_min_dominant():
    # Venue min 2.5% (UNITREE class) outranks the 2% floor and a 1% ATR.
    stop = main._latefill_stop_geometry("long", 100.0, 100.0, 0.01,
                                        min_stop_pct=2.5)
    assert abs(stop - 97.5) < 1e-9


def test_stop_geometry_reference_moved_mark():
    # Long with mark already BELOW entry: ref = mark so the stop sits below
    # the live mark (the startup-sync protective-stop pattern).
    stop = main._latefill_stop_geometry("long", 100.0, 90.0, None,
                                        min_stop_pct=1.0)
    assert abs(stop - 90.0 * 0.98) < 1e-9
    # Short with mark already ABOVE entry: ref = mark, stop above it.
    stop = main._latefill_stop_geometry("short", 100.0, 110.0, None,
                                        min_stop_pct=1.0)
    assert abs(stop - 110.0 * 1.02) < 1e-9


def test_stop_geometry_degenerate_inputs():
    assert main._latefill_stop_geometry("long", 0.0, 100.0, None) == 0.0
    assert main._latefill_stop_geometry("flat", 100.0, 100.0, None) == 0.0
    assert main._latefill_stop_geometry("long", "x", 100.0, None) == 0.0


# ── _latefill_journal_provenance_row ─────────────────────────────────────

def _jrow(**over):
    row = {"symbol": "ARB-USD", "direction": "long", "approved": True,
           "strategy_tag": "anticipator", "outcome": "rejected",
           "exit_reason": "anticipator_evicted", "entry_id": "eid-9",
           "entry_price": 0.22583, "stop_price": 0.22, "tp1_price": 0.24,
           "initial_margin": 55.0, "timestamp_ms": NOW_MS - 120_000,
           "closed_at_ms": NOW_MS - 60_000}
    row.update(over)
    return row


def test_journal_row_hit_returns_registry_shape():
    got = main._latefill_journal_provenance_row(
        [_jrow()], "ARB-USD", "long", NOW_MS, 900.0)
    assert got is not None
    assert got["entry_id"] == "eid-9"
    assert got["side"] == "long"
    assert got["reason"] == "anticipator_evicted"
    assert got["tp_price"] == 0.24


def test_journal_row_open_outcome_ignored():
    assert main._latefill_journal_provenance_row(
        [_jrow(outcome="open")], "ARB-USD", "long", NOW_MS, 900.0) is None


def test_journal_row_stale_ignored():
    assert main._latefill_journal_provenance_row(
        [_jrow(closed_at_ms=NOW_MS - 901_000)],
        "ARB-USD", "long", NOW_MS, 900.0) is None


def test_journal_row_reason_symbol_side_filters():
    # Non-anticipator exit reason, wrong symbol, wrong side, wrong tag.
    assert main._latefill_journal_provenance_row(
        [_jrow(exit_reason="manual_close")],
        "ARB-USD", "long", NOW_MS, 900.0) is None
    assert main._latefill_journal_provenance_row(
        [_jrow(symbol="SOL-USD")], "ARB-USD", "long", NOW_MS, 900.0) is None
    assert main._latefill_journal_provenance_row(
        [_jrow()], "ARB-USD", "short", NOW_MS, 900.0) is None
    assert main._latefill_journal_provenance_row(
        [_jrow(strategy_tag="cascade")],
        "ARB-USD", "long", NOW_MS, 900.0) is None


def test_journal_row_all_three_eviction_reasons_match():
    for reason in ("anticipator_evicted", "anticipator_pruned",
                   "anticipator_dust"):
        got = main._latefill_journal_provenance_row(
            [_jrow(exit_reason=reason)], "ARB-USD", "long", NOW_MS, 900.0)
        assert got is not None, reason


# ── Source pins (wiring) ─────────────────────────────────────────────────

def test_registry_writes_at_all_three_pop_sites():
    src = _main_src()
    # Eviction, prune pass, dust sweep — each records provenance at the pop.
    assert src.count("_latefill_evictions[") >= 3
    assert '"reason": "anticipator_evicted"' in src
    assert '"reason": "anticipator_pruned"' in src
    assert '"reason": "anticipator_dust"' in src


def test_l1_splice_precedes_mid_session_firewall():
    src = _main_src()
    prov = src.index("_lf_prov = None")
    # The firewall call AFTER the provenance splice (the boot call site is
    # earlier in the file and must not satisfy this pin).
    fw = src.index("if _operator_long_firewall_verdict(", prov)
    assert prov < fw
    # The intent re-arm (journal flip + intent-cache write) lands between
    # the provenance check and the classification read.
    rearm = src.index("_operator_intent_cache[sym] = (", prov)
    assert prov < rearm < fw


def test_telemetry_events_present():
    src = _main_src()
    assert '"latefill_orphan_repaired"' in src
    assert '"latefill_stop_synthesized"' in src
    assert '"latefill_repair_failed"' in src
    assert '"latefill_stop_placed"' in src
    assert 'provenance="anticipator_late_fill"' in src


def test_dust_check_latefill_exempt():
    src = _main_src()
    dust = src.index('reconciliation_dust_skipped')
    guard = src.rindex("if (size * entry_px < config.min_trade_notional_usd",
                       0, dust)
    snippet = src[guard:dust]
    assert "_lf_prov is None" in snippet


def test_l2_splice_at_startup_sync():
    src = _main_src()
    assert 'source="startup_sync"' in src
    l2 = src.index("latefill_stop_synthesized")
    native = src.index("_place_startup_stop", l2)
    # L2 geometry lands BEFORE the native protective-stop task (the native
    # stop overwrites on success — tighten-only ordering).
    assert l2 < native


def test_knobs_in_config_with_defaults():
    src = _config_src()
    assert "latefill_provenance_repair_enabled: bool = True" in src
    assert "latefill_stop_synth_enabled: bool = True" in src
    assert "latefill_eviction_ttl_s: float = 900.0" in src


def test_kill_switch_gates_read_sides():
    src = _main_src()
    # L1 read-side gate (registry ignored when False → pre-repair firewall).
    assert '"latefill_provenance_repair_enabled"' in src
    # L2 read-side gate (no synth when False → legacy stop_price 0.0).
    assert '"latefill_stop_synth_enabled"' in src
