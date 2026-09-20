"""Pins for the #70 adoption opened_at re-anchor (CEO DIR 2026-09-20).

Boot-adopted positions were getting opened_at_ms = now at a 100% rate — and
that field is a LIVE RISK INPUT (time-stop, treasury grace/recycle). The
resolver restores the true entry instant from, in precedence order: venue
payload -> persisted age ledger -> journal open row -> plane-ledger
attempt_id -> now. Every adoption emits whether an anchor was found.
"""
import importlib

import main as m


NOW = 1_790_000_000_000  # fixed "boot" instant (ms)


def _journal_row(symbol, direction, ts, outcome="open", closed=None):
    r = {"symbol": symbol, "direction": direction, "timestamp_ms": ts,
         "outcome": outcome}
    if closed:
        r["closed_at_ms"] = closed
    return r


def _plane_row(symbol, opened_ms, side=None):
    r = {"attempt_id": f"{symbol}_{opened_ms}"}
    if side:
        r["identity"] = {"side": side}
    return r


def test_module_loads_and_kill_switch_default_on():
    importlib.reload(m)
    assert m.ADOPTION_REANCHOR_ENABLED is True
    assert callable(m.resolve_adopted_opened_at_ms)


def test_venue_payload_wins_over_everything():
    ms, src = m.resolve_adopted_opened_at_ms(
        "SOL-USD", "long", 111,
        {"SOL-USD|long": {"opened_at_ms": 222}},
        [_journal_row("SOL-USD", "long", 333)],
        [_plane_row("SOL-USD", 444)], NOW)
    assert (ms, src) == (111, "venue")


def test_age_ledger_leg_used_when_no_venue():
    ms, src = m.resolve_adopted_opened_at_ms(
        "SOL-USD", "long", None,
        {"SOL-USD|long": {"opened_at_ms": NOW - 3_600_000}},
        [_journal_row("SOL-USD", "long", NOW - 7_200_000)],
        [], NOW)
    assert (ms, src) == (NOW - 3_600_000, "age_ledger")


def test_journal_leg_picks_latest_open_row_matching_side():
    rows = [
        _journal_row("ETH-USD", "long", NOW - 90_000),
        _journal_row("ETH-USD", "long", NOW - 50_000),           # latest open
        _journal_row("ETH-USD", "short", NOW - 10_000),          # wrong side
        _journal_row("ETH-USD", "long", NOW - 5_000, outcome="rejected"),
        _journal_row("ETH-USD", "long", NOW - 1_000, closed=NOW),  # closed
    ]
    ms, src = m.resolve_adopted_opened_at_ms(
        "ETH-USD", "long", None, {}, rows, [], NOW)
    assert (ms, src) == (NOW - 50_000, "journal")


def test_journal_row_without_direction_still_matches():
    rows = [{"symbol": "XMR-USD", "timestamp_ms": NOW - 60_000,
             "outcome": "open"}]
    ms, src = m.resolve_adopted_opened_at_ms(
        "XMR-USD", "short", None, {}, rows, [], NOW)
    assert (ms, src) == (NOW - 60_000, "journal")


def test_plane_ledger_leg_parses_attempt_id():
    rows = [
        {"attempt_id": "AVAX-USD_notanumber"},
        _plane_row("AVAX-USD", NOW - 80_000, side="long"),
        _plane_row("AVAX-USD", NOW - 30_000, side="short"),  # wrong side
        _plane_row("BTC-USD", NOW - 10_000, side="long"),    # wrong symbol
    ]
    ms, src = m.resolve_adopted_opened_at_ms(
        "AVAX-USD", "long", None, {}, [], rows, NOW)
    assert (ms, src) == (NOW - 80_000, "plane_ledger")


def test_fallback_now_when_no_anchor_anywhere():
    ms, src = m.resolve_adopted_opened_at_ms(
        "DOS-USD", "short", None, {}, [], [], NOW)
    assert (ms, src) == (NOW, "fallback_now")


def test_stale_ledger_entry_distrusted():
    old = NOW - (8 * 86400_000)  # beyond the 7d bound
    ms, src = m.resolve_adopted_opened_at_ms(
        "SOL-USD", "long", None,
        {"SOL-USD|long": {"opened_at_ms": old}},
        [_journal_row("SOL-USD", "long", NOW - 40_000)], [], NOW)
    assert (ms, src) == (NOW - 40_000, "journal")


def test_future_ledger_entry_distrusted():
    ms, src = m.resolve_adopted_opened_at_ms(
        "SOL-USD", "long", None,
        {"SOL-USD|long": {"opened_at_ms": NOW + 60_000}},
        [], [], NOW)
    assert (ms, src) == (NOW, "fallback_now")


def test_malformed_ledger_and_rows_tolerated():
    ledger = {"SOL-USD|long": "garbage", "SOL-USD|short": {"opened_at_ms": "x"}}
    rows = ["not-a-dict", {"symbol": "SOL-USD", "outcome": "open",
                           "timestamp_ms": "bad"}]
    ms, src = m.resolve_adopted_opened_at_ms(
        "SOL-USD", "long", None, ledger, rows, ["bad"], NOW)
    assert (ms, src) == (NOW, "fallback_now")


def test_ledger_key_is_side_scoped():
    ms, src = m.resolve_adopted_opened_at_ms(
        "SOL-USD", "short", None,
        {"SOL-USD|long": {"opened_at_ms": NOW - 5_000}},
        [], [], NOW)
    assert (ms, src) == (NOW, "fallback_now")


def test_none_planes_tolerated():
    ms, src = m.resolve_adopted_opened_at_ms(
        "SOL-USD", "long", None, None, None, None, NOW)
    assert (ms, src) == (NOW, "fallback_now")
