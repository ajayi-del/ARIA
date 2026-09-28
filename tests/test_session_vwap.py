"""tests/test_session_vwap.py — pins for the E3 session-VWAP accumulator.

Pinned: hand-computed 3-bar VWAP math, out-of-window bar rejection, session
rollover resetting accumulation, deviation sign, the stale flag boundaries,
the persistence round-trip (save -> load restores cumulative state), and
the kill switch.
"""
from __future__ import annotations

import datetime
import json
import os

import pytest

from data.session_vwap import (
    SESSION_LEN_S,
    STALE_S,
    SessionVwap,
    SessionVwapStore,
    in_session,
    session_anchor_for,
    session_vwap_enabled,
    vwap_accumulate,
)


def _ts(day: int, hour: int, minute: int = 0, second: int = 0) -> float:
    return datetime.datetime(2026, 9, day, hour, minute, second,
                             tzinfo=datetime.timezone.utc).timestamp()


# Bar times inside the 2026-09-25 session (13:30-20:00 UTC)
T1 = _ts(25, 14, 0)
T2 = _ts(25, 14, 1)
T3 = _ts(25, 14, 2)
ANCHOR = session_anchor_for(T1)


class TestPureMath:
    def test_three_bar_hand_computed(self):
        # tp1=9 -> pv 900; tp2=10 -> pv 2000; tp3=11 -> pv 3300
        pv, vol = vwap_accumulate(0.0, 0.0, 10, 8, 9, 100)
        assert (pv, vol) == (900.0, 100.0)
        pv, vol = vwap_accumulate(pv, vol, 11, 9, 10, 200)
        assert (pv, vol) == (2900.0, 300.0)
        pv, vol = vwap_accumulate(pv, vol, 12, 10, 11, 300)
        assert (pv, vol) == (6200.0, 600.0)

    def test_zero_volume_contributes_nothing(self):
        pv, vol = vwap_accumulate(100.0, 10.0, 50, 40, 45, 0)
        assert (pv, vol) == (100.0, 10.0)

    def test_garbage_fails_closed(self):
        assert vwap_accumulate(100.0, 10.0, None, 40, 45, 5) == (100.0, 10.0)
        assert vwap_accumulate(100.0, 10.0, "x", 40, 45, 5) == (100.0, 10.0)


class TestSessionGeometry:
    def test_anchor_is_1330_utc(self):
        assert ANCHOR == _ts(25, 13, 30)

    def test_before_open_belongs_to_yesterday(self):
        assert session_anchor_for(_ts(25, 9, 0)) == _ts(24, 13, 30)

    def test_in_session_boundaries(self):
        assert in_session(_ts(25, 13, 30))
        assert in_session(_ts(25, 19, 59, 59))
        assert not in_session(_ts(25, 13, 29, 59))
        assert not in_session(_ts(25, 20, 0))


class TestSessionVwap:
    def _acc_with_three_bars(self) -> SessionVwap:
        acc = SessionVwap("NVDA", ANCHOR)
        acc.update(T1, 10, 8, 9, 100)
        acc.update(T2, 11, 9, 10, 200)
        acc.update(T3, 12, 10, 11, 300)
        return acc

    def test_vwap_math(self):
        acc = self._acc_with_three_bars()
        assert acc.vwap() == pytest.approx(6200.0 / 600.0)
        assert acc.bars_seen() == 3

    def test_deviation_sign(self):
        acc = self._acc_with_three_bars()
        v = acc.vwap()
        hi = acc.deviation_pct(v * 1.01)
        lo = acc.deviation_pct(v * 0.99)
        assert hi == pytest.approx(1.0)
        assert lo == pytest.approx(-1.0)
        assert acc.deviation_pct(v) == pytest.approx(0.0)

    def test_deviation_none_before_volume(self):
        acc = SessionVwap("NVDA", ANCHOR)
        assert acc.vwap() is None
        assert acc.deviation_pct(100) is None

    def test_out_of_window_bar_rejected(self):
        acc = SessionVwap("NVDA", ANCHOR)
        assert acc.update(ANCHOR + SESSION_LEN_S, 10, 8, 9, 100) is False  # 20:00
        assert acc.update(ANCHOR - 60, 10, 8, 9, 100) is False             # 13:29
        assert acc.bars_seen() == 0

    def test_stale_flag(self):
        acc = self._acc_with_three_bars()
        assert acc.is_stale(now=T3 + STALE_S - 1) is False
        assert acc.is_stale(now=T3 + STALE_S + 1) is True
        assert SessionVwap("NVDA", ANCHOR).is_stale(now=T3) is True  # no bar ever

    def test_dict_round_trip(self):
        acc = self._acc_with_three_bars()
        back = SessionVwap.from_dict(acc.to_dict())
        assert back.vwap() == acc.vwap()
        assert back.bars_seen() == 3
        assert back.last_bar_ts == T3


