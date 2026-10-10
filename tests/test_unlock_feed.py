"""Token-unlock data plane pins (2026-10-10, Governor order wave-1).

Pure brains (normalize_index / catalyst_rows / merge_catalyst_store /
upcoming) + fetcher day discipline + store atomicity/merge doctrine +
wiring pins (supervision, shadow hook, kill-switch defaults)."""
import asyncio
import json
import os

import pytest

from data.unlock_feed import (UnlockFeed, normalize_index, catalyst_rows,
                              merge_catalyst_store, _FEED_SOURCE)

NOW = 1_800_000_000.0   # 2027-01-15-ish epoch fixture


def _row(slug, ticker, px, circ, amount, ts, name=None):
    return {
        "protocolSlug": slug, "name": name or slug, "circSupply": circ,
        "tokenPrice": [{"price": px, "symbol": ticker, "timestamp": NOW}],
        "nextEventV2": {"date": ts, "unlockType": "cliff", "amount": amount},
    }


class TestNormalize:
    def test_basic_mapping(self):
        rows = [_row("aptos", "APT", 0.82, 864_805_144.0, 3_210_144.66,
                     NOW + 3 * 86400)]
        out = normalize_index(rows, {"APT-USD"}, NOW)
        assert "APT-USD" in out
        u = out["APT-USD"]
        assert u["unlock_ts"] == NOW + 3 * 86400
        assert u["amount_usd"] == pytest.approx(3_210_144.66 * 0.82)
        assert u["pct_supply"] == pytest.approx(
            3_210_144.66 / 864_805_144.0 * 100.0)

    def test_thousand_prefix_mapping(self):
        rows = [_row("pepe", "PEPE", 0.00001, 4.2e14, 1e12, NOW + 86400)]
        out = normalize_index(rows, {"1000PEPE-USD"}, NOW)
        assert "1000PEPE-USD" in out

    def test_out_of_universe_skipped(self):
        rows = [_row("aptos", "APT", 0.82, 1e9, 1e6, NOW + 86400)]
        assert normalize_index(rows, {"BTC-USD"}, NOW) == {}

    def test_no_price_skipped(self):
        r = _row("arbitrum-exchange", "ARB", None, 1e9, 1e6, NOW + 86400)
        r["tokenPrice"] = []
        assert normalize_index([r], {"ARB-USD"}, NOW) == {}

    def test_past_and_beyond_horizon_skipped(self):
        rows = [_row("aptos", "APT", 1.0, 1e9, 1e6, NOW - 60),
                _row("sui-foundation", "SUI", 1.0, 1e9, 1e6, NOW + 90 * 86400)]
        assert normalize_index(rows, {"APT-USD", "SUI-USD"}, NOW) == {}

    def test_no_next_event_skipped(self):
        r = _row("jupiter", "JUP", 0.38, 1e9, 1e6, NOW + 86400)
        r["nextEventV2"] = None
        r["nextEvent"] = None
        assert normalize_index([r], {"JUP-USD"}, NOW) == {}

    def test_ambiguous_ticker_keeps_larger_usd(self):
        rows = [_row("a", "SUI", 1.0, 1e9, 100.0, NOW + 86400),
                _row("b", "SUI", 1.0, 1e9, 900.0, NOW + 2 * 86400)]
        out = normalize_index(rows, {"SUI-USD"}, NOW)
        assert out["SUI-USD"]["amount_tokens"] == 900.0

    def test_one_bad_row_kills_one_row(self):
        rows = [{"garbage": True},
                _row("aptos", "APT", 1.0, 1e9, 1e6, NOW + 86400)]
        out = normalize_index(rows, {"APT-USD"}, NOW)
        assert "APT-USD" in out


class TestCatalystRows:
    def test_schema_matches_load_catalysts(self):
        from intelligence.catalyst_calendar import load_catalysts
        sched = {"APT-USD": {"symbol": "APT-USD", "unlock_ts": NOW + 86400,
                             "unlock_type": "cliff", "amount_tokens": 3.2e6,
                             "amount_usd": 2.6e6, "pct_supply": 0.37,
                             "protocol": "Aptos", "ticker": "APT"}}
        rows = catalyst_rows(sched)
        assert len(rows) == 1
        evs, errs = load_catalysts(rows)
        assert errs == []
        assert evs[0].symbol_or_cluster == "APT-USD"
        assert evs[0].event_type == "token_unlock"
        assert evs[0].source == _FEED_SOURCE
        assert abs(evs[0].ts_utc - (NOW + 86400)) < 1.0   # ISO Z parse


