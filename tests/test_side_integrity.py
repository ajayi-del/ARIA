"""Side-integrity schema acceptance pins (CEO P0, 2026-10-24).

Covers the SI-A..SI-G repair rows against intelligence/side_integrity.py,
the si_detach_execute executor + side_integrity_enabled kill switch in
main.py, the additive side= filter on TradeJournal.find_open_entry_in_files,
and the legacy classify_size_sync matrix (must stay bit-for-bit).
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as m  # noqa: E402
from intelligence import side_integrity as si  # noqa: E402
from memory.trade_journal import TradeJournal  # noqa: E402
from risk.position_manager import PositionManager  # noqa: E402
from execution.schemas import Position  # noqa: E402

NOW_MS = 1_800_000_000_000
TODAY = datetime.fromtimestamp(NOW_MS / 1000, timezone.utc).date()
TODAY_S = TODAY.isoformat()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()


def _pos(symbol="BTC-USD", side="long", size=0.01, entry=100.0,
         opened_at_ms=NOW_MS - 60_000, venue="sodex"):
    p = Position(
        symbol=symbol, side=side, size=size, entry_price=entry,
        stop_price=entry * 0.99, tp1_price=entry * 1.01,
        tp2_price=entry * 1.02, tp3_price=entry * 1.03,
        liq_price=0.0, initial_margin=entry * size / 5,
        leverage=5, opened_at_ms=opened_at_ms,
    )
    p.venue = venue  # additive attribution tag — not a dataclass field
    return p


# ── SI-1: ONE parser — unsigned/directionless is UNKNOWN, never long ────────

class TestParsePositionSide:
    def test_explicit_long_tokens(self):
        for raw in ("long", "LONG", "Buy", "buy", "1", 1):
            assert si.parse_position_side({"side": raw}) == si.SIDE_LONG

    def test_explicit_short_tokens(self):
        for raw in ("short", "SHORT", "Sell", "sell", "-1", "2", 2):
            assert si.parse_position_side({"side": raw}) == si.SIDE_SHORT

    def test_direction_and_positionside_fields(self):
        assert si.parse_position_side({"direction": "short"}) == si.SIDE_SHORT
        assert si.parse_position_side({"positionSide": "LONG"}) == si.SIDE_LONG

    def test_signed_qty_infers_side(self):
        assert si.parse_position_side({"size": -0.5}) == si.SIDE_SHORT
        assert si.parse_position_side({"size": 0.5}) == si.SIDE_LONG
        assert si.parse_position_side({"positionAmt": "-2.5"}) == si.SIDE_SHORT

    def test_unsigned_directionless_is_unknown_never_long(self):
        # The SoDEX one-way wound: unsigned size with no side field must
        # NOT default long.
        assert si.parse_position_side({"size": 0.0}) == si.SIDE_UNKNOWN
        assert si.parse_position_side({}) == si.SIDE_UNKNOWN
        assert si.parse_position_side({"side": ""}) == si.SIDE_UNKNOWN
        assert si.parse_position_side({"side": "flat"}) == si.SIDE_UNKNOWN

    def test_malformed_is_unknown(self):
        assert si.parse_position_side("not-a-dict") == si.SIDE_UNKNOWN
        assert si.parse_position_side(None) == si.SIDE_UNKNOWN
        assert si.parse_position_side({"size": "abc"}) == si.SIDE_UNKNOWN


# ── SI-A: side/venue verdict ────────────────────────────────────────────────

class TestSideVenueVerdict:
    def test_same_side(self):
        row = {"symbol": "BTC-USD", "side": "long", "size": 0.01}
        assert si.side_venue_verdict("long", row, "sodex") == si.VERDICT_SAME_SIDE

    def test_side_conflict(self):
        row = {"symbol": "BTC-USD", "side": "short", "size": 0.01}
        assert si.side_venue_verdict("long", row, "sodex") == si.VERDICT_SIDE_CONFLICT

    def test_side_unknown_exchange(self):
        # Truly directionless: no side field AND no signed quantity.
        row = {"symbol": "BTC-USD"}
        assert si.side_venue_verdict("long", row, "sodex") == si.VERDICT_SIDE_UNKNOWN

    def test_signed_qty_fallback_is_side(self):
        # SI-1 contract: SoDEX one-way netting rows carry side as the SIGN
        # of size — positive = long, negative = short (never UNKNOWN, or
        # every unstamped SoDEX row would quarantine the reconcile path).
        assert si.side_venue_verdict(
            "long", {"symbol": "BTC-USD", "size": 0.01}, "sodex"
        ) == si.VERDICT_SAME_SIDE
        assert si.side_venue_verdict(
            "long", {"symbol": "BTC-USD", "size": -0.01}, "sodex"
        ) == si.VERDICT_SIDE_CONFLICT

    def test_side_unknown_tracked(self):
        row = {"symbol": "BTC-USD", "side": "long", "size": 0.01}
        assert si.side_venue_verdict("", row, "sodex") == si.VERDICT_SIDE_UNKNOWN
        assert si.side_venue_verdict("unknown", row, "sodex") == si.VERDICT_SIDE_UNKNOWN

    def test_duplicate_rows_are_venue_ambiguous(self):
        row = {"symbol": "BTC-USD", "side": "long", "size": 0.01}
        assert si.side_venue_verdict(
            "long", row, "sodex", duplicate=True) == si.VERDICT_VENUE_AMBIGUOUS

    def test_wrong_venue_stamp_is_venue_ambiguous(self):
        row = {"symbol": "BTC-USD", "side": "long", "size": 0.01, "venue": "aster"}
        assert si.side_venue_verdict("long", row, "sodex") == si.VERDICT_VENUE_AMBIGUOUS

    def test_unstamped_row_matches_routed_venue(self):
        # SoDEX rows are unstamped — no venue ambiguity.
        row = {"symbol": "BTC-USD", "side": "long", "size": 0.01}
        assert si.side_venue_verdict("long", row, "sodex") == si.VERDICT_SAME_SIDE

    def test_non_crypto_included(self):
        # Equity perp rows get the same quarantine — no crypto-only carve-out.
        row = {"symbol": "SPY-USD"}  # directionless equity row
        assert si.side_venue_verdict("long", row, "sodex") == si.VERDICT_SIDE_UNKNOWN
        row2 = {"symbol": "SPY-USD", "side": "short", "size": 1.5}
        assert si.side_venue_verdict("long", row2, "sodex") == si.VERDICT_SIDE_CONFLICT


# ── SI-D/SI-6: THE single structural verifier ───────────────────────────────

class TestVerifyAction:
    def test_failed_poll_quarantines(self):
        out = si.verify_action(False, None, "sodex", "long")
        assert out[0] == si.V_QUARANTINE and out[3] == "fetch_failed"

    def test_flat_confirmed(self):
        out = si.verify_action(True, [], "sodex", "long")
        assert out[0] == si.V_FLAT

    def test_zero_qty_row_is_flat(self):
        out = si.verify_action(True, [{"side": "long", "size": 0.0}], "sodex", "long")
        assert out[0] == si.V_FLAT

    def test_opposite_side_equal_detaches(self):
        row = {"side": "short", "size": 0.01}
        out = si.verify_action(True, [row], "sodex", "long")
        assert out[0] == si.V_DETACH and out[1] == si.SIDE_SHORT
        assert out[2] == 0.01

    def test_opposite_side_larger_detaches(self):
        row = {"side": "short", "size": 0.05}
        out = si.verify_action(True, [row], "sodex", "long")
        assert out[0] == si.V_DETACH and out[2] == 0.05

    def test_opposite_side_smaller_detaches(self):
        row = {"side": "short", "size": 0.001}
        out = si.verify_action(True, [row], "sodex", "long")
        assert out[0] == si.V_DETACH and out[2] == 0.001

    def test_opposite_side_dust_detaches(self):
        # Even structurally-unclosable dust on the opposite side is a flip.
        row = {"side": "short", "size": 0.00005}
        out = si.verify_action(True, [row], "sodex", "long")
        assert out[0] == si.V_DETACH

    def test_same_side_ro_overshoot_refreshes_qty(self):
        # RO close rejected "would open" but the poll finds the SAME side
        # bigger — a partial-close race, not a flip: refresh fresh qty.
        row = {"side": "long", "size": 0.02}
        out = si.verify_action(True, [row], "sodex", "long")
        assert out[0] == si.V_REFRESH_QTY and out[2] == 0.02

    def test_unknown_side_quarantines(self):
        row = {"symbol": "BTC-USD"}  # no side field, no signed qty
        out = si.verify_action(True, [row], "sodex", "long")
        assert out[0] == si.V_QUARANTINE and out[3] == "unknown_side"

    def test_malformed_row_quarantines(self):
        row = {"side": "long", "size": "abc"}
        out = si.verify_action(True, [row], "sodex", "long")
        assert out[0] == si.V_QUARANTINE and out[3] == "malformed_row"

    def test_duplicate_rows_quarantine(self):
        rows = [{"side": "long", "size": 0.01}, {"side": "long", "size": 0.02}]
        out = si.verify_action(True, rows, "sodex", "long")
        assert out[0] == si.V_QUARANTINE and out[3] == "duplicate_rows"

    def test_wrong_venue_quarantines(self):
        row = {"side": "long", "size": 0.01, "venue": "bybit"}
        out = si.verify_action(True, [row], "sodex", "long")
        assert out[0] == si.V_QUARANTINE and out[3] == "wrong_venue"


class TestErrorStringNormalization:
    """All four structural error strings route through the ONE verifier (SI-6)."""

    def test_all_four_error_strings_classify(self):
        assert si.normalize_verify_error("quantity is invalid") == si.ERR_QTY_INVALID
        assert si.normalize_verify_error("position not found: 25492912645") == si.ERR_POSITION_NOT_FOUND
        assert si.normalize_verify_error("no position") == si.ERR_POSITION_NOT_FOUND
        assert si.normalize_verify_error(
            "reduce only order cannot open position") == si.ERR_RO_WOULD_OPEN

    def test_unknown_error_is_other(self):
        assert si.normalize_verify_error("insufficient margin") == si.ERR_OTHER
        assert si.normalize_verify_error("") == si.ERR_OTHER

    def test_every_class_flows_through_the_same_verdict_function(self):
        # Error text alone never books: whatever the class, the fresh poll
        # decides. Opposite-side poll → detach for ALL error classes.
        row = {"side": "short", "size": 0.01}
        for err in ("quantity is invalid", "position not found: 9",
                    "no position", "reduce only order cannot open position"):
            _cls = si.normalize_verify_error(err)
            out = si.verify_action(True, [row], "sodex", "long")
            assert out[0] == si.V_DETACH, err


# ── SI-3: generation binding ────────────────────────────────────────────────

class TestGenerationBinding:
    def test_same_generation_matches(self):
        p = _pos()
        assert si.generation_matches(si.capture_generation(p), p) is True

    def test_replacement_generation_never_matches(self):
        old = _pos(opened_at_ms=NOW_MS - 60_000)
        new = _pos(opened_at_ms=NOW_MS - 5_000)   # close + re-entry
        assert si.generation_matches(si.capture_generation(old), new) is False

    def test_mutated_opened_at_breaks_match(self):
        p = _pos()
        snap = si.capture_generation(p)
        p.opened_at_ms += 1
        assert si.generation_matches(snap, p) is False

    def test_mutated_side_breaks_match(self):
        p = _pos(side="long")
        snap = si.capture_generation(p)
        p.side = "short"
        assert si.generation_matches(snap, p) is False

    def test_none_expected_fails_closed(self):
        assert si.generation_matches(None, _pos()) is False
        assert si.generation_matches(si.capture_generation(_pos()), None) is False


# ── SI-E/SI-4: detach — ZERO accounting, ZERO learner calls ─────────────────

class _Rec:
    def __init__(self, raise_on=()):
        self.calls = []
        self.raise_on = set(raise_on)

    def fn(self, name):
        def _f(*a, **k):
            self.calls.append(name)
            if name in self.raise_on:
                raise RuntimeError(f"boom:{name}")
        return _f


class TestDetachZeroAccounting:
    def _run(self, rec, pm):
        p = _pos()
        pm.add(p)
        m.si_detach_execute(
            "BTC-USD", pm.get("BTC-USD")[0], live_side="short",
            reason="test:ro_would_open",
            cancel_resting=rec.fn("cancel_resting"),
            mfe_on_close=rec.fn("mfe_on_close"),
            spoils_on_close=rec.fn("spoils_on_close"),
            mfe_key_fn=lambda s, p: f"{s}:{p.opened_at_ms}",
            pop_entry_id=rec.fn("pop_entry_id"),
            clear_intent_cache=rec.fn("clear_intent_cache"),
            treasury_release=rec.fn("treasury_release"),
            scalper_release=rec.fn("scalper_release"),
            pos_manager=pm,
            log=SimpleNamespace(warning=lambda *a, **k: None,
                                info=lambda *a, **k: None))

    def test_detach_releases_everything_and_books_nothing(self):
        rec, pm = _Rec(), PositionManager()
        self._run(rec, pm)
        # Tracking removed.
        assert pm.get("BTC-USD") == []
        # All ownership planes released; entry id POPPED (never written).
        for name in ("cancel_resting", "mfe_on_close", "spoils_on_close",
                     "pop_entry_id", "clear_intent_cache",
                     "treasury_release", "scalper_release"):
            assert name in rec.calls, name
        # There is NO journal/trade_db/outcome/perf/daily/chancellor/cooldown
        # parameter on the executor at all — learners structurally unreachable.

    def test_detach_pnl_is_honest_zero_formula_untouched(self):
        # SI-7: position_manager.close at mark=entry_price → _calculate_pnl
        # returns exactly 0.0 via the UNCHANGED formula.
        p = _pos(side="long", size=0.01, entry=100.0)
        assert PositionManager._calculate_pnl(p, 100.0) == 0.0
        p2 = _pos(side="short", size=0.01, entry=100.0)
        assert PositionManager._calculate_pnl(p2, 100.0) == 0.0

    def test_detach_is_fail_open(self):
        rec, pm = _Rec(raise_on={"cancel_resting", "mfe_on_close",
                                 "pop_entry_id"}), PositionManager()
        self._run(rec, pm)   # must not raise
        assert pm.get("BTC-USD") == []


# ── SI-C: foreign grow deferral + registered-intent positive leg ────────────

class TestGrowAdoption:
    def test_foreign_grow_without_intent_defers(self):
        assert si.grow_adoption_allowed(False) is False

    def test_registered_intent_same_side_grow_still_adopts(self):
        # The late-fill / self-heal positive leg MUST keep adopting.
        assert si.grow_adoption_allowed(True) is True


# ── SI-G: bracket merge side assert ─────────────────────────────────────────

class TestBracketMergeSideGuard:
    def test_opposite_side_never_merges(self):
        # A conflict must never write fresh order IDs/stops onto the track.
        assert si.bracket_merge_side_ok("long", "short") is False
        assert si.bracket_merge_side_ok("short", "long") is False

    def test_same_side_merges(self):
        assert si.bracket_merge_side_ok("long", "long") is True
        assert si.bracket_merge_side_ok("short", "short") is True

    def test_unknown_side_fails_closed(self):
        assert si.bracket_merge_side_ok("unknown", "long") is False
        assert si.bracket_merge_side_ok("long", "") is False

    def test_kill_switch_off_is_legacy(self):
        assert si.bracket_merge_side_ok("long", "short", enabled=False) is True


# ── Kill switch: getattr(config, "side_integrity_guard_enabled", True) ──────

class TestKillSwitch:
    def test_default_on_when_attr_missing(self):
        assert m.side_integrity_enabled(SimpleNamespace()) is True

    def test_explicit_off(self):
        assert m.side_integrity_enabled(
            SimpleNamespace(side_integrity_guard_enabled=False)) is False

    def test_explicit_on(self):
        assert m.side_integrity_enabled(
            SimpleNamespace(side_integrity_guard_enabled=True)) is True


# ── SI-F: journal intent side matching ──────────────────────────────────────

class TestJournalIntentSide:
    def test_entry_side_prefers_additive_side_field(self):
        assert si.journal_entry_side({"side": "short", "direction": "long"}) == si.SIDE_SHORT

    def test_entry_side_legacy_direction_fallback(self):
        assert si.journal_entry_side({"direction": "long"}) == si.SIDE_LONG
        assert si.journal_entry_side({"direction": "sell"}) == si.SIDE_SHORT

    def test_entry_side_missing_is_unknown(self):
        assert si.journal_entry_side({}) == si.SIDE_UNKNOWN
        assert si.journal_entry_side(None) == si.SIDE_UNKNOWN

    def test_intent_side_matches_only_equal(self):
        e = {"direction": "short"}
        assert si.intent_side_matches(e, "short") is True
        assert si.intent_side_matches(e, "long") is False

    def test_sideless_legacy_row_fails_closed_for_matching(self):
        assert si.intent_side_matches({"symbol": "BTC-USD"}, "long") is False

    def test_invalid_requested_side_never_matches(self):
        assert si.intent_side_matches({"direction": "long"}, "unknown") is False


def _journal(tmp_path, monkeypatch):
    from core.clock import exchange_clock
    monkeypatch.setattr(exchange_clock, "now_ms", lambda: NOW_MS)
    monkeypatch.setattr(exchange_clock, "now_date_str", lambda: TODAY_S)
    return TradeJournal(log_dir=str(tmp_path))


def _intent_row(direction=None, side=None, symbol="BTC-USD"):
    row = {
        "entry_id": "e-1", "timestamp_ms": NOW_MS - 3_600_000,
        "symbol": symbol, "approved": True, "outcome": None,
        "entry_price": 100.0, "position_size": 1.0,
    }
    if direction is not None:
        row["direction"] = direction
    if side is not None:
        row["side"] = side
    return row


class TestFindOpenEntrySideFilter:
    def test_stale_opposite_side_intent_cannot_resurrect(self, tmp_path, monkeypatch):
        j = _journal(tmp_path, monkeypatch)
        (tmp_path / f"trade_journal_{TODAY_S}.json").write_text(
            json.dumps([_intent_row(direction="short")]))
        # A stale SHORT intent must NOT match a LONG adoption probe.
        e, d = j.find_open_entry_in_files("BTC-USD", days=7, side="long")
        assert e is None and d is None

    def test_matching_side_intent_found(self, tmp_path, monkeypatch):
        j = _journal(tmp_path, monkeypatch)
        (tmp_path / f"trade_journal_{TODAY_S}.json").write_text(
            json.dumps([_intent_row(direction="short")]))
        e, d = j.find_open_entry_in_files("BTC-USD", days=7, side="short")
        assert e is not None and d == TODAY_S

    def test_additive_side_field_matches(self, tmp_path, monkeypatch):
        j = _journal(tmp_path, monkeypatch)
        (tmp_path / f"trade_journal_{TODAY_S}.json").write_text(
            json.dumps([_intent_row(side="long")]))
        e, _ = j.find_open_entry_in_files("BTC-USD", days=7, side="long")
        assert e is not None

    def test_sideless_legacy_row_fails_closed(self, tmp_path, monkeypatch):
        j = _journal(tmp_path, monkeypatch)
        (tmp_path / f"trade_journal_{TODAY_S}.json").write_text(
            json.dumps([_intent_row()]))   # no side, no direction
        e, _ = j.find_open_entry_in_files("BTC-USD", days=7, side="long")
        assert e is None

    def test_symbol_scoped_legacy_scan_unchanged(self, tmp_path, monkeypatch):
        # side=None reproduces the legacy symbol-only scan bit-for-bit.
        j = _journal(tmp_path, monkeypatch)
        (tmp_path / f"trade_journal_{TODAY_S}.json").write_text(
            json.dumps([_intent_row(direction="short")]))
        e, d = j.find_open_entry_in_files("BTC-USD", days=7)
        assert e is not None and d == TODAY_S

    def test_non_crypto_symbol_side_filter_applies(self, tmp_path, monkeypatch):
        # Non-crypto same quarantine — the side filter is category-agnostic.
        j = _journal(tmp_path, monkeypatch)
        (tmp_path / f"trade_journal_{TODAY_S}.json").write_text(
            json.dumps([_intent_row(direction="short", symbol="SPY-USD")]))
        e, _ = j.find_open_entry_in_files("SPY-USD", days=7, side="long")
        assert e is None
        e2, _ = j.find_open_entry_in_files("SPY-USD", days=7, side="short")
        assert e2 is not None


# ── Legacy invariants: classify_size_sync bit-for-bit ───────────────────────

class TestLegacyClassifySizeSync:
    """Every legacy same-side size verdict is unchanged by the schema."""

    def test_none_within_tolerance(self):
        assert m.classify_size_sync(1.0, 1.0005, 100.0, "sodex") == "none"

    def test_grow(self):
        assert m.classify_size_sync(1.0, 1.5, 100.0, "sodex") == "grow"

    def test_shrink_silent_without_mark(self):
        assert m.classify_size_sync(1.0, 0.5, 0.0, "sodex") == "shrink_silent"

    def test_shrink_book(self):
        assert m.classify_size_sync(1.0, 0.5, 100.0, "sodex") == "shrink_book"

    def test_shrink_purge_sodex_dust(self):
        assert m.classify_size_sync(1.0, 0.05, 100.0, "sodex") == "shrink_purge"

    def test_aster_remnant_never_purged(self):
        assert m.classify_size_sync(1.0, 0.05, 100.0, "aster") == "shrink_book"
