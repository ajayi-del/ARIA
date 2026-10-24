"""tests/test_squeeze_splice.py — pins for the squeeze radar + playbook
SHADOW splice (2026-10-24): main.py wiring of intelligence/squeeze_radar.py
+ intelligence/squeeze_playbook.py + data/coingecko_supply.py. Telemetry +
the shadow ledger ONLY — zero trade-path surface. Kill switch OFF = the
loop body never runs (pre-splice system bit-for-bit).

Conventions follow tests/test_main_fast_cycle_splice.py: behavioral pins on
the importable module-level helpers, source pins on main.py for the closure
wiring that cannot be booted without main().
"""
import asyncio
import json
import os
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as m  # noqa: E402
from data.coingecko_supply import CoinGeckoSupplyFeed  # noqa: E402
from data.orderbook_store import OrderbookStore  # noqa: E402
from intelligence.squeeze_playbook import (  # noqa: E402
    PlaybookConfig, SqueezePlaybook)
from intelligence.squeeze_radar import SqueezeRadar  # noqa: E402
from structlog.testing import capture_logs  # noqa: E402

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = os.path.join(_REPO, "main.py")

_BYBIT_MAP = {"BTC-USD": "BTCUSDT", "ETH-USD": "ETHUSDT", "SPCX-USD": "unknown"}
_NOW = 1_790_000_000.0


def _main_src() -> str:
    with open(_MAIN_SRC) as fh:
        return fh.read()


class _MarkStore:
    def __init__(self, price, healthy=True):
        self.mark_price = price
        self._healthy = healthy

    def is_healthy(self, max_age_ms):
        return self._healthy


class _RatioFeed:
    def __init__(self, ratio):
        self._ratio = ratio
        self.seen = []

    def get_reading(self, symbol):
        self.seen.append(symbol)
        if self._ratio is None:
            return None
        return SimpleNamespace(ratio=self._ratio)


def _ob_store(bids, asks, healthy=True):
    st = OrderbookStore("BTC-USD")
    now_ms = int(time.time() * 1000)
    st.update(bids, asks, now_ms if healthy else now_ms - 600_000)
    return st


def _supply_feed(tmp_path, supply=1e7):
    log_dir = tmp_path / "cg"
    log_dir.mkdir(exist_ok=True)
    (log_dir / "coingecko_supply.json").write_text(json.dumps({
        "fetched_at": time.time(),
        "rows": {"BTC": {"supply_usd": supply, "mcap": supply, "cg_id": "bitcoin"},
                 "ETH": {"supply_usd": supply, "mcap": supply, "cg_id": "ethereum"}}}))
    return CoinGeckoSupplyFeed(log_dir=str(log_dir))


def _planes(tmp_path, **over):
    """Default full-plane bundle for the gather/pass; over rides any leg."""
    p = dict(
        mark_price_stores={"BTC-USD": _MarkStore(100.0)},
        live_funding={"BTC-USD": -9e-5},
        ticker_stores={"BTC-USD": {"open_interest": 5.1e6}},
        bybit_map=_BYBIT_MAP,
        supply_feed=_supply_feed(tmp_path),
        ratio_feed=_RatioFeed(0.5),
        orderbook_stores={"BTC-USD": _ob_store(
            [(99.99, 10.0), (99.98, 5.0)], [(100.01, 10.0), (100.02, 5.0)])},
        macro_secs_fn=lambda: 3600.0,
    )
    p.update(over)
    return p


def _clear_env(monkeypatch):
    for var in ("SQUEEZE_PLAYBOOK_SHADOW_ENABLED", "SQUEEZE_RADAR_ENABLED",
                "SQUEEZE_PLAYBOOK_ENABLED", "COINGECKO_SUPPLY_ENABLED"):
        monkeypatch.delenv(var, raising=False)


# ── (a) Kill switch OFF → loop body never runs ──────────────────────────────

