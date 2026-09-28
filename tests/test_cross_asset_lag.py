"""
tests/test_cross_asset_lag.py — pin suite for the E2 cross-asset lag engine.

Doctrine pinned (2026-09-26 E2 build):
  1. NVDA +3% emits AMD inside the 1–3d window (not before 1d, not after 3d)
     and FET inside 6–24h.
  2. Confidence peaks mid-window (1.0) and decays toward max_lag (0.5).
  3. SOX confirm armed adds +0.15 to FET/RENDER only — never AMD.
  4. RENDER absent-from-universe abstains cleanly (never emitted, no crash).
  5. Dedupe: a second NVDA move while a FET signal is open refreshes the
     window — never duplicates.
  6. Persistence round-trip across an engine restart preserves in-window
     signals (6–24h windows survive restarts).
  7. Pair verdict fires on (googl −1%, coin +1%), not on (googl +0.5%, coin +1%).
  8. Expiry pruning removes spent windows.
  9. Kill switch CROSS_ASSET_LAG_ENABLED=false → no signals, pair None.

Test universe note: FET and RENDER are BOTH absent from the real tradeable
universe (verified 2026-09-26). Tests that exercise the FET-live path
inject tradeable_targets explicitly — exactly what the coordinator does
when a symbol gets listed. The module default keeps both dark.
"""
import json
import os

import pytest

from intelligence.cross_asset_lag import (
    CrossAssetLagEngine,
    LagSignal,
    LeadEvent,
    PROPAGATION_MAP,
    SOX_CONF_BOOST,
    pair_signal,
    window_confidence,
    PAIR_NAME,
)

T0 = 1_800_000_000.0

# The spec-author's assumed universe: FET listed, RENDER not.
FET_LIVE = ("AMD", "FET", "BTC")
FULL = ("AMD", "FET", "RENDER", "BTC")


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


def make_engine(tmp_path, clock, tradeable=FET_LIVE):
    return CrossAssetLagEngine(
        time_fn=clock,
        persist_path=str(tmp_path / "cross_asset_lag.json"),
        tradeable_targets=tradeable,
    )


def targets(signals):
    return {s.target_symbol for s in signals}


def one(signals, target):
    got = [s for s in signals if s.target_symbol == target]
    assert len(got) == 1, f"expected exactly one {target} signal, got {got}"
    return got[0]


# ── 1. NVDA +3% → AMD inside 1–3d, FET inside 6–24h ─────────────────────────

def test_nvda_move_emits_amd_inside_1_to_3d_window(tmp_path):
    clock = Clock()
    eng = make_engine(tmp_path, clock)
    ev = eng.ingest_move("NVDA", 3.0, T0)
    assert isinstance(ev, LeadEvent) and ev.direction == "LONG"

    clock.t = T0 + 86400 - 1          # 1s before min_lag
    assert "AMD" not in targets(eng.active_signals(clock.t))
    clock.t = T0 + 86400              # exactly min_lag — armed
    assert "AMD" in targets(eng.active_signals(clock.t))
    clock.t = T0 + 259200             # exactly max_lag — still armed (0.5)
    assert "AMD" in targets(eng.active_signals(clock.t))
    clock.t = T0 + 259200 + 1         # 1s past max_lag — expired
    assert "AMD" not in targets(eng.active_signals(clock.t))


def test_nvda_move_emits_fet_inside_6_to_24h_window(tmp_path):
    clock = Clock()
    eng = make_engine(tmp_path, clock)
    eng.ingest_move("NVDA", 3.0, T0)

    clock.t = T0 + 21600 - 1
    assert "FET" not in targets(eng.active_signals(clock.t))
    clock.t = T0 + 21600
    sig = one(eng.active_signals(clock.t), "FET")
    assert sig.direction == "LONG" and sig.source == "NVDA"
    assert sig.expires_ts == T0 + 86400
    clock.t = T0 + 86400
    assert "FET" in targets(eng.active_signals(clock.t))
    clock.t = T0 + 86400 + 1
    assert "FET" not in targets(eng.active_signals(clock.t))


def test_amd_move_emits_fet_and_direction_follows_leader(tmp_path):
    clock = Clock()
    eng = make_engine(tmp_path, clock)
    eng.ingest_move("AMD", -2.5, T0)   # leader down → follower SHORT
    clock.t = T0 + 21600
    sig = one(eng.active_signals(clock.t), "FET")
    assert sig.direction == "SHORT" and sig.source == "AMD"


def test_sub_threshold_move_arms_nothing(tmp_path):
    clock = Clock()
    eng = make_engine(tmp_path, clock)
    assert eng.ingest_move("NVDA", 1.9, T0) is None
    clock.t = T0 + 21600
    assert eng.active_signals(clock.t) == []
    assert eng.open_window_count() == 0


