"""tests/test_axiom_wiring.py — pins for the main.py axiom splices.

Splice 2 (the gate, `_axiom_gate_decision`): bypass on kill switch / campaign /
non-crypto scope / dark param-store infrastructure; fail-OPEN on store read
errors (mechanism); fail-CLOSED reject on dark evidence, DEAD_ZONE-class
regimes, tier floor, and Kant failures; approve re-sizes to equity ×
margin_fraction(tier, Kelly) × regime leverage_mult with the rotation tilt
binding tiers 1-2 ONLY and the 30% symbol exposure cap as the ceiling.
Nietzsche rides as telemetry, never sizing.

Splice 1 (`_axiom_evidence_tick`): per-symbol score → tier → Kant verdict
published to param_store under axiom:* keys with a TTL, append-only jsonl,
atomic json snapshot, and a per-tick result census. A dark proxy scores 0
(Kant whale_floor) and lands in the dark list — evidence never mints a tier
from nothing.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import main as aria_main


# ── Stubs ─────────────────────────────────────────────────────────────────────

class _Store:
    """Dict-backed param_store stub (get_ai_param/set_ai_param contract)."""

    def __init__(self, initial=None, raise_on_read=False):
        self.data = dict(initial or {})
        self.raise_on_read = raise_on_read
        self.sets = []

    def get_ai_param(self, key):
        if self.raise_on_read:
            raise RuntimeError("store broken")
        return self.data.get(key)

    def set_ai_param(self, key, value, ttl_seconds=None):
        self.sets.append((key, value, ttl_seconds))
        self.data[key] = value


class _Proxy:
    def __init__(self, score=0.9, tier=1, notes=""):
        self.score = score
        self.tier = tier
        self.components = {"funding": 0.5}
        self.notes = notes


class _ProxyFeed:
    def __init__(self, proxy=None, raise_on_score=False):
        self.proxy = proxy
        self.raise_on_score = raise_on_score

    def score(self, sym):
        if self.raise_on_score:
            raise RuntimeError("feed broken")
        return self.proxy


def _candidate(symbol="BTC-USD", entry=100.0, size=1.0, lev=5.0):
    return SimpleNamespace(symbol=symbol, entry_price=entry, size=size,
                           leverage=lev, initial_margin=0.0)


def _cfg(**over):
    base = dict(axiom_gate_enabled=True, axiom_sizing_enabled=True,
                axiom_symbol_exposure_cap=0.30)
    base.update(over)
    return SimpleNamespace(**base)


def _lit_store(tier=1, kant_ok=True, regime="NORMAL", score=0.9,
               failures=(), tilt=None):
    data = {
        "axiom:tier:BTC-USD": tier,
        "axiom:kant_ok:BTC-USD": kant_ok,
        "axiom:regime": regime,
        "axiom:score:BTC-USD": score,
        "axiom:kant_failures:BTC-USD": list(failures),
    }
    if tilt is not None:
        data["equity_flow:rotation_tilt"] = {"size_tilt": tilt}
    return _Store(data)


def _decide(store=None, cand=None, cfg=None, **kw):
    args = dict(symbol="BTC-USD", side="long",
                candidate=cand if cand is not None else _candidate(),
                cfg=cfg if cfg is not None else _cfg(),
                param_store=store if store is not None else _lit_store(),
                equity=1000.0, kelly_stats=(0, 0, 0.0, 0.0),
                is_campaign=False, asset_class="crypto",
                bybit_symbols={"BTC-USD": "BTCUSDT"})
    args.update(kw)
    return aria_main._axiom_gate_decision(**args), args["candidate"]


# Prior-Kelly constants (n=0 → full_kelly(0.55, 2.0) = 0.325):
#   tier1 margin frac = 0.325×0.5   = 0.1625  → $162.50 on $1000
#   tier3 margin frac = 0.325×0.25  = 0.08125 → $81.25
T1_MARGIN = 162.5
T3_MARGIN = 81.25


# ── Bypass plane (legacy bit-for-bit) ─────────────────────────────────────────

class TestBypass:
    def test_kill_switch_env(self, monkeypatch):
        monkeypatch.setenv("AXIOM_STACK_ENABLED", "false")
        rec, cand = _decide()
        assert rec["action"] == "bypass"
        assert rec["reason"] == "kill_switch_env"
        assert cand.size == 1.0

    def test_kill_switch_cfg(self, monkeypatch):
        monkeypatch.delenv("AXIOM_STACK_ENABLED", raising=False)
        rec, cand = _decide(cfg=_cfg(axiom_gate_enabled=False))
        assert rec["action"] == "bypass"
        assert rec["reason"] == "kill_switch_cfg"
        assert cand.size == 1.0

    def test_campaign_exempt(self):
        rec, cand = _decide(is_campaign=True)
        assert rec["action"] == "bypass"
        assert rec["reason"] == "campaign_exempt"
        assert cand.size == 1.0

    def test_scope_bypass_equity(self):
        rec, _ = _decide(asset_class="equity")
        assert rec["action"] == "bypass"
        assert rec["reason"] == "scope_bypass"

    def test_scope_bypass_symbol_unmapped(self):
        rec, _ = _decide(symbol="SPCX-USD", bybit_symbols={})
        assert rec["action"] == "bypass"
        assert rec["reason"] == "scope_bypass"

    def test_param_store_dark_bypass(self):
        rec, _ = _decide(param_store=None)
        assert rec["action"] == "bypass"
        assert rec["reason"] == "param_store_dark"

    def test_param_store_read_error_fails_open(self):
        rec, cand = _decide(store=_Store(raise_on_read=True))
        assert rec["action"] == "bypass"
        assert rec["reason"] == "param_store_read_error"
        assert cand.size == 1.0


# ── Reject plane (fail-closed on evidence) ────────────────────────────────────

class TestReject:
    def test_evidence_dark_reject(self):
        rec, cand = _decide(store=_Store({}))       # tier/kant/regime all None
        assert rec["action"] == "reject"
        assert rec["reason"] == "axiom_evidence_dark"
        assert cand.size == 1.0

    def test_dead_zone_reject(self):
        rec, _ = _decide(store=_lit_store(regime="DEAD_ZONE"))
        assert rec["action"] == "reject"
        assert rec["reason"] == "DEAD_ZONE"

    def test_unknown_regime_reject(self):
        rec, _ = _decide(store=_lit_store(regime="SOME_FUTURE_STATE"))
        assert rec["action"] == "reject"

    def test_tier_floor_reject(self):
        rec, cand = _decide(store=_lit_store(tier=0))
        assert rec["action"] == "reject"
        assert rec["reason"] == "tier_floor"
        assert cand.size == 1.0

    def test_kant_failed_reject_names_failure(self):
        rec, _ = _decide(store=_lit_store(tier=2, kant_ok=False,
                                          failures=["coherence_floor"]))
        assert rec["action"] == "reject"
        assert rec["reason"] == "coherence_floor"


# ── Approve plane (Kelly-by-tier sizing) ──────────────────────────────────────

class TestApprove:
    def test_tier1_prior_kelly_math(self):
        rec, cand = _decide(store=_lit_store(tier=1))
        assert rec["action"] == "approve"
        assert rec["sized"] is True
        assert rec["nietzsche"] == 2.5            # telemetry only
        assert rec["margin_frac"] == pytest.approx(0.1625)
        assert cand.initial_margin == pytest.approx(T1_MARGIN)
        assert cand.size == pytest.approx(T1_MARGIN * 5.0 / 100.0)

    def test_tilt_binds_tier1(self):
        rec, cand = _decide(store=_lit_store(tier=1, tilt=1.15))
        assert rec["tilt"] == 1.15
        assert cand.initial_margin == pytest.approx(T1_MARGIN * 1.15)
        assert cand.size == pytest.approx(T1_MARGIN * 1.15 * 5.0 / 100.0)

    def test_tilt_binds_tier2(self):
        rec, cand = _decide(store=_lit_store(tier=2, tilt=1.15))
        t2 = 1000.0 * (0.325 / 3.0)
        assert rec["tilt"] == 1.15
        assert cand.initial_margin == pytest.approx(t2 * 1.15)

    def test_tilt_never_tier3(self):
        rec, cand = _decide(store=_lit_store(tier=3, tilt=1.15))
        assert rec["action"] == "approve"
        assert rec["tilt"] is None
        assert cand.initial_margin == pytest.approx(T3_MARGIN)

    def test_exposure_cap_is_ceiling(self):
        rec, cand = _decide(store=_lit_store(tier=1, tilt=5.0))
        assert cand.initial_margin == pytest.approx(300.0)   # 30% of $1000
        assert rec["cap_frac"] == pytest.approx(0.30)
        assert cand.size == pytest.approx(15.0)

    def test_post_cascade_half_size(self):
        rec, cand = _decide(store=_lit_store(tier=1, regime="POST_CASCADE"))
        assert rec["action"] == "approve"
        assert rec["leverage_mult"] == 0.5
        assert cand.initial_margin == pytest.approx(T1_MARGIN * 0.5)

    def test_sizing_disabled_approves_unsized(self):
        rec, cand = _decide(cfg=_cfg(axiom_sizing_enabled=False))
        assert rec["action"] == "approve"
        assert rec["sized"] is False
        assert cand.size == 1.0                  # legacy candidate untouched

    def test_bad_equity_approves_unsized(self):
        rec, cand = _decide(equity=0.0)
        assert rec["action"] == "approve"
        assert rec["sized"] is False
        assert cand.size == 1.0


# ── Kelly stats pooling ───────────────────────────────────────────────────────

class TestKellyStats:
    def test_pools_personalities(self):
        perf = SimpleNamespace(get_all_stats=lambda: {
            "TREND": SimpleNamespace(wins=8, losses=2, avg_win_r=2.0,
                                     avg_loss_r=1.0),
            "FLOW": SimpleNamespace(wins=2, losses=8, avg_win_r=3.0,
                                    avg_loss_r=0.5),
        })
        w, l, aw, al = aria_main._axiom_kelly_stats(perf)
        assert (w, l) == (10, 10)
        assert aw == pytest.approx((8 * 2.0 + 2 * 3.0) / 10)
        assert al == pytest.approx((2 * 1.0 + 8 * 0.5) / 10)

    def test_none_perf_falls_back_to_priors(self):
        assert aria_main._axiom_kelly_stats(None) == (0, 0, 0.0, 0.0)

    def test_broken_perf_never_raises(self):
        perf = SimpleNamespace(get_all_stats=lambda: 1 / 0)
        assert aria_main._axiom_kelly_stats(perf) == (0, 0, 0.0, 0.0)


# ── Evidence tick (Splice 1) ──────────────────────────────────────────────────

class TestEvidenceTick:
    def _tick(self, tmp_path, feed=None, store=None, coh=6.0):
        return aria_main._axiom_evidence_tick(
            symbols=["BTC-USD"], proxy_feed=feed, param_store=store,
            coherence_fn=lambda s: coh,
            funding_fn=lambda s: (1.0, 2.0),
            regime="NORMAL", now_ts=1_800_000_000.0, ttl_s=600,
            jsonl_path=str(tmp_path / "ev.jsonl"),
            json_path=str(tmp_path / "ev.json")), store

    def test_publishes_axiom_keys(self, tmp_path):
        out, store = self._tick(tmp_path, feed=_ProxyFeed(_Proxy()),
                                store=_Store())
        assert out == {"scored": 1, "dark": [], "published": 1,
                       "regime": "NORMAL"}
        assert store.data["axiom:tier:BTC-USD"] == 1
        assert store.data["axiom:kant_ok:BTC-USD"] is True
        assert store.data["axiom:score:BTC-USD"] == pytest.approx(0.9)
        assert store.data["axiom:regime"] == "NORMAL"
        assert store.data["axiom:kant_failures:BTC-USD"] == []
        assert all(t[2] == 600 for t in store.sets)

    def test_jsonl_and_atomic_snapshot(self, tmp_path):
        self._tick(tmp_path, feed=_ProxyFeed(_Proxy()), store=_Store())
        rows = [json.loads(l) for l in
                open(tmp_path / "ev.jsonl").read().splitlines()]
        assert len(rows) == 1
        assert rows[0]["symbol"] == "BTC-USD"
        assert "rr_actual" in rows[0]["assumed"]
        snap = json.load(open(tmp_path / "ev.json"))
        assert snap["regime"] == "NORMAL"
        assert "BTC-USD" in snap["symbols"]

    def test_dark_proxy_fails_closed(self, tmp_path):
        out, store = self._tick(tmp_path, feed=_ProxyFeed(None),
                                store=_Store())
        assert out["scored"] == 1
        assert out["dark"] == ["BTC-USD"]
        assert store.data["axiom:tier:BTC-USD"] == 0
        assert store.data["axiom:kant_ok:BTC-USD"] is False
        assert "whale_floor" in store.data["axiom:kant_failures:BTC-USD"]

    def test_broken_feed_scores_dark_never_raises(self, tmp_path):
        out, _ = self._tick(tmp_path,
                            feed=_ProxyFeed(raise_on_score=True),
                            store=_Store())
        assert out["dark"] == ["BTC-USD"]

    def test_dark_coherence_fails_kant(self, tmp_path):
        out, store = self._tick(tmp_path, feed=_ProxyFeed(_Proxy()),
                                store=_Store(), coh=None)
        assert store.data["axiom:kant_ok:BTC-USD"] is False
        assert "coherence_floor" in \
            store.data["axiom:kant_failures:BTC-USD"]