class TestKillSwitch:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("SQUEEZE_PLAYBOOK_SHADOW_ENABLED", raising=False)
        assert m.squeeze_playbook_shadow_enabled() is True

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv("SQUEEZE_PLAYBOOK_SHADOW_ENABLED", "false")
        assert m.squeeze_playbook_shadow_enabled() is False

    def test_env_read_at_call_time(self, monkeypatch):
        monkeypatch.setenv("SQUEEZE_PLAYBOOK_SHADOW_ENABLED", "false")
        assert m.squeeze_playbook_shadow_enabled() is False
        monkeypatch.setenv("SQUEEZE_PLAYBOOK_SHADOW_ENABLED", "true")
        assert m.squeeze_playbook_shadow_enabled() is True

    def test_pass_never_scans_when_off(self, monkeypatch, tmp_path):
        _clear_env(monkeypatch)
        monkeypatch.setenv("SQUEEZE_PLAYBOOK_SHADOW_ENABLED", "false")
        calls = []
        radar = SimpleNamespace(
            scan=lambda snaps: calls.append(snaps) or [])
        playbook = SqueezePlaybook(PlaybookConfig())
        log_path = tmp_path / "squeeze_playbook.jsonl"
        with capture_logs() as logs:
            out = m._squeeze_radar_pass(
                symbols=["BTC-USD"], radar=radar, playbook=playbook,
                arcus_assets=set(), log_path=str(log_path), now_s=_NOW,
                **_planes(tmp_path))
        assert out == {"n_symbols": 0, "n_verdicts": 0, "n_intents": 0}
        assert calls == []                       # the body never ran
        assert not log_path.exists()             # zero state touched
        assert [e for e in logs
                if e.get("event", "").startswith("squeeze_")] == []

    def test_loop_source_pins(self):
        src = _main_src()
        assert "if squeeze_playbook_shadow_enabled():" in src
        assert "_supervise(_squeeze_radar_loop," in src
        assert "asyncio.to_thread(_cg_supply_feed.refresh_if_stale)" in src
        assert 'CoinGeckoSupplyFeed(\n        log_dir=getattr(config, "log_dir", "logs"))' in src


# ── (b) Snapshot gathering: plane mapping + None propagation ────────────────

class TestSnapshotGather:
    def test_full_planes_map_all_fields(self, monkeypatch, tmp_path):
        _clear_env(monkeypatch)
        ratio = _RatioFeed(0.5)
        row = m._squeeze_snapshot_for(
            "BTC-USD", **_planes(tmp_path, ratio_feed=ratio))
        assert row is not None
        assert row["symbol"] == "BTC-USD"
        assert row["mark_price"] == 100.0
        assert row["funding_rate"] == -9e-5
        assert row["oi_usd"] == 5.1e6
        assert row["supply_usd"] == 1e7
        assert row["long_short_ratio"] == 0.5
        assert ratio.seen == ["BTCUSDT"]         # bybit-mapped read
        assert row["spread_bps"] is not None and row["spread_bps"] > 0
        # depth within 10bps of mid 100.0: all four levels qualify
        assert row["depth_usd_10bps"] == 99.99 * 10 + 99.98 * 5 \
            + 100.01 * 10 + 100.02 * 5
        assert row["macro_window"] is True       # 1h from a print (< ±3d)
        assert row["oi_growth_3h"] is None       # daily-bar plane abstains
        assert row["volume_spike"] is None       # no rolling-ratio plane
        assert row["listing_age_days"] is None

    def test_dark_planes_abstain_per_field(self, monkeypatch, tmp_path):
        _clear_env(monkeypatch)
        row = m._squeeze_snapshot_for(
            "BTC-USD",
            mark_price_stores={"BTC-USD": _MarkStore(100.0)},
            live_funding={}, ticker_stores={}, bybit_map=_BYBIT_MAP,
            supply_feed=None, ratio_feed=None, orderbook_stores={},
            macro_secs_fn=None)
        assert row is not None                   # mark alive = row survives
        assert row["funding_rate"] is None
        assert row["oi_usd"] is None
        assert row["supply_usd"] is None
        assert row["long_short_ratio"] is None
        assert row["spread_bps"] is None
        assert row["depth_usd_10bps"] is None
        assert row["macro_window"] is False

    def test_non_crypto_symbol_skipped(self, monkeypatch, tmp_path):
        _clear_env(monkeypatch)
        p = _planes(tmp_path)
        p["mark_price_stores"] = {"SPCX-USD": _MarkStore(140.0)}
        assert m._squeeze_snapshot_for("SPCX-USD", **p) is None

    def test_dark_mark_drops_symbol(self, monkeypatch, tmp_path):
        _clear_env(monkeypatch)
        p = _planes(tmp_path)
        p["mark_price_stores"] = {"BTC-USD": _MarkStore(100.0, healthy=False)}
        assert m._squeeze_snapshot_for("BTC-USD", **p) is None
        p["mark_price_stores"] = {"BTC-USD": _MarkStore(0.0)}
        assert m._squeeze_snapshot_for("BTC-USD", **p) is None
        p["mark_price_stores"] = {}
        assert m._squeeze_snapshot_for("BTC-USD", **p) is None

    def test_funding_falls_back_to_ticker_store(self, monkeypatch, tmp_path):
        _clear_env(monkeypatch)
        p = _planes(tmp_path, live_funding={},
                    ticker_stores={"BTC-USD": {"funding_rate": -5e-5,
                                               "open_interest": 5.1e6}})
        row = m._squeeze_snapshot_for("BTC-USD", **p)
        assert row["funding_rate"] == -5e-5
        assert row["oi_usd"] == 5.1e6

    def test_unhealthy_or_empty_book_abstains(self, monkeypatch, tmp_path):
        _clear_env(monkeypatch)
        p = _planes(tmp_path, orderbook_stores={
            "BTC-USD": _ob_store([(99.99, 1.0)], [(100.01, 1.0)],
                                 healthy=False)})
        row = m._squeeze_snapshot_for("BTC-USD", **p)
        assert row["spread_bps"] is None
        assert row["depth_usd_10bps"] is None
        p["orderbook_stores"] = {"BTC-USD": _ob_store([], [], healthy=True)}
        row = m._squeeze_snapshot_for("BTC-USD", **p)
        assert row["spread_bps"] is None         # 9999.0 sentinel filtered
        assert row["depth_usd_10bps"] is None

    def test_gather_never_raises(self, monkeypatch, tmp_path):
        _clear_env(monkeypatch)

        class _Boom:
            def get(self, *a, **k):
                raise RuntimeError("boom")

        row = m._squeeze_snapshot_for(
            "BTC-USD", mark_price_stores=_Boom(), live_funding=_Boom(),
            ticker_stores=_Boom(), bybit_map=_Boom(), supply_feed=None,
            ratio_feed=None, orderbook_stores=_Boom(), macro_secs_fn=None)
        assert row is None