def test_coin_move_emits_btc_inside_1_to_12h(tmp_path):
    clock = Clock()
    eng = make_engine(tmp_path, clock)
    eng.ingest_move("COIN", 4.0, T0)
    clock.t = T0 + 3600
    sig = one(eng.active_signals(clock.t), "BTC")
    assert sig.source == "COIN"
    clock.t = T0 + 43200 + 1
    assert "BTC" not in targets(eng.active_signals(clock.t))


# ── 2. Confidence peaks mid-window and decays ───────────────────────────────

def test_confidence_curve_peaks_mid_window_decays_to_max_lag():
    # FET window: min 21600, max 86400, mid 54000
    assert window_confidence(21600, 21600, 86400) == pytest.approx(0.5)
    assert window_confidence(54000, 21600, 86400) == pytest.approx(1.0)
    assert window_confidence(86400, 21600, 86400) == pytest.approx(0.5)
    assert window_confidence(21599, 21600, 86400) is None
    assert window_confidence(86401, 21600, 86400) is None
    rising = window_confidence(30000, 21600, 86400)
    falling = window_confidence(70000, 21600, 86400)
    assert 0.5 < rising < 1.0 and 0.5 < falling < 1.0
    assert falling < window_confidence(60000, 21600, 86400)  # monotone decay


def test_confidence_mid_window_via_engine(tmp_path):
    clock = Clock()
    eng = make_engine(tmp_path, clock)
    eng.ingest_move("NVDA", 3.0, T0)
    clock.t = T0 + 54000              # FET mid-window
    assert one(eng.active_signals(clock.t), "FET").confidence == pytest.approx(1.0)
    clock.t = T0 + 21600              # window edge
    assert one(eng.active_signals(clock.t), "FET").confidence == pytest.approx(0.5)


# ── 3. SOX confirm: +0.15 to FET/RENDER only, never AMD ─────────────────────

def test_sox_confirm_boosts_semi_crypto_targets_only(tmp_path):
    clock = Clock()
    eng = make_engine(tmp_path, clock, tradeable=FULL)
    eng.ingest_move("NVDA", 3.0, T0)

    clock.t = T0 + 86400              # AMD at min_lag; FET/RENDER at max_lag (0.5)
    base = {s.target_symbol: s.confidence for s in eng.active_signals(clock.t)}
    assert base["AMD"] == pytest.approx(0.5)
    assert base["FET"] == pytest.approx(0.5)
    assert base["RENDER"] == pytest.approx(0.5)

    eng2 = make_engine(tmp_path / "b", clock, tradeable=FULL)
    eng2.ingest_move("NVDA", 3.0, T0)
    assert eng2.ingest_sox_gap(2.0, T0) is True   # > 1.5% arms the confirm
    boosted = {s.target_symbol: s.confidence for s in eng2.active_signals(clock.t)}
    # AMD (equity) untouched; FET/RENDER semi-crypto boosted +0.15
    assert boosted["AMD"] == pytest.approx(base["AMD"])
    assert boosted["FET"] == pytest.approx(base["FET"] + SOX_CONF_BOOST)
    assert boosted["RENDER"] == pytest.approx(base["RENDER"] + SOX_CONF_BOOST)
    # mid-window the boost is capped at the 1.0 peak, never above
    clock.t = T0 + 54000
    assert one(eng2.active_signals(clock.t), "FET").confidence == pytest.approx(1.0)


def test_sox_absent_or_small_gap_never_boosts_and_never_blocks(tmp_path):
    clock = Clock()
    eng = make_engine(tmp_path, clock)
    eng.ingest_move("NVDA", 3.0, T0)
    eng.ingest_sox_gap(1.0, T0)                    # below the 1.5% arm
    clock.t = T0 + 54000
    assert one(eng.active_signals(clock.t), "FET").confidence == pytest.approx(1.0)
    eng.ingest_sox_gap(3.0, T0 - 90000)            # armed but stale (> TTL)
    assert eng.sox_confirm_armed(clock.t) is False


# ── 4. RENDER absent-from-universe abstains cleanly ─────────────────────────

def test_render_dark_target_never_emitted(tmp_path):
    clock = Clock()
    eng = make_engine(tmp_path, clock)             # FET_LIVE: RENDER absent
    assert "RENDER" in eng.dark_targets
    eng.ingest_move("NVDA", 5.0, T0)
    for dt in (21600, 54000, 86400):
        clock.t = T0 + dt
        sigs = eng.active_signals(clock.t)
        assert "RENDER" not in targets(sigs)       # abstain, not crash
    assert eng.open_window_count() == 3            # window still tracked


