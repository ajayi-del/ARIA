"""tests/test_adopted_stamp.py — FOLD B (2026-10-19)

adopted_stamp restores the fresh-entry neural stamps (entry_plane /
coherence_measured / coherence_source) onto boot-adopted positions from the
newest FILLED execution plane ledger row for symbol+side. Pure, never raises,
None when there is nothing honest to restore.
"""
import unittest

from intelligence.plane_ledger import adopted_stamp


def _row(symbol="BTC-USD", side="long", filled=True, ts_ms=1000,
         plane="gated", measured=4.5, asserted=None):
    return {
        "ts_ms": ts_ms,
        "identity": {"symbol": symbol, "side": side,
                     "attempt_id": f"{symbol}_{ts_ms}"},
        "plane": {"plane": plane, "strategy_tag": "momentum_cont",
                  "executor": "standard", "entry_path_site": "post_fill"},
        "gate_vector": {"coherence": {"measured": measured,
                                      "asserted": asserted,
                                      "floor": 3.0, "would_block": False}},
        "outcome_key": {"entry_id_uuid": "u", "trade_id": "t",
                        "filled": filled, "fill_ts_ms": ts_ms,
                        "reject_reason": None},
    }


class TestAdoptedStamp(unittest.TestCase):

    def test_match_measured_leg(self):
        rows = [_row(ts_ms=2000, plane="fastpath", measured=6.25)]
        st = adopted_stamp(rows, "BTC-USD", "long")
        self.assertEqual(st, {"entry_plane": "fastpath",
                              "coherence_source": "measured",
                              "coherence_measured": 6.25})

    def test_asserted_fallback(self):
        rows = [_row(measured=None, asserted=8.0)]
        st = adopted_stamp(rows, "BTC-USD", "long")
        self.assertEqual(st, {"entry_plane": "gated",
                              "coherence_source": "asserted",
                              "coherence_measured": 8.0})

    def test_no_match_none(self):
        self.assertIsNone(adopted_stamp([], "BTC-USD", "long"))
        self.assertIsNone(adopted_stamp(None, "BTC-USD", "long"))
        # wrong symbol / wrong side
        self.assertIsNone(adopted_stamp([_row(symbol="ETH-USD")],
                                        "BTC-USD", "long"))
        self.assertIsNone(adopted_stamp([_row(side="short")],
                                        "BTC-USD", "long"))

    def test_unfilled_rows_skipped(self):
        rows = [_row(filled=False)]
        self.assertIsNone(adopted_stamp(rows, "BTC-USD", "long"))

    def test_newest_wins_on_duplicate_symbol_side(self):
        rows = [
            _row(ts_ms=9000, plane="fastpath", measured=7.0),
            _row(ts_ms=1000, plane="gated", measured=3.5),
            _row(ts_ms=5000, plane="gated", measured=4.0),
        ]
        st = adopted_stamp(rows, "BTC-USD", "long")
        self.assertEqual(st["entry_plane"], "fastpath")
        self.assertEqual(st["coherence_measured"], 7.0)
        # newest filled must not be beaten by an older UNFILLED row either
        rows2 = rows + [_row(ts_ms=99999, filled=False, plane="gated")]
        self.assertEqual(adopted_stamp(rows2, "BTC-USD", "long"), st)

    def test_plane_only_and_coherence_only(self):
        # plane present, coherence vector absent → plane-only stamp
        r = _row()
        r["gate_vector"] = {}
        st = adopted_stamp([r], "BTC-USD", "long")
        self.assertEqual(st, {"entry_plane": "gated"})
        # coherence present, plane absent → coherence-only stamp
        r2 = _row(measured=5.0)
        r2["plane"] = {"plane": None}
        st2 = adopted_stamp([r2], "BTC-USD", "long")
        self.assertEqual(st2, {"coherence_source": "measured",
                               "coherence_measured": 5.0})

    def test_both_legs_absent_none(self):
        r = _row(measured=None, asserted=None)
        r["plane"] = {"plane": None}
        self.assertIsNone(adopted_stamp([r], "BTC-USD", "long"))

    def test_never_raises_on_garbage(self):
        for bad in (None, "x", 7, {}, {"ts_ms": "NaN-str"},
                    {"identity": None, "plane": None,
                     "outcome_key": None, "ts_ms": None}):
            try:
                adopted_stamp([bad], "BTC-USD", "long")
            except Exception as e:  # pragma: no cover
                self.fail(f"raised on {bad!r}: {e}")


if __name__ == "__main__":
    unittest.main()