# ── (c) Synthetic tier-3 → radar verdict + playbook intent row ──────────────

class TestTier3Pass:
    def test_tier3_produces_verdict_and_intent_row(self, monkeypatch, tmp_path):
        _clear_env(monkeypatch)
        log_path = tmp_path / "squeeze_playbook.jsonl"
        radar = SqueezeRadar()
        playbook = SqueezePlaybook(PlaybookConfig())
        with capture_logs() as logs:
            out = m._squeeze_radar_pass(
                symbols=["BTC-USD"], radar=radar, playbook=playbook,
                arcus_assets=set(), log_path=str(log_path), now_s=_NOW,
                **_planes(tmp_path))
        assert out["n_symbols"] == 1
        assert out["n_verdicts"] == 1
        assert out["n_intents"] == 1            # IDLE -> WATCHING "watch"
        rows = [json.loads(ln) for ln in log_path.read_text().splitlines()]
        scan = [r for r in rows if r["kind"] == "scan"]
        intents = [r for r in rows if r["kind"] == "intent"]
        assert len(scan) == 1 and len(intents) == 1
        assert scan[0]["verdicts"][0]["symbol"] == "BTC-USD"
        assert scan[0]["verdicts"][0]["tier"] == 3
        assert scan[0]["verdicts"][0]["oi_to_float"] == 0.51
        assert intents[0]["symbol"] == "BTC-USD"
        assert intents[0]["intent"] == "watch"
        assert intents[0]["state_from"] == "IDLE"
        assert intents[0]["state_to"] == "WATCHING"
        assert intents[0]["venue_hint"] == "none"
        scanned = [e for e in logs if e.get("event") == "squeeze_radar_scanned"]
        fired = [e for e in logs if e.get("event") == "squeeze_playbook_intent"]
        assert len(scanned) == 1
        assert scanned[0]["n_symbols"] == 1
        assert scanned[0]["n_verdicts"] == 1
        assert scanned[0]["max_tier"] == 3
        assert len(fired) == 1
        assert fired[0]["kind"] == "watch"
        assert fired[0]["state_from"] == "IDLE"
        assert fired[0]["state_to"] == "WATCHING"

    def test_shadow_only_no_gate_state(self, monkeypatch, tmp_path):
        _clear_env(monkeypatch)
        playbook = SqueezePlaybook(PlaybookConfig())
        m._squeeze_radar_pass(
            symbols=["BTC-USD"], radar=SqueezeRadar(), playbook=playbook,
            arcus_assets=set(), log_path=str(tmp_path / "sp.jsonl"),
            now_s=_NOW, **_planes(tmp_path))
        ctx = playbook.state_snapshot()["contexts"]["BTC-USD"]
        assert ctx["state"] == "WATCHING"        # brain state only, no orders


# ── (d) A raising pass logs squeeze_radar_loop_error and the loop survives ──

class TestErrorSurvival:
    def test_raising_gather_logs_and_survives(self, monkeypatch, tmp_path):
        _clear_env(monkeypatch)

        def _boom(snaps):
            raise RuntimeError("gather exploded")

        kwargs = dict(symbols=["BTC-USD"],
                      radar=SimpleNamespace(scan=_boom),
                      playbook=SqueezePlaybook(PlaybookConfig()),
                      arcus_assets=set(),
                      log_path=str(tmp_path / "sp.jsonl"), now_s=_NOW,
                      **_planes(tmp_path))
        with capture_logs() as logs:
            out = asyncio.run(m._squeeze_radar_tick(**kwargs))
        assert out == {}
        errs = [e for e in logs if e.get("event") == "squeeze_radar_loop_error"]
        assert len(errs) == 1
        assert "gather exploded" in errs[0]["error"]
        # the loop survives: the next pass runs clean
        out2 = asyncio.run(m._squeeze_radar_tick(
            **{**kwargs, "radar": SqueezeRadar()}))
        assert out2["n_symbols"] == 1
        assert out2["n_verdicts"] == 1