def test_default_universe_keeps_fet_and_render_dark(tmp_path):
    clock = Clock()
    eng = CrossAssetLagEngine(time_fn=clock,
                              persist_path=str(tmp_path / "s.json"))
    assert eng.dark_targets == frozenset({"FET", "RENDER"})
    eng.ingest_move("NVDA", 3.0, T0)
    clock.t = T0 + 54000
    assert targets(eng.active_signals(clock.t)) == set()  # AMD not yet in window


# ── 5. Dedupe: refresh, never duplicate ─────────────────────────────────────

def test_second_nvda_move_refreshes_fet_window_no_duplicate(tmp_path):
    clock = Clock()
    eng = make_engine(tmp_path, clock)
    eng.ingest_move("NVDA", 3.0, T0)
    clock.t = T0 + 10000
    eng.ingest_move("NVDA", 4.0, clock.t)          # second leader move

    clock.t = T0 + 10000 + 21600                   # in the SECOND window
    sigs = eng.active_signals(clock.t)
    sig = one(sigs, "FET")
    assert sig.expires_ts == T0 + 10000 + 86400    # refreshed, not stacked
    assert eng.open_window_count() == 3            # AMD/FET/RENDER — one each


# ── 6. Persistence round-trip across restart ────────────────────────────────

def test_persistence_round_trip_preserves_in_window_signals(tmp_path):
    clock = Clock()
    path = str(tmp_path / "cross_asset_lag.json")
    eng = CrossAssetLagEngine(time_fn=clock, persist_path=path,
                              tradeable_targets=FET_LIVE)
    eng.ingest_move("NVDA", 3.0, T0)
    eng.ingest_sox_gap(2.0, T0)
    assert os.path.exists(path)
    assert os.path.exists(str(tmp_path / "cross_asset_lag.jsonl"))

    clock.t = T0 + 30000                           # 8.3h later — FET in window
    eng2 = CrossAssetLagEngine(time_fn=clock, persist_path=path,
                               tradeable_targets=FET_LIVE)
    sig = one(eng2.active_signals(clock.t), "FET")
    assert sig.source == "NVDA" and sig.expires_ts == T0 + 86400
    assert eng2.sox_confirm_armed(clock.t) is True

    clock.t = T0 + 86400 + 1                       # past expiry across restart
    eng3 = CrossAssetLagEngine(time_fn=clock, persist_path=path,
                               tradeable_targets=FET_LIVE)
    assert "FET" not in targets(eng3.active_signals(clock.t))


def test_corrupt_state_fails_closed(tmp_path):
    clock = Clock()
    path = tmp_path / "cross_asset_lag.json"
    path.write_text("{not json")
    eng = CrossAssetLagEngine(time_fn=clock, persist_path=str(path),
                              tradeable_targets=FET_LIVE)
    assert eng.active_signals(clock.t) == []
    assert eng.open_window_count() == 0


# ── 7. Rotation pair verdict ────────────────────────────────────────────────

def test_pair_signal_fires_on_googl_bleed_coin_bid():
    got = pair_signal(-1.0, 1.0)
    assert got is not None
    name, conf = got
    assert name == PAIR_NAME == "SHORT_GOOGL_LONG_COIN"
    assert conf == pytest.approx(0.5)


def test_pair_signal_silent_on_googl_strength_or_coin_bleed():
    assert pair_signal(0.5, 1.0) is None           # GOOGL not bleeding
    assert pair_signal(-1.0, -0.5) is None         # COIN not catching the bid
    assert pair_signal(None, 1.0) is None          # abstain on garbage


def test_engine_pair_delegate(tmp_path):
    clock = Clock()
    eng = make_engine(tmp_path, clock)
    assert eng.pair_signal(-1.0, 1.0) is not None


# ── 8. Expiry pruning ───────────────────────────────────────────────────────

def test_expired_windows_pruned(tmp_path):
    clock = Clock()
    eng = make_engine(tmp_path, clock)
    eng.ingest_move("NVDA", 3.0, T0)
    assert eng.open_window_count() == 3
    clock.t = T0 + 86400 + 1                       # FET/RENDER spent, AMD live
    eng.active_signals(clock.t)
    assert eng.open_window_count() == 1
    clock.t = T0 + 259200 + 1
    assert eng.active_signals(clock.t) == []
    assert eng.open_window_count() == 0


# ── 9. Kill switch ──────────────────────────────────────────────────────────

def test_kill_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv("CROSS_ASSET_LAG_ENABLED", "false")
    clock = Clock()
    eng = make_engine(tmp_path, clock)
    eng.ingest_move("NVDA", 3.0, T0)               # state still tracked
    clock.t = T0 + 54000
    assert eng.active_signals(clock.t) == []
    assert pair_signal(-1.0, 1.0) is None
    assert eng.open_window_count() == 3            # nothing lost
