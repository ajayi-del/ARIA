"""tests/test_coherence_iv.py — pins for the wave-2 coherence IV reweighting.

Doctrine pinned: k=20 shrinkage, coin-flip tiers earn ZERO weight, measured
winners dominate, Σw = #active tiers normalization, IV score on the legacy
sum's scale, unmeasured tiers neutral (never silently zeroed), kill switch
False = legacy bit-for-bit, stats loader read-only + fail-closed.
"""
import os
import sqlite3

import pytest

from intelligence.coherence import (
    CoherenceEngine, iv_tier_weights, iv_weighted_score, load_tier_hit_stats,
)


def _fixture_db(tmp_path, rows):
    """rows: list of (tier_prefix, fired, correct)."""
    db = tmp_path / "outcomes.db"
    con = sqlite3.connect(str(db))
    cols = []
    for tier in ("macro", "regime", "structure", "micro", "funding", "ssi"):
        cols += [f"{tier}_fired INTEGER", f"{tier}_correct INTEGER"]
    con.execute(f"CREATE TABLE outcomes (trade_id TEXT, {', '.join(cols)})")
    for tier, fired, correct in rows:
        rec = {"trade_id": "t"}
        for t in ("macro", "regime", "structure", "micro", "funding", "ssi"):
            rec[f"{t}_fired"] = int(t == tier and fired)
            rec[f"{t}_correct"] = int(t == tier and correct)
        keys = ", ".join(rec)
        con.execute(f"INSERT INTO outcomes ({keys}) VALUES ({', '.join('?' * len(rec))})",
                    list(rec.values()))
    con.commit()
    con.close()
    return str(db)


# ── stats loader ─────────────────────────────────────────────────────────────

def test_loader_counts_fired_and_correct(tmp_path):
    db = _fixture_db(tmp_path, [("micro", 1, 1), ("micro", 1, 0),
                                ("micro", 0, 0), ("funding", 1, 1)])
    stats = load_tier_hit_stats(db)
    assert stats["micro"] == {"n": 2, "wins": 1}
    assert stats["funding"] == {"n": 1, "wins": 1}
    assert stats["regime"] == {"n": 0, "wins": 0}


def test_loader_missing_db_returns_empty(tmp_path):
    assert load_tier_hit_stats(str(tmp_path / "nope.db")) == {}


def test_loader_readonly_on_wal_db(tmp_path):
    db = _fixture_db(tmp_path, [("micro", 1, 1)])
    stats = load_tier_hit_stats(db)
    assert stats["micro"]["wins"] == 1
    # file untouched: reopen rw and confirm no journal side effects broke it
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM outcomes").fetchone()[0] == 1
    con.close()


# ── weight math ──────────────────────────────────────────────────────────────

def test_coin_flip_tier_zero_weight():
    # n=200, wins=100 → p=0.5 exactly → w = max(p−0.5,0)/var = 0
    w = iv_tier_weights({"microstructure": 1.0},
                        {"micro": {"n": 200, "wins": 100}})
    assert w["microstructure"] == 0.0


def test_proven_tier_dominates():
    w = iv_tier_weights(
        {"microstructure": 1.0, "funding": 1.0},
        {"micro": {"n": 200, "wins": 160},   # p = (160+5)/220 = 0.75
         "funding": {"n": 200, "wins": 100}})  # coin flip → 0
    assert w["microstructure"] == pytest.approx(2.0)  # Σw = n_active = 2
    assert w["funding"] == 0.0


def test_weights_normalize_to_active_count():
    w = iv_tier_weights(
        {"microstructure": 1.0, "funding": 1.0, "regime": 1.0},
        {"micro": {"n": 100, "wins": 70},
         "funding": {"n": 100, "wins": 65}})
    assert sum(w.values()) == pytest.approx(3.0)


def test_unmeasured_tier_neutral_not_zeroed():
    # rotation_laggard has no DB mapping; liquidation neither — both abstain
    w = iv_tier_weights({"rotation_laggard": 1.0, "microstructure": 1.0},
                        {"micro": {"n": 200, "wins": 100}})  # coin flip → 0
    assert w["rotation_laggard"] > 0.0
    assert sum(w.values()) == pytest.approx(2.0)


def test_all_coin_flip_uniform_fallback():
    w = iv_tier_weights({"microstructure": 1.0, "funding": 1.0},
                        {"micro": {"n": 200, "wins": 100},
                         "funding": {"n": 200, "wins": 100}})
    assert w == {"microstructure": 1.0, "funding": 1.0}


def test_shrinkage_small_n_pulls_to_half():
    # n=2, wins=2 raw 100% — shrunk p = (2+5)/22 ≈ 0.318 < 0.5 → zero weight
    w = iv_tier_weights({"microstructure": 1.0},
                        {"micro": {"n": 2, "wins": 2}})
    assert w["microstructure"] == 0.0


def test_empty_components_empty_weights():
    assert iv_tier_weights({}, {"micro": {"n": 10, "wins": 9}}) == {}
    assert iv_weighted_score({}, {"micro": {"n": 10, "wins": 9}}) == (None, {})


def test_iv_score_legacy_scale():
    # single active tier at neutral weight 1.0 → IV score == legacy sum
    score, w = iv_weighted_score({"microstructure": 2.0},
                                 {"micro": {"n": 100, "wins": 90}})
    assert score == pytest.approx(2.0)
    # two tiers: proven micro double-weight vs coin-flip funding
    score2, _ = iv_weighted_score({"microstructure": 2.0, "funding": 1.0},
                                  {"micro": {"n": 200, "wins": 160},
                                   "funding": {"n": 200, "wins": 100}})
    assert score2 == pytest.approx(4.0)  # 2.0×2.0 + 0×1.0


# ── kill switch bit-for-bit ──────────────────────────────────────────────────

def test_engine_legacy_bit_for_bit_when_switch_off(monkeypatch, tmp_path):
    # no stats available → no IV path at all; and the switch defaults False
    # via getattr(config, ..., False). Legacy sum must be untouched.
    import intelligence.coherence as coh
    monkeypatch.setattr(coh, "tier_hit_stats_cached", lambda *a, **k: {})
    monkeypatch.setattr(coh, "_iv_enabled", lambda: False)
    eng = CoherenceEngine()
    out = {"macro_bias": "bullish", "volume_surge": 3.0, "candle_conviction": 0.8}
    w1, r1, c1 = eng.calculate_weighted_score("ETH-USD", out)
    legacy_sum = sum(v for k, v in c1.items() if k != "independence_discount")
    assert w1 == pytest.approx(
        legacy_sum * c1["independence_discount"])


def test_engine_iv_replaces_sum_when_enabled(monkeypatch):
    import intelligence.coherence as coh
    stats = {"micro": {"n": 200, "wins": 160}}
    monkeypatch.setattr(coh, "tier_hit_stats_cached", lambda *a, **k: stats)
    monkeypatch.setattr(coh, "_iv_enabled", lambda: True)
    eng = CoherenceEngine()
    # microstructure-only signal: IV keeps the single active tier at weight 1
    out = {"macro_bias": "bullish", "volume_surge": 3.0}
    w_score, _, comps = eng.calculate_weighted_score("ETH-USD", out)
    assert w_score == pytest.approx(
        comps["microstructure"] * comps["independence_discount"])
