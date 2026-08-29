"""DayMoveProvider extraction pins (Deploy 4, 2026-08-28).

Bit-for-bit vs the main() closures they replace: same buffers + clocks in,
same evidence bundles out. The provider measures; doctrines live elsewhere.
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.day_move_provider import (  # noqa: E402
    CRYPTO_EXCLUDED_CATS, DG_NONE_EV, DayMoveProvider)

MID_MS = 1_799_971_200_000   # 86400*20833*1000 — a real midnight boundary
TODAY = MID_MS // 86_400_000
MIN = 60_000
NOW_S = MID_MS / 1000.0 + 20 * 3600   # 20:00 UTC same day


def _candle(i, open_, close, vol=1.0):
    ot = MID_MS + i * MIN
    return SimpleNamespace(open_time=ot, open=open_, close=close,
                           close_time=ot + MIN, volume=vol)


class _Buf:
    def __init__(self, candles):
        self._c = candles

    def latest(self, n):
        return self._c[-n:]

    def closes(self, n):
        return [c.close for c in self._c[-n:]]

    def volumes(self, n):
        return [c.volume for c in self._c[-n:]]


def _provider(buffers, assets=(), categories=None, now=NOW_S):
    return DayMoveProvider(
        lambda s, iv: buffers.get((s, iv)),
        lambda: assets,
        lambda s: (categories or {}).get(s, ""),
        time_fn=lambda: now,
        monotonic_fn=lambda: 1000.0)


def _mk_series(n, open0, drift_per_bar=0.0, vol=1.0, start_bar=0):
    cds, px = [], open0
    for i in range(start_bar, start_bar + n):
        nxt = px * (1.0 + drift_per_bar)
        cds.append(_candle(i, px, nxt, vol))
        px = nxt
    return cds


# ── day_move_elapsed / trend_day_move_pct ────────────────────────────────────

def test_move_from_anchor_when_buffer_truncated():
    cds = _mk_series(200, 105.0, start_bar=1200)   # 20:00 UTC — midnight bar gone
    p = _provider({("TAO-USD", "1m"): _Buf(cds)})
    p.anchor["TAO-USD"] = (TODAY, 100.0, MID_MS)
    move, elapsed = p.day_move_elapsed("TAO-USD")
    assert move is not None and abs(move - (cds[-1].close / 100.0 - 1.0) * 100.0) < 1e-9
    assert elapsed > 0
    assert p.trend_day_move_pct("TAO-USD") == move


def test_missing_buffer_returns_none():
    p = _provider({})
    assert p.day_move_elapsed("NOPE-USD") == (None, 0.0)
    assert p.trend_day_move_pct("NOPE-USD") is None


def test_anchor_cache_shared_with_boot_seed():
    p = _provider({("X-USD", "1m"): _Buf(_mk_series(200, 100.0))})
    assert p.anchor == {}
    p.anchor["X-USD"] = (TODAY, 99.0, MID_MS)   # what the boot seed writes
    move, _ = p.day_move_elapsed("X-USD")
    assert move is not None


# ── crypto_day_moves ─────────────────────────────────────────────────────────

def test_crypto_moves_excludes_non_crypto_categories():
    assets = ["BTC-USD", "SPCX-USD", "XAUT-USD"]
    cats = {"SPCX-USD": "equity_index", "XAUT-USD": "commodity_precious"}
    bufs = {}
    for s, o in (("BTC-USD", 100.0), ("SPCX-USD", 50.0), ("XAUT-USD", 60.0)):
        bufs[(s, "1m")] = _Buf(_mk_series(200, o, drift_per_bar=0.0001))
    p = _provider(bufs, assets=assets, categories=cats)
    moves = p.crypto_day_moves()
    assert set(moves) == {"BTC-USD"}        # equity + commodity excluded
    assert moves["BTC-USD"] > 0
    for c in ("equity", "equity_index", "commodity", "index_meme"):
        assert c in CRYPTO_EXCLUDED_CATS


def test_crypto_moves_memoized_60s():
    assets = ["BTC-USD"]
    bufs = {("BTC-USD", "1m"): _Buf(_mk_series(200, 100.0, 0.0001))}
    clock = {"t": 1000.0}
    p = DayMoveProvider(lambda s, iv: bufs.get((s, iv)),
                        lambda: assets, lambda s: "",
                        time_fn=lambda: NOW_S, monotonic_fn=lambda: clock["t"])
    first = p.crypto_day_moves()
    bufs[("BTC-USD", "1m")] = _Buf(_mk_series(200, 100.0, -0.001))  # market flips
    assert p.crypto_day_moves() is first    # memo: same object within 60s
    clock["t"] += 61.0
    second = p.crypto_day_moves()
    assert second is not first and second["BTC-USD"] < 0


# ── dg_symbol_evidence ───────────────────────────────────────────────────────

def test_evidence_bundle_full():
    cds = _mk_series(200, 100.0, 0.0005, vol=2.0)
    bufs = {("TAO-USD", "1m"): _Buf(cds), ("TAO-USD", "5m"): _Buf(cds)}
    p = _provider(bufs, assets=["TAO-USD"])
    ev = p.dg_symbol_evidence("TAO-USD")
    assert ev["day_move_pct"] is not None and ev["day_move_pct"] > 0
    assert ev["day_elapsed_s"] > 0
    assert ev["daily_sigma_pct"] is not None
    assert ev["ret_1h_pct"] is not None
    assert ev["sigma_1h_pct"] is not None
    assert ev["vol_ratio"] == 1.0           # constant volume
    assert ev["move_rank_pctile"] is None   # singleton population < min_n=10


def test_evidence_missing_buffers_fails_closed():
    p = _provider({})
    ev = p.dg_symbol_evidence("GHOST-USD")
    assert ev == DG_NONE_EV
    assert ev is not DG_NONE_EV             # copy, not the shared dict


def test_evidence_kill_switch_returns_legacy():
    os.environ["DISPERSION_SELF_MOVE_EXEMPT_ENABLED"] = "false"
    try:
        cds = _mk_series(200, 100.0, 0.0005)
        p = _provider({("TAO-USD", "1m"): _Buf(cds)})
        assert p.dg_symbol_evidence("TAO-USD") == DG_NONE_EV
    finally:
        del os.environ["DISPERSION_SELF_MOVE_EXEMPT_ENABLED"]


def test_evidence_rank_uses_abs_moves():
    assets = [f"S{i}-USD" for i in range(12)]
    bufs = {}
    for i, s in enumerate(assets):
        drift = 0.0001 * (i - 6)            # mixed signs
        bufs[(s, "1m")] = _Buf(_mk_series(200, 100.0, drift))
    p = _provider(bufs, assets=assets)
    ev = p.dg_symbol_evidence("S11-USD")    # largest positive drift
    assert ev["move_rank_pctile"] is not None
    assert 0.0 < ev["move_rank_pctile"] <= 1.0
    assert ev["daily_sigma_pct"] is None    # no 5m buffer → leg abstains