class TestStore:
    SYMBOLS = ["NVDA", "COIN"]

    def test_session_rollover_resets(self, tmp_path):
        store = SessionVwapStore(self.SYMBOLS, log_dir=str(tmp_path),
                                 time_fn=lambda: T1)
        store.update("NVDA", T1, 10, 8, 9, 100)
        store.update("NVDA", T2, 11, 9, 10, 200)
        assert store.get("NVDA").bars_seen() == 2
        # next-day bar rolls the session: accumulation resets
        t_next = _ts(26, 14, 0)
        store.update("NVDA", t_next, 12, 10, 11, 300)
        acc = store.get("NVDA")
        assert acc.anchor == _ts(26, 13, 30)
        assert acc.bars_seen() == 1
        assert acc.vwap() == pytest.approx(11.0)

    def test_persistence_round_trip(self, tmp_path):
        store = SessionVwapStore(self.SYMBOLS, log_dir=str(tmp_path),
                                 time_fn=lambda: T3)
        store.update("NVDA", T1, 10, 8, 9, 100)
        store.update("NVDA", T2, 11, 9, 10, 200)
        store.update("NVDA", T3, 12, 10, 11, 300)
        assert os.path.exists(tmp_path / "session_vwap.json")
        assert os.path.exists(tmp_path / "session_vwap.jsonl")

        restored = SessionVwapStore(self.SYMBOLS, log_dir=str(tmp_path),
                                    time_fn=lambda: T3)
        assert restored.load() == 1
        acc = restored.get("NVDA")
        assert acc.cum_pv == pytest.approx(6200.0)
        assert acc.cum_vol == pytest.approx(600.0)
        assert acc.bars_seen() == 3
        assert acc.vwap() == pytest.approx(6200.0 / 600.0)

    def test_load_missing_file_is_zero(self, tmp_path):
        store = SessionVwapStore(self.SYMBOLS, log_dir=str(tmp_path))
        assert store.load() == 0

    def test_load_corrupt_file_is_zero(self, tmp_path):
        (tmp_path / "session_vwap.json").write_text("{not json")
        store = SessionVwapStore(self.SYMBOLS, log_dir=str(tmp_path))
        assert store.load() == 0

    def test_unknown_symbol_rejected(self, tmp_path):
        store = SessionVwapStore(self.SYMBOLS, log_dir=str(tmp_path))
        assert store.update("TSLA", T1, 10, 8, 9, 100) is False

    def test_out_of_session_bar_rejected(self, tmp_path):
        store = SessionVwapStore(self.SYMBOLS, log_dir=str(tmp_path))
        assert store.update("NVDA", _ts(25, 10, 0), 10, 8, 9, 100) is False

    def test_kill_switch(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SESSION_VWAP_ENABLED", "false")
        assert session_vwap_enabled() is False
        store = SessionVwapStore(self.SYMBOLS, log_dir=str(tmp_path))
        assert store.update("NVDA", T1, 10, 8, 9, 100) is False
        assert store.get("NVDA") is None
        monkeypatch.setenv("SESSION_VWAP_ENABLED", "true")
        assert store.update("NVDA", T1, 10, 8, 9, 100) is True

    def test_jsonl_snapshot_rows(self, tmp_path):
        store = SessionVwapStore(self.SYMBOLS, log_dir=str(tmp_path),
                                 time_fn=lambda: T2)
        store.update("NVDA", T1, 10, 8, 9, 100)
        store.update("NVDA", T2, 11, 9, 10, 200)
        with open(tmp_path / "session_vwap.jsonl") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        assert len(rows) == 2
        assert rows[-1]["vwap"] == pytest.approx(2900.0 / 300.0)
