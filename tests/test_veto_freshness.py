"""Tests for intelligence/veto_freshness.py (2026-09-17).

Pins: TTL expiry, regime invalidation, decay monotonicity + half-life, fire
threshold, classifier branches, LOO unique-vs-shared attribution, JSONL
append, fail-silence on garbage input.
"""
import json
from pathlib import Path

import pytest

from intelligence import veto_freshness as vf


# ── VetoRecord.effective_weight ───────────────────────────────────────────────

def _rec(weight=1.0, collected_at=1_000.0, regime="QUIET", ttl=400.0):
    return vf.VetoRecord(
        veto_type="quiet_market_pause",
        weight=weight,
        collected_at=collected_at,
        collected_regime=regime,
        hard_ttl_s=ttl,
    )


def test_ttl_expiry_zero_weight():
    rec = _rec(ttl=400.0)
    assert rec.effective_weight(1_000.0 + 401.0, "QUIET") == 0.0
    # Exactly at the boundary is still alive (strict >).
    assert rec.effective_weight(1_000.0 + 400.0, "QUIET") > 0.0


def test_regime_shift_invalidates_immediately():
    rec = _rec()
    assert rec.effective_weight(1_000.0, "BREAKOUT") == 0.0
    assert rec.effective_weight(1_000.0, "NORMAL") == 0.0
    assert rec.effective_weight(1_000.0, "UNKNOWN") == 0.0
    assert rec.effective_weight(1_000.0, "QUIET") == pytest.approx(1.0)


def test_decay_monotonic_within_ttl():
    rec = _rec(ttl=400.0)
    ages = [0.0, 50.0, 100.0, 200.0, 300.0, 399.0]
    weights = [rec.effective_weight(1_000.0 + a, "QUIET") for a in ages]
    for earlier, later in zip(weights, weights[1:]):
        assert later < earlier
    assert all(w > 0.0 for w in weights)


def test_decay_half_life_at_half_ttl():
    rec = _rec(weight=1.0, ttl=400.0)
    # half_life = ttl * 0.5 = 200s -> at age 200 weight halves.
    assert rec.effective_weight(1_200.0, "QUIET") == pytest.approx(0.5)
    assert rec.effective_weight(1_400.0, "QUIET") == pytest.approx(0.25)


def test_future_timestamp_zero_weight():
    rec = _rec()
    assert rec.effective_weight(999.0, "QUIET") == 0.0


def test_record_fail_silent_garbage():
    rec = _rec()
    assert rec.effective_weight("not-a-number", "QUIET") == 0.0
    bad = vf.VetoRecord("x", float("nan"), float("nan"), "QUIET", 0.0)
    assert bad.effective_weight(1_000.0, "QUIET") == 0.0


# ── VetoRegistry ─────────────────────────────────────────────────────────────

def test_registry_uses_ttl_table_and_default():
    reg = vf.VetoRegistry()
    reg.record("quiet_market_pause", 1.0, "QUIET", now=1_000.0)
    rec = reg._records["quiet_market_pause"]
    assert rec.hard_ttl_s == pytest.approx(4.0 * 3600.0)
    reg.record("unlisted_veto", 1.0, "NORMAL", now=1_000.0)
    assert reg._records["unlisted_veto"].hard_ttl_s == pytest.approx(vf.DEFAULT_TTL_SECONDS)


def test_registry_fire_threshold():
    reg = vf.VetoRegistry(ttl_table={"quiet_market_pause": 400.0})
    reg.record("quiet_market_pause", 1.0, "QUIET", now=1_000.0)
    # age 0 -> weight 1.0 >= 0.40 fires.
    assert reg.fires("quiet_market_pause", 1_000.0, "QUIET")
    # weight at fire threshold: 0.5 ** (age/200) = 0.40 -> age ~264.4
    assert reg.fires("quiet_market_pause", 1_000.0 + 260.0, "QUIET")
    assert not reg.fires("quiet_market_pause", 1_000.0 + 270.0, "QUIET")
    # Absent veto never fires; wrong regime never fires.
    assert not reg.fires("low_winrate_regime", 1_000.0, "QUIET")
    assert not reg.fires("quiet_market_pause", 1_000.0, "BREAKOUT")