class TestMergeStore:
    def test_foreign_rows_preserved(self):
        existing = [
            '{"symbol_or_cluster": "APT-USD", "event_type": "token_unlock", '
            '"ts_utc": 1, "source": "recurring_schedule"}\n',
            '{"symbol_or_cluster": "APT-USD", "event_type": "token_unlock", '
            '"ts_utc": 2, "source": "defillama_emissions"}\n',
            'NOT JSON AT ALL\n',
        ]
        merged = merge_catalyst_store(existing, [{"source": _FEED_SOURCE,
                                                  "ts_utc": 3}])
        lines = [l for l in merged.strip().split("\n") if l]
        assert len(lines) == 3
        assert "recurring_schedule" in lines[0]
        assert lines[1] == "NOT JSON AT ALL"          # verbatim, never destroy
        assert json.loads(lines[2])["ts_utc"] == 3
        assert "defillama_emissions" not in lines[0]

    def test_feed_rows_replaced_not_duplicated(self):
        existing = [json.dumps({"source": _FEED_SOURCE, "old": True}) + "\n"]
        merged = merge_catalyst_store(existing, [{"source": _FEED_SOURCE,
                                                  "new": True}])
        assert merged.count(_FEED_SOURCE) == 1
        assert '"new": true' in merged


class TestFeed:
    @staticmethod
    def _feed(tmp_path, rows, universe=("APT-USD",)):
        async def _fetch():
            return rows
        return UnlockFeed(
            universe=set(universe), log_dir=str(tmp_path),
            catalyst_path=str(tmp_path / "catalyst_events.json"),
            time_fn=lambda: NOW, fetch_fn=_fetch)

    def test_fetch_writes_store_and_cache(self, tmp_path):
        (tmp_path / "catalyst_events.json").write_text(
            '{"symbol_or_cluster": "SUI-USD", "event_type": "token_unlock", '
            '"ts_utc": "2026-10-01T00:00:00Z", "source": "recurring_schedule"}\n')
        f = self._feed(tmp_path, [_row("aptos", "APT", 0.82, 864e6, 3.2e6,
                                       NOW + 86400)])
        assert asyncio.run(f.fetch_due()) is True
        lines = (tmp_path / "catalyst_events.json").read_text().strip().split("\n")
        assert len(lines) == 2
        assert "recurring_schedule" in lines[0]          # hand row preserved
        feed_row = json.loads(lines[1])
        assert feed_row["source"] == _FEED_SOURCE
        assert feed_row["symbol_or_cluster"] == "APT-USD"
        cache = json.loads((tmp_path / "unlock_schedule.json").read_text())
        assert "APT-USD" in cache

    def test_daily_throttle(self, tmp_path):
        calls = {"n": 0}

        async def _fetch():
            calls["n"] += 1
            return [_row("aptos", "APT", 1.0, 1e9, 1e6, NOW + 86400)]

        f = UnlockFeed(universe={"APT-USD"}, log_dir=str(tmp_path),
                       catalyst_path=str(tmp_path / "cat.json"),
                       time_fn=lambda: NOW, fetch_fn=_fetch)
        assert asyncio.run(f.fetch_due()) is True
        assert asyncio.run(f.fetch_due()) is False   # same UTC day
        assert calls["n"] == 1

    def test_fetch_failure_keeps_last_good(self, tmp_path):
        async def _bad():
            raise RuntimeError("network down")
        f = UnlockFeed(universe={"APT-USD"}, log_dir=str(tmp_path),
                       catalyst_path=str(tmp_path / "cat.json"),
                       time_fn=lambda: NOW, fetch_fn=_bad)
        assert asyncio.run(f.fetch_due()) is False
        assert not (tmp_path / "cat.json").exists()   # never wrote garbage

    def test_upcoming_horizon(self, tmp_path):
        f = self._feed(tmp_path, [_row("aptos", "APT", 1.0, 1e9, 1e6,
                                       NOW + 10 * 86400)])
        asyncio.run(f.fetch_due())
        u = f.upcoming("APT-USD", NOW, horizon_days=14.0)
        assert u["days_to_unlock"] == pytest.approx(10.0)
        assert f.upcoming("APT-USD", NOW, horizon_days=7.0) == {}
        assert f.upcoming("BTC-USD", NOW, horizon_days=14.0) == {}


class TestWiring:
    @staticmethod
    def _main_src():
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent / "main.py").read_text()

    def test_kill_switch_defaults(self):
        from core.config import Settings
        s = Settings()
        assert s.unlock_feed_enabled is True
        assert s.unlock_pressure_shadow_enabled is True
        assert s.unlock_shadow_min_pct_supply == 1.0
        assert s.unlock_shadow_horizon_days == 14.0
        assert s.sosovalue_premium_enabled is True
        assert s.sosovalue_news_enabled is True

    def test_loop_supervised(self):
        src = self._main_src()
        assert '_supervise(_unlock_loop,                    "unlock_feed")' in src

    def test_shadow_hook_is_cohort_only(self):
        src = self._main_src()
        i = src.index('logger.info(\n                                "unlock_pressure_shadow"')
        # the hook logs the tag; it must never reject/size — no veto wiring
        hook = src[i - 4000:i]
        assert "unlock_pressure_shadow_enabled" in hook
        assert "signal_rejected" not in hook.split("unlock_pressure_shadow_enabled")[-1]

    def test_macro_pillar_tide_wired(self):
        src = self._main_src()
        assert "_breadth, _bc_tide)" in src
        assert "etf_tide_macro_pillar" in src
