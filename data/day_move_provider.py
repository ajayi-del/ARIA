"""Day-move provider — the single measurement plane for from-midnight moves.

2026-08-28 extraction (Deploy 4): the day-move measurement was wired 4x
(Hugo alt-breadth, dispersion evidence closures, capacity_governor
day_move_aligned, mover_radar) with three memoization regimes and zero
verdict sharing. The doctrines may differ; the MEASUREMENT must not.
This module owns exactly one measurement stack:

    day_move_elapsed      — anchored move from 00:00 UTC open (the
                            2026-08-28 midnight-anchor fix: the 200-deep
                            1m buffer loses the day-open bar after 03:20,
                            so the anchor is cached while visible and a
                            Bybit daily kline seeds it at boot)
    trend_day_move_pct    — thin wrapper (move only)
    crypto_day_moves      — complex-wide moves, 60s memo
    dg_symbol_evidence    — the dispersion gate's evidence bundle

Pure helpers (sigma_from_closes, vol_ratio_from_volumes, rank_pctile,
_alt_breadth_vote, day_move_elapsed_anchored) live here and are
re-exported by main.py for existing importers.

Doctrine boundaries: thresholds and verdicts stay in their owning brains
(dispersion_gate, trend_offensive, capacity_governor, day_type_classifier).
This provider measures; it never decides. Buffer access is injected —
zero I/O of its own, so tests drive it with fixtures.
"""
import math
import os
import time
from typing import Optional

CRYPTO_EXCLUDED_CATS = frozenset({
    "equity", "equity_index",
    "commodity", "commodity_energy", "commodity_precious",
    "commodity_industrial",
    "index_tech", "index_broad", "index_equity", "index_meme", "index_defi",
})

DG_NONE_EV = {"day_move_pct": None, "day_elapsed_s": 0.0,
              "daily_sigma_pct": None, "ret_1h_pct": None,
              "sigma_1h_pct": None, "vol_ratio": None,
              "move_rank_pctile": None}


def day_move_elapsed_anchored(candles, mid_ms: int, today_int: int,
                              anchor_cache: dict, symbol: str) -> tuple:
    """(move-from-00:00-UTC-open %, elapsed_s) with the 2026-08-28 anchor.

    The 1m CandleBuffer is 200 bars deep; after 03:20 UTC the true day-open
    bar falls off and the naive scan silently reads a trailing 3.33h window
    (live: TAO -0.15% measured vs -6.71% true on 2026-08-28). Cache the
    day-open while visible (first bar ≤30min after midnight); serve it for
    the rest of the UTC day. Fail-open: no anchor → legacy truncated read.
    """
    if not candles:
        return None, 0.0
    base = None
    for cd in candles:
        if cd.open_time >= mid_ms:
            base = cd
            break
    if base is None or base.open <= 0:
        return None, 0.0
    last_ts = float(candles[-1].close_time or candles[-1].open_time)
    if base.open_time - mid_ms <= 1800_000:
        anchor_cache[symbol] = (today_int, base.open, base.open_time)
    else:
        anch = anchor_cache.get(symbol)
        if anch is not None and anch[0] == today_int:
            elapsed = max(0.0, (last_ts - float(anch[2])) / 1000.0)
            return (candles[-1].close / anch[1] - 1.0) * 100.0, elapsed
    elapsed = max(0.0, (last_ts - float(base.open_time)) / 1000.0)
    return (candles[-1].close / base.open - 1.0) * 100.0, elapsed


def sigma_from_closes(closes: list) -> float | None:
    """Sample stdev of log returns; None on thin input or degenerate prices.
    Anti-contamination is the caller's job (pass the BASE window, excluding
    the leg being measured)."""
    if len(closes) < 31:
        return None
    try:
        rets = [math.log(float(b) / float(a))
                for a, b in zip(closes, closes[1:]) if float(a) > 0 and float(b) > 0]
    except (TypeError, ValueError):
        return None
    if len(rets) < 30:
        return None
    mu = sum(rets) / len(rets)
    var = sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) if var > 0 else None


def vol_ratio_from_volumes(volumes: list, recent_n: int = 60,
                           min_base: int = 60) -> float | None:
    """mean(last recent_n) / mean(base before that); None when the base is
    too thin or volume-less (off-hours synthetics → fail-closed)."""
    if len(volumes) < recent_n + min_base:
        return None
    try:
        base = [float(v) for v in volumes[:-recent_n]]
        recent = [float(v) for v in volumes[-recent_n:]]
    except (TypeError, ValueError):
        return None
    base_mu = sum(base) / len(base)
    if base_mu <= 0:
        return None
    return (sum(recent) / len(recent)) / base_mu


def rank_pctile(value: float, population: list, min_n: int = 10) -> float | None:
    """Fraction of population ≤ value; None below the decile-meaningful n."""
    if len(population) < min_n:
        return None
    return sum(1 for v in population if v <= value) / len(population)


def _alt_breadth_vote(moves: dict, min_n: int = 5, move_pct: float = 5.0) -> int:
    """Hugo alt-breadth day-move tiebreak (2026-08-27): +1/−1 when ≥min_n alts
    move ≥move_pct in one direction; 0 when split or below quorum — a
    divergent tape abstains, never overrides a majors vote."""
    longs = sum(1 for m in moves.values() if m is not None and m >= move_pct)
    shorts = sum(1 for m in moves.values() if m is not None and m <= -move_pct)
    if longs >= min_n and shorts >= min_n:
        return 0
    if longs >= min_n:
        return 1
    if shorts >= min_n:
        return -1
    return 0


