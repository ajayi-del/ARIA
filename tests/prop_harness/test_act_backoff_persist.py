"""L8/L10 latch persistence pins (CEO audit S4, 2026-11-17).

Pins: the ActBackoff 429 latch survives a simulated restart while its
expiry is in the future; expired latch state is never honored on boot;
expired post_win_respend / post_win_cooloff entries are pruned on the
read path so stale respend_committed values never cap fresh budgets;
kill switches OFF = legacy bit-for-bit; corrupt state = fresh fail-open
(post_loss_cooloff policy).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from prop_harness.brains.act_backoff import ActBackoff
from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import DrawdownBrain
from prop_harness.config import PropConfig
from prop_harness.executor import ProprExecutor


def _events(tmp_path: Path) -> list[dict]:
    path = tmp_path / "journal" / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()
            if line.strip()]


class _FakeClient:
    account_id = "urn:prp-account:x"

    def get_open_orders(self, base=None):
        return []

    def place_orders(self, body):
        return [{"orderId": "urn:prp-order:x", **body["orders"][0]}]

    def cancel_order(self, order_id):
        return {}


def _executor(tmp_path, **cfg_kw):
    cfg = PropConfig(enabled=True, state_root=tmp_path, **cfg_kw)
    (tmp_path / "journal").mkdir(exist_ok=True)
    budget = BudgetBrain(tmp_path / "budget.json")
    budget.roll(headroom=150.0)
    dd = DrawdownBrain(tmp_path / "dd.json", floor_line=4850.0,
                       floor_usd=150.0)
    return ProprExecutor(cfg, _FakeClient(), budget, dd)


class TestActBackoffPersist:
    def test_latch_survives_restart_within_ttl(self, tmp_path):
        ex1 = _executor(tmp_path)
        now = time.time()
        for _ in range(3):  # latch_after default 3
            payload = ex1.act_backoff.note_rate_limit("resize:a", now=now)
        assert payload["type"] == "act_backoff_latched"
        assert ex1.act_backoff.blocked("resize:a", now=now)
        assert (tmp_path / "act_backoff.json").is_file()

        # Simulated restart: fresh executor, same state root.
        ex2 = _executor(tmp_path)
        assert ex2.act_backoff.blocked("resize:a")
        restored = [e for e in _events(tmp_path)
                    if e.get("type") == "act_backoff_restored"]
        assert restored and restored[-1]["keys"] >= 1

    def test_expired_latch_not_honored(self, tmp_path):
        past = time.time() - 60.0
        (tmp_path / "act_backoff.json").write_text(json.dumps({
            "next_ok": {"resize:a": past},
            "fails": {"resize:a": 5},
            "latched": ["resize:a"],
            "account_until": past,
            "account_fails": 4,
        }))
        ex = _executor(tmp_path)
        assert not ex.act_backoff.blocked("resize:a")
        assert ex.act_backoff.export_state()["latched"] == []
        restored = [e for e in _events(tmp_path)
                    if e.get("type") == "act_backoff_restored"]
        assert restored == []  # nothing live → no boot event

    def test_corrupt_state_fails_open_fresh(self, tmp_path):
        (tmp_path / "act_backoff.json").write_text("{not json")
        ex = _executor(tmp_path)
        assert not ex.act_backoff.blocked("resize:a")
        assert not ex.act_backoff.blocked("")

    def test_kill_switch_off_is_legacy_memory_only(self, tmp_path):
        ex1 = _executor(tmp_path, act_backoff_persist_enabled=False)
        now = time.time()
        for _ in range(3):
            ex1.act_backoff.note_rate_limit("resize:a", now=now)
        assert not (tmp_path / "act_backoff.json").exists()
        # Restart: nothing persisted → nothing honored.
        ex2 = _executor(tmp_path, act_backoff_persist_enabled=False)
        assert not ex2.act_backoff.blocked("resize:a")


class TestStateTtlPrune:
    def test_expired_respend_latch_pruned_on_read(self, tmp_path):
        latch = {"armed": True, "expires_at": time.time() - 10.0,
                 "respend_committed": 12.5, "max_respend_usd": 15.0}
        (tmp_path / "post_win_respend.json").write_text(json.dumps(latch))
        ex = _executor(tmp_path)
        assert ex._respend_latch() == {}

    def test_respend_prune_kill_off_returns_raw(self, tmp_path):
        latch = {"armed": True, "expires_at": time.time() - 10.0,
                 "respend_committed": 12.5, "max_respend_usd": 15.0}
        (tmp_path / "post_win_respend.json").write_text(json.dumps(latch))
        ex = _executor(tmp_path, state_ttl_prune_enabled=False)
        assert ex._respend_latch()["respend_committed"] == 12.5

    def test_live_respend_latch_survives(self, tmp_path):
        latch = {"armed": True, "expires_at": time.time() + 600.0,
                 "respend_committed": 3.0, "max_respend_usd": 15.0}
        (tmp_path / "post_win_respend.json").write_text(json.dumps(latch))
        ex = _executor(tmp_path)
        assert ex._respend_latch()["respend_committed"] == 3.0

    def test_expired_cooloff_entries_pruned_on_read(self, tmp_path):
        future = time.time() + 600.0
        past = time.time() - 600.0
        latches = {
            "BTC:long": {"armed": True, "expires_at": past},
            "ETH:long": {"armed": True, "expires_at": future},
        }
        (tmp_path / "post_win_cooloff.json").write_text(json.dumps(latches))
        ex = _executor(tmp_path)
        out = ex._cooloff_latches()
        assert list(out) == ["ETH:long"]

    def test_cooloff_prune_kill_off_returns_raw(self, tmp_path):
        past = time.time() - 600.0
        latches = {"BTC:long": {"armed": True, "expires_at": past}}
        (tmp_path / "post_win_cooloff.json").write_text(json.dumps(latches))
        ex = _executor(tmp_path, state_ttl_prune_enabled=False)
        assert "BTC:long" in ex._cooloff_latches()


class TestBrainRoundTrip:
    def test_export_load_roundtrip_pure(self):
        b1 = ActBackoff(enabled=True, initial_s=10.0, max_s=80.0,
                        latch_after=2)
        now = time.time()
        b1.note_rate_limit("resize:a", now=now)
        b1.note_rate_limit("resize:a", now=now)
        state = b1.export_state()
        b2 = ActBackoff(enabled=True, initial_s=10.0, max_s=80.0,
                        latch_after=2)
        summary = b2.load_state(state, now=now)
        assert summary["keys"] == 1
        assert summary["latched"] == 1
        assert summary["account"] is True
        assert b2.blocked("resize:a", now=now)
        # NaN / garbage entries are dropped, never honored.
        b3 = ActBackoff()
        summary = b3.load_state(
            {"next_ok": {"x": float("nan"), "y": "junk",
                         "z": now + 50.0},
             "fails": {"x": 3, "z": 2}, "latched": ["x", "z"],
             "account_until": "junk"}, now=now)
        assert summary["keys"] == 1
        assert summary["latched"] == 1
        assert summary["account"] is False
        assert b3.blocked("z", now=now)
