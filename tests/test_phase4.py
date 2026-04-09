import unittest
import os
import shutil
import time
from tests.helpers import make_neutral_market_state, make_test_candidate, make_journal_with_trades
from memory.trade_journal import TradeJournal
from memory.performance import PerformanceTracker

class TestTradeJournal(unittest.TestCase):

    def setUp(self):
        self.test_dir = "/tmp/aria_test_logs"
        os.makedirs(self.test_dir, exist_ok=True)
        self.journal = TradeJournal(log_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_log_decision_creates_entry(self):
        entry_id = self.journal.log_decision(
            state=make_neutral_market_state("BTC"),
            candidate=make_test_candidate("BTC"),
            approved=True,
            reason="APPROVED"
        )
        self.assertIsNotNone(entry_id)
        entries = self.journal.get_all()
        self.assertEqual(len(entries), 1)

    def test_update_outcome(self):
        entry_id = self.journal.log_decision(
            state=make_neutral_market_state("BTC"),
            candidate=make_test_candidate("BTC"),
            approved=True,
            reason="APPROVED"
        )
        self.journal.update_outcome(
            entry_id=entry_id,
            outcome="tp1_hit",
            pnl_usd=15.0,
            closed_at_ms=int(time.time()*1000)
        )
        closed = self.journal.get_closed()
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["outcome"], "tp1_hit")

class TestPerformanceTracker(unittest.TestCase):

    def test_empty_journal_returns_zeros(self):
        journal = TradeJournal(log_dir="/tmp/aria_test_empty")
        tracker = PerformanceTracker()
        stats = tracker.compute(journal)
        self.assertEqual(stats.total_trades, 0)
        self.assertEqual(stats.win_rate, 0.0)

    def test_win_rate_calculation(self):
        journal = make_journal_with_trades(wins=7, losses=3)
        tracker = PerformanceTracker()
        stats = tracker.compute(journal)
        # wins/(wins+losses) = 7/10 = 0.7
        self.assertAlmostEqual(stats.win_rate, 0.7, places=1)

    def test_profit_factor(self):
        # 6 wins @ 200, 4 losses @ -100
        journal = make_journal_with_trades(wins=6, losses=4, avg_win_r=2.0, avg_loss_r=-1.0)
        tracker = PerformanceTracker()
        stats = tracker.compute(journal)
        # gross profit = 6 * 200 = 1200
        # gross loss = 4 * 100 = 400
        # PF = 1200 / 400 = 3.0
        self.assertGreater(stats.profit_factor, 1.0)
        self.assertEqual(stats.profit_factor, 3.0)

if __name__ == "__main__":
    unittest.main()