class DayMoveProvider:
    """One measurement plane for from-midnight day moves.

    Injected:
      buffer_getter(symbol, interval) -> CandleBuffer | None
      assets()       -> iterable of universe symbols (live ref OK)
      category_of(s) -> ASSET_CONFIG category string ("" default)
      time_fn / monotonic_fn — injectable clocks (department template).

    Owns the midnight-anchor cache (seed it at boot via .anchor) and the
    60s crypto-complex memo. Bit-for-bit vs the 2026-08-28 main() closures.
    """

    def __init__(self, buffer_getter, assets, category_of,
                 time_fn=time.time, monotonic_fn=time.monotonic):
        self._buf = buffer_getter
        self._assets = assets
        self._category_of = category_of
        self._time = time_fn
        self._monotonic = monotonic_fn
        self.anchor: dict = {}   # symbol -> (utc_day_int, open_price, open_time_ms)
        self._crypto_memo = {"ts": 0.0, "moves": {}}

    def day_move_elapsed(self, symbol: str) -> tuple:
        """(move from 00:00 UTC open to latest 1m close in %, elapsed_s of the
        measured window) — elapsed is candle-derived so the vol-z leg's √t
        scaling matches the window actually measured, whatever the buffer
        depth. Buffer starting after midnight reads a later base — fail-open
        by construction."""
        _buf = self._buf(symbol, "1m")
        if _buf is None:
            return None, 0.0
        try:
            return day_move_elapsed_anchored(
                _buf.latest(1500),
                int(self._time() // 86400 * 86400 * 1000),
                int(self._time() // 86400),
                self.anchor, symbol)
        except Exception:
            return None, 0.0

    def trend_day_move_pct(self, symbol: str) -> Optional[float]:
        """Move from today's 00:00 UTC open to the latest 1m close, in %.
        Buffer starting after midnight (fresh boot) reads a later base —
        small move → below threshold → inert. Fail-open by construction."""
        return self.day_move_elapsed(symbol)[0]

    def crypto_day_moves(self) -> dict:
        """Crypto-complex day moves, memoized 60s — shared by the dispersion
        self-move rank leg (population) and the Hugo alt-breadth tiebreak.
        Non-crypto categories (equities, commodities, synthetic indices) are
        excluded: the dispersion low-branch only gates crypto, and indices
        would double-count their constituents."""
        _now = self._monotonic()
        if _now - self._crypto_memo["ts"] < 60.0:
            return self._crypto_memo["moves"]
        _moves = {}
        for _s in self._assets():
            if self._category_of(_s) in CRYPTO_EXCLUDED_CATS:
                continue
            _m = self.trend_day_move_pct(_s)
            if _m is not None:
                _moves[_s] = _m
        self._crypto_memo["ts"] = _now
        self._crypto_memo["moves"] = _moves
        return _moves

    def dg_symbol_evidence(self, symbol: str) -> dict:
        """Self-move evidence bundle for the dispersion gate (2026-08-27).
        Gathers the three legs' raw inputs; the gate owns all thresholds.
        Every leg fail-closed: missing buffers → None fields → legacy gate.
        Kill switch off → all-None → bit-for-bit legacy."""
        if os.environ.get("DISPERSION_SELF_MOVE_EXEMPT_ENABLED",
                          "true").lower() == "false":
            return dict(DG_NONE_EV)
        try:
            _ev = dict(DG_NONE_EV)
            _dm, _el = self.day_move_elapsed(symbol)
            _ev["day_move_pct"], _ev["day_elapsed_s"] = _dm, _el
            # Daily σ from the 5m buffer (200 bars ≈ 16.7h) — Carver scaling.
            _b5 = self._buf(symbol, "5m")
            if _b5 is not None:
                _s5 = sigma_from_closes(_b5.closes(200))
                if _s5 is not None:
                    _ev["daily_sigma_pct"] = _s5 * math.sqrt(288.0) * 100.0
            # Fast leg + participation from the 1m buffer: ret over the last
            # 60 bars, σ from the BASE window (anti-contamination — the
            # expansion being measured must not inflate its own baseline).
            _b1 = self._buf(symbol, "1m")
            if _b1 is not None:
                _c1 = _b1.closes(200)
                if len(_c1) >= 152 and _c1[-61] > 0 and _c1[-1] > 0:
                    _s1b = sigma_from_closes(_c1[:-61])
                    if _s1b is not None:
                        _ev["ret_1h_pct"] = math.log(_c1[-1] / _c1[-61]) * 100.0
                        _ev["sigma_1h_pct"] = _s1b * math.sqrt(60.0) * 100.0
                _ev["vol_ratio"] = vol_ratio_from_volumes(_b1.volumes(200))
            if _dm is not None:
                _pop = [abs(_m) for _m in self.crypto_day_moves().values()]
                _ev["move_rank_pctile"] = rank_pctile(abs(_dm), _pop)
            return _ev
        except Exception:
            return dict(DG_NONE_EV)
