"""Symbol-evidence gate pins (Governor 2026-09-27 wrong-trades verdict).

The gate lives inline in on_signal_ready (main.py, beside the personality
blacklist — same journal-backed pool), hard-blocking entries whose
(symbol, direction) pair has WR < floor over >= min_trades closes.
Direction-conditioned per the 2026-08-29 symbol_edge lesson: pooled
beliefs let ETH's 17%-WR opposed-tide shorts throttle 100%-WR longs —
each direction carries its own evidence, and an insufficient same-direction
sample is NO belief (never the pooled pool).

These pins cover the data plane: PerformanceTracker (symbol, direction)
aggregation at boot (restore_from_journal) and live (record_trade_closed),
the operator/phantom filters the gate inherits, the config contract, and
the shadow-registry capacity the gate's counterfactual scoring needs.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.performance import PerformanceTracker  # noqa: E402


def _close(pnl, sym="XAUT-USD", ms=1790400000000, direction="long",
           personality="FLOW", **extra):
    rec = {"entry_id": f"e-{sym}-{direction}-{ms}-{pnl}", "symbol": sym,
           "outcome": "win" if pnl >= 0 else "loss",
           "direction": direction,
           "pnl_usd": pnl, "pnl_net_usd": pnl, "closed_at_ms": ms,
           "timestamp_ms": ms - 60_000, "approved": True,
           "personality": personality}
    rec.update(extra)
    return rec


class TestSymbolDirectionStatsRestore:
    def _write(self, log_dir, name, records):
        (log_dir / name).write_text(json.dumps(records))

    def test_stats_restored_per_direction(self, tmp_path):
        recs = [_close(-1.0, ms=1790400000000 + i * 1000) for i in range(18)]
        recs += [_close(0.5, ms=1790400018000 + i * 1000) for i in range(2)]
        self._write(tmp_path, "trade_journal_2026-09-26.json", recs)
        pt = PerformanceTracker()
        pt.restore_from_journal(str(tmp_path))
        st = pt.get_symbol_direction_stats("XAUT-USD", "long")
        assert st is not None
        assert st.total_trades == 20
        assert st.wins == 2
        assert st.win_rate == 0.1

    def test_direction_evidence_does_not_cross_poison(self, tmp_path):
        # The 2026-08-29 ETH lesson: a heap of dead shorts must leave the
        # long belief EMPTY (no evidence), never throttled by the pool.
        shorts = [_close(-1.0, direction="short", ms=1790400000000 + i * 1000)
                  for i in range(30)]
        self._write(tmp_path, "trade_journal_2026-09-26.json", shorts)
        pt = PerformanceTracker()
        pt.restore_from_journal(str(tmp_path))
        assert pt.get_symbol_direction_stats("XAUT-USD", "long") is None
        st = pt.get_symbol_direction_stats("XAUT-USD", "short")
        assert st is not None and st.total_trades == 30

    def test_missing_direction_is_no_belief(self, tmp_path):
        rec = _close(-1.0)
        del rec["direction"]
        self._write(tmp_path, "trade_journal_2026-09-26.json", [rec])
        pt = PerformanceTracker()
        pt.restore_from_journal(str(tmp_path))
        assert pt.get_symbol_direction_stats("XAUT-USD", "long") is None

    def test_unknown_symbol_returns_none(self, tmp_path):
        self._write(tmp_path, "trade_journal_2026-09-26.json", [_close(1.0)])
        pt = PerformanceTracker()
        pt.restore_from_journal(str(tmp_path))
        assert pt.get_symbol_direction_stats("NOPE-USD", "long") is None

    def test_operator_closes_excluded(self, tmp_path):
        # The gate inherits the 2026-09-26 operator purge: manual-session
        # orphan closes must never count against a symbol's evidence.
        op = _close(-5.0, orphan_close=True)
        real = _close(1.0, ms=1790400060000)
        self._write(tmp_path, "trade_journal_2026-09-26.json", [op, real])
        pt = PerformanceTracker()
        pt.restore_from_journal(str(tmp_path))
        st = pt.get_symbol_direction_stats("XAUT-USD", "long")
        assert st is not None
        assert st.total_trades == 1
        assert st.win_rate == 1.0

    def test_campaign_closes_never_feed_the_pool(self, tmp_path):
        # R1 (main-book beliefs never ingest campaign outcomes) applies to
        # the evidence pool: CAMPAIGN rows are invisible to the gate.
        camp = [_close(-1.0, personality="CAMPAIGN",
                       ms=1790400000000 + i * 1000) for i in range(25)]
        real = _close(1.0, ms=1790400026000)
        self._write(tmp_path, "trade_journal_2026-09-26.json", camp + [real])
        pt = PerformanceTracker()
        pt.restore_from_journal(str(tmp_path))
        st = pt.get_symbol_direction_stats("XAUT-USD", "long")
        assert st is not None
        assert st.total_trades == 1
        assert st.win_rate == 1.0

    def test_dedup_across_day_files(self, tmp_path):
        dup = _close(-1.0)
        self._write(tmp_path, "trade_journal_2026-09-26.json", [dup])
        self._write(tmp_path, "trade_journal_2026-09-27.json", [dup])
        pt = PerformanceTracker()
        pt.restore_from_journal(str(tmp_path))
        st = pt.get_symbol_direction_stats("XAUT-USD", "long")
        assert st.total_trades == 1


class TestSymbolDirectionStatsLive:
    def test_record_trade_closed_updates_pair(self):
        pt = PerformanceTracker()
        pt.record_trade_closed("FLOW", "loss", -1.0, "stop_loss",
                               symbol="XAUT-USD", direction="long")
        pt.record_trade_closed("FLOW", "win", 0.5, "",
                               symbol="XAUT-USD", direction="long")
        st = pt.get_symbol_direction_stats("XAUT-USD", "long")
        assert st is not None
        assert st.total_trades == 2
        assert st.wins == 1
        assert st.win_rate == 0.5
        assert st.stop_hits == 1
        assert pt.get_symbol_direction_stats("XAUT-USD", "short") is None

    def test_record_trade_closed_campaign_close_skipped(self):
        pt = PerformanceTracker()
        pt.record_trade_closed("CAMPAIGN", "loss", -1.0, "",
                               symbol="XAUT-USD", direction="long")
        assert pt.get_symbol_direction_stats("XAUT-USD", "long") is None

    def test_record_trade_closed_without_pair_is_legacy(self):
        pt = PerformanceTracker()
        pt.record_trade_closed("FLOW", "win", 1.0)
        assert pt.get_symbol_direction_stats("XAUT-USD", "long") is None
        assert pt.get_personality_stats("FLOW").total_trades == 1


class TestConfigContract:
    def test_gate_knobs_exist_with_doctrine_defaults(self):
        from core.config import Settings
        s = Settings()
        assert s.symbol_evidence_gate_enabled is True
        assert s.symbol_evidence_min_trades == 20
        assert s.symbol_evidence_wr_floor == 0.35

    def test_shadow_capacity_knobs_cover_24h_finalization(self):
        from core.config import Settings
        s = Settings()
        # ~5k records/day at the live rate must survive the 24h horizon.
        assert s.shadow_max_open >= 12000
        assert s.shadow_max_record_per_day >= 12000


class TestShadowCapacity:
    def test_module_defaults_cover_24h(self):
        from intelligence import shadow_journal as sj
        assert sj._MAX_OPEN >= 12000
        assert sj._MAX_RECORD_PER_DAY >= 12000

    def test_config_override(self):
        from intelligence import shadow_journal as sj
        j = sj.ShadowJournal()

        class _C:
            shadow_max_open = 42
            shadow_max_record_per_day = 43

        j._config = _C()
        assert j._max_open() == 42
        assert j._max_record_per_day() == 43
        j._config = None
        assert j._max_open() == sj._MAX_OPEN
        assert j._max_record_per_day() == sj._MAX_RECORD_PER_DAY