def test_registry_prune_drops_expired_only():
    reg = vf.VetoRegistry(ttl_table={"a": 100.0, "b": 10_000.0})
    reg.record("a", 1.0, "NORMAL", now=1_000.0)
    reg.record("b", 1.0, "NORMAL", now=1_000.0)
    dropped = reg.prune(now=1_000.0 + 200.0)
    assert dropped == 1
    assert "a" not in reg._records
    assert "b" in reg._records


def test_registry_latest_record_wins():
    reg = vf.VetoRegistry(ttl_table={"a": 400.0})
    reg.record("a", 1.0, "QUIET", now=1_000.0)
    reg.record("a", 1.0, "NORMAL", now=1_100.0)
    assert reg.effective_weight("a", 1_100.0, "NORMAL") == pytest.approx(1.0)
    assert reg.effective_weight("a", 1_100.0, "QUIET") == 0.0


def test_registry_fail_silent():
    reg = vf.VetoRegistry()
    reg.record("a", "garbage", "NORMAL", now=1_000.0)  # swallowed, not stored
    assert reg.effective_weight("a", 1_000.0, "NORMAL") == 0.0
    assert reg.effective_weight(None, 1_000.0, "NORMAL") == 0.0
    assert reg.prune(now="garbage") == 0


# ── classify_regime ──────────────────────────────────────────────────────────

def test_classifier_breakout_legs():
    assert vf.classify_regime({"hv_annualized_pct": 119.6}) == "BREAKOUT"
    assert vf.classify_regime({"oi_delta_24h_pct": 3.4}) == "BREAKOUT"
    assert vf.classify_regime({"funding_vs_avg_ratio": 1.83}) == "BREAKOUT"
    # Boundary values fire (>=).
    assert vf.classify_regime({"hv_annualized_pct": 100.0}) == "BREAKOUT"
    assert vf.classify_regime({"oi_delta_24h_pct": 2.0}) == "BREAKOUT"
    assert vf.classify_regime({"funding_vs_avg_ratio": 1.5}) == "BREAKOUT"


def test_classifier_breakout_precedence_over_quiet():
    m = {"hv_annualized_pct": 120.0, "volume_ratio": 0.3}
    assert vf.classify_regime(m) == "BREAKOUT"


def test_classifier_quiet():
    m = {"hv_annualized_pct": 25.0, "volume_ratio": 0.5}
    assert vf.classify_regime(m) == "QUIET"
    # Boundary: both legs strictly below.
    assert vf.classify_regime({"hv_annualized_pct": 39.9, "volume_ratio": 0.59}) == "QUIET"
    # Missing one leg -> cannot declare quiet.
    assert vf.classify_regime({"volume_ratio": 0.5}) == "NORMAL"
    assert vf.classify_regime({"hv_annualized_pct": 25.0}) == "NORMAL"
    # Just above a threshold -> normal.
    assert vf.classify_regime({"hv_annualized_pct": 40.0, "volume_ratio": 0.5}) == "NORMAL"
    assert vf.classify_regime({"hv_annualized_pct": 25.0, "volume_ratio": 0.6}) == "NORMAL"


def test_classifier_normal_and_unknown():
    assert vf.classify_regime({"hv_annualized_pct": 60.0, "volume_ratio": 1.1}) == "NORMAL"
    assert vf.classify_regime({}) == "UNKNOWN"
    assert vf.classify_regime({"hv_annualized_pct": None}) == "UNKNOWN"
    assert vf.classify_regime("not-a-dict") == "UNKNOWN"
    assert vf.classify_regime({"hv_annualized_pct": "garbage"}) == "UNKNOWN"
    assert vf.classify_regime({"hv_annualized_pct": float("nan")}) == "UNKNOWN"


# ── LOO attribution ──────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _loo_armed(monkeypatch):
    monkeypatch.setattr(vf.kill_switch, "enabled", lambda name: True)
    yield


def test_loo_unique_blocker_share_one(tmp_path):
    rep = vf.LooAttributionReporter(path=tmp_path / "loo.jsonl")
    rep.record_block("cand-1", ["quiet_market_pause"], 1_000.0, "UNI-USD", "long", 120.0)
    row = json.loads((tmp_path / "loo.jsonl").read_text().strip())
    assert row["shares"] == {"quiet_market_pause": 1.0}
    assert row["candidate_id"] == "cand-1"
    assert row["symbol"] == "UNI-USD"
    assert row["direction"] == "long"
    assert row["intended_notional"] == pytest.approx(120.0)


def test_loo_shared_blockers_split_evenly(tmp_path):
    rep = vf.LooAttributionReporter(path=tmp_path / "loo.jsonl")
    rep.record_block("cand-2", ["quiet_market_pause", "low_winrate_regime"],
                     1_000.0, "UNI-USD", "long", 120.0)
    row = json.loads((tmp_path / "loo.jsonl").read_text().strip())
    assert row["shares"] == {"quiet_market_pause": 0.5, "low_winrate_regime": 0.5}


def test_loo_jsonl_appends(tmp_path):
    p = tmp_path / "loo.jsonl"
    rep = vf.LooAttributionReporter(path=p)
    rep.record_block("c1", ["a"], 1.0, "S", "long", 10.0)
    rep.record_block("c2", ["a", "b", "c"], 2.0, "S", "short", 20.0)
    lines = p.read_text().strip().splitlines()
    assert len(lines) == 2
    rows = [json.loads(ln) for ln in lines]
    assert rows[0]["candidate_id"] == "c1"
    assert rows[1]["shares"] == {"a": pytest.approx(1 / 3),
                                 "b": pytest.approx(1 / 3),
                                 "c": pytest.approx(1 / 3)}


def test_loo_empty_fired_list_empty_shares():
    assert vf.LooAttributionReporter.responsibility_shares([]) == {}
    assert vf.LooAttributionReporter.responsibility_shares(None) == {}


def test_loo_fail_silent_unwritable_path(monkeypatch):
    rep = vf.LooAttributionReporter(path=Path("/proc/definitely/not/writable.jsonl"))
    # Must not raise.
    rep.record_block("c", ["a"], 1.0, "S", "long", 1.0)


def test_loo_fail_silent_garbage_args(tmp_path):
    rep = vf.LooAttributionReporter(path=tmp_path / "loo.jsonl")
    rep.record_block(None, None, "garbage", None, None, "garbage")


def test_loo_kill_switch_off_skips_write(tmp_path, monkeypatch):
    monkeypatch.setattr(vf.kill_switch, "enabled", lambda name: False)
    p = tmp_path / "loo.jsonl"
    rep = vf.LooAttributionReporter(path=p)
    rep.record_block("c", ["a"], 1.0, "S", "long", 1.0)
    assert not p.exists()


# ── kill-switch helpers ───────────────────────────────────────────────────────

def test_kill_switch_helpers_delegate(monkeypatch):
    seen = []
    monkeypatch.setattr(vf.kill_switch, "enabled",
                        lambda name: seen.append(name) or name == "veto_freshness")
    assert vf.freshness_enabled() is True
    assert vf.loo_enabled() is False
    assert seen == ["veto_freshness", "veto_loo_report"]


def test_kill_switch_helpers_fail_closed_or_safe(monkeypatch):
    def _boom(name):
        raise RuntimeError("kill switch unreadable")
    monkeypatch.setattr(vf.kill_switch, "enabled", _boom)
    assert vf.freshness_enabled() is False   # behavior-changing: fail closed
    assert vf.loo_enabled() is False         # shadow write skipped, never raises
