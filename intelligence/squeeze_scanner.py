"""Squeeze scanner — short-squeeze positioning-structure watchlist (SHADOW).

2026-09-17 ZEC +18.2% autopsy: a pure short squeeze whose setup was visible
24-48h early — funding BELOW its own average (shorts accumulating cheap,
paying longs), OI growing while price was flat (short inventory building),
whales positioned long, and realized-vol rank low (the move was not priced).
ARIA watches price/volume, not positioning structure, and saw none of it.

This module is a ZERO-I/O brain: pure functions + injected providers. The
wiring loop in main.py feeds the planes; this module scores them and keeps a
JSONL watchlist the coherence scorer / operator can read. It NEVER gates
entries (kill switch `squeeze_scanner_shadow`, default True = measure-only).

PROXIED / PARTIAL PLANES (documented, do not "fix"):
  - funding_avg: logs/funding_history.json holds ~7d of HOURLY rows
    post-compaction (168-row cap). The available window IS the proxy for
    the "30d average" — the ratio leg is a within-window relative read,
    not a true 30d baseline.
  - oi_delta_pct: best available window is ~24h (oi_collector plane is
    BTC/ETH/SOL only; live fallback is the bybit_ticker_stores OI snapshot
    which has no history). A 30d OI history does NOT exist yet.
  - rv_rank: realized-vol rank (0-100) is the proxy for IVR — ARIA has no
    options plane. Provider: data/klines_4h.realized_vol_rank on 4h closes.
  - whale_ls: long/short ratio from ARIA's WPP registry ONLY
    (whale_mirror consensus flows). None = abstain (no external whale feed).

ALL THRESHOLDS ARE UNSOURCED HYPOTHESES (single-event reconstruction,
n=1 — the ZEC 2026-09-17 case). They are module constants, not config, so
shadow data argues for tuning before any live use. Aronson: one event
proves nothing; the watchlist exists to build the sample.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional

# ── UNSOURCED hypothesis constants (see module docstring) ────────────────────
FUNDING_ACCUM_RATIO: float = 0.65   # funding < 0.65*avg → shorts accumulating cheap (+3)
FUNDING_ACCUM_PTS: int = 3
FUNDING_PAYING_PTS: int = 1         # funding < 0 → actively paying longs (+1)
OI_STRONG_PCT: float = 15.0         # OI delta > 15% → short inventory building fast (+2)
OI_STRONG_PTS: int = 2
OI_BUILDING_PCT: float = 5.0        # OI delta > 5% → building (+1)
OI_BUILDING_PTS: int = 1
WHALE_LS_LONG: float = 1.3          # whale long/short ratio > 1.3 → whales net long (+2)
WHALE_LS_PTS: int = 2
RV_RANK_LOW: float = 35.0           # rv_rank < 35 → the move is not priced (+2)
RV_RANK_LOW_PTS: int = 2
COIL_MOVE_PCT: float = 2.0          # |day move| < 2% → coiled, hasn't moved (+1)
COIL_PTS: int = 1

VERDICT_SETUP_MIN: int = 7          # score >= 7 → SQUEEZE_SETUP
VERDICT_WATCH_MIN: int = 5          # score >= 5 → WATCH
EXHAUSTED_RV_RANK: float = 85.0     # rv_rank > 85 → post-squeeze DO-NOT-ENTER

VERDICTS = ("NONE", "WATCH", "SQUEEZE_SETUP")

_LOG_PATH_DEFAULT = "logs/squeeze_watchlist.jsonl"
_THROTTLE_S_DEFAULT = 4 * 3600.0    # per-symbol re-log cadence absent a verdict change


@dataclass(frozen=True)
class SqueezeInputs:
    """One symbol's positioning-structure snapshot (all planes injected).

    funding_rate / funding_avg in the SAME units the provider serves
    (Bybit-style percent, e.g. 2.648 = 2.648%). oi_delta_pct in percent over
    the best available window. rv_rank 0-100. whale_ls = long/short ratio
    from the WPP registry, None = abstain. price_day_move_pct in percent
    from the 00:00 UTC anchor.
    """
    symbol: str
    funding_rate: float
    funding_avg: float
    oi_delta_pct: float
    rv_rank: float
    whale_ls: Optional[float] = None
    price_day_move_pct: float = 0.0


@dataclass(frozen=True)
class SqueezeResult:
    symbol: str
    score: int
    verdict: str
    legs: Dict[str, int] = field(default_factory=dict)


def squeeze_score(inputs: SqueezeInputs) -> SqueezeResult:
    """Score one symbol 0-10 across the five positioning legs.

    Legs (all thresholds UNSOURCED — module docstring):
      funding accumulation: funding_rate < 0.65 * funding_avg → +3
      funding paying:       funding_rate < 0 → +1 (stacks with the ratio leg)
      OI building:          oi_delta_pct > 15 → +2, elif > 5 → +1
      whale net long:       whale_ls not None and whale_ls > 1.3 → +2
      vol not priced:       rv_rank < 35 → +2
      coiled:               |price_day_move_pct| < 2.0 → +1

    The funding-ratio leg is skipped when funding_avg == 0 (the comparison
    degenerates to funding<0, which the paying leg already owns).
    """
    legs: Dict[str, int] = {}

    try:
        fr = float(inputs.funding_rate)
        fa = float(inputs.funding_avg)
    except (TypeError, ValueError):
        fr, fa = 0.0, 0.0
    if fa != 0.0 and fr < FUNDING_ACCUM_RATIO * fa:
        legs["funding_accum"] = FUNDING_ACCUM_PTS
    if fr < 0.0:
        legs["funding_paying"] = FUNDING_PAYING_PTS

    try:
        oi = float(inputs.oi_delta_pct)
    except (TypeError, ValueError):
        oi = 0.0
    if oi > OI_STRONG_PCT:
        legs["oi_strong"] = OI_STRONG_PTS
    elif oi > OI_BUILDING_PCT:
        legs["oi_building"] = OI_BUILDING_PTS

    if inputs.whale_ls is not None:
        try:
            if float(inputs.whale_ls) > WHALE_LS_LONG:
                legs["whale_long"] = WHALE_LS_PTS
        except (TypeError, ValueError):
            pass

    try:
        rv = float(inputs.rv_rank)
    except (TypeError, ValueError):
        rv = 100.0   # unknown vol plane → never credit "not priced"
    if rv < RV_RANK_LOW:
        legs["rv_not_priced"] = RV_RANK_LOW_PTS

    try:
        dm = abs(float(inputs.price_day_move_pct))
    except (TypeError, ValueError):
        dm = 0.0
    if dm < COIL_MOVE_PCT:
        legs["coiled"] = COIL_PTS

    score = sum(legs.values())
    if score >= VERDICT_SETUP_MIN:
        verdict = "SQUEEZE_SETUP"
    elif score >= VERDICT_WATCH_MIN:
        verdict = "WATCH"
    else:
        verdict = "NONE"
    return SqueezeResult(symbol=inputs.symbol, score=score,
                         verdict=verdict, legs=legs)


def post_squeeze_exhausted(rv_rank: float, hv: Optional[float] = None) -> bool:
    """DO-NOT-ENTER flag for a COMPLETED squeeze.

    rv_rank > 85 → the move is fully priced; chasing it is the noise class
    (ZEC 2026-09-17 sat at an IVR-equivalent of ~92.8% post-print). `hv` is
    a reserved slot for a future historical-vol cross-check — accepted,
    documented, and intentionally unused until that plane exists.
    """
    try:
        return float(rv_rank) > EXHAUSTED_RV_RANK
    except (TypeError, ValueError):
        return False   # unknown vol plane → no opinion, never blocks


def scanner_enabled() -> bool:
    """Kill switch: squeeze_scanner_shadow (default True — measure-only).

    This module NEVER gates entries regardless; the switch only arms/disarms
    the watchlist scan itself. Fail-silent to the DEFAULTS entry.
    """
    try:
        from intelligence import kill_switch
        return bool(kill_switch.enabled("squeeze_scanner_shadow"))
    except Exception:
        return True


class SqueezeWatchlist:
    """Per-symbol latest SqueezeResult + throttled JSONL append.

    Log discipline: a row is appended to the JSONL only on a VERDICT CHANGE
    for the symbol (first sighting counts) or when THROTTLE_S (default 4h)
    has elapsed since that symbol's last logged row. All I/O is fail-silent
    — a dead disk never kills the scan.
    """

    def __init__(self, log_path: str = _LOG_PATH_DEFAULT,
                 throttle_s: float = _THROTTLE_S_DEFAULT,
                 now: Optional[Callable[[], float]] = None) -> None:
        self._log_path = Path(log_path)
        self._throttle_s = float(throttle_s)
        self._now = now or time.time
        self._latest: Dict[str, SqueezeResult] = {}
        self._last_logged_verdict: Dict[str, str] = {}
        self._last_logged_ts: Dict[str, float] = {}

    def latest(self, symbol: str) -> Optional[SqueezeResult]:
        return self._latest.get(symbol)

    @property
    def results(self) -> Dict[str, SqueezeResult]:
        return dict(self._latest)

    def scan(self, symbols_inputs: Iterable[SqueezeInputs]) -> List[SqueezeResult]:
        """Score all inputs, update the watchlist, append throttled JSONL rows.

        Returns the results sorted by score descending. A malformed input
        aborts that symbol only (fail-silent per row).
        """
        out: List[SqueezeResult] = []
        for inp in symbols_inputs or []:
            try:
                res = squeeze_score(inp)
            except Exception:
                continue
            self._latest[res.symbol] = res
            out.append(res)
            self._maybe_log(inp, res)
        out.sort(key=lambda r: -r.score)
        return out

    def _maybe_log(self, inp: SqueezeInputs, res: SqueezeResult) -> None:
        try:
            now = float(self._now())
            prev_verdict = self._last_logged_verdict.get(res.symbol)
            last_ts = self._last_logged_ts.get(res.symbol, 0.0)
            if prev_verdict == res.verdict and (now - last_ts) < self._throttle_s:
                return
            row = {
                "ts": now,
                "symbol": res.symbol,
                "score": res.score,
                "verdict": res.verdict,
                "legs": res.legs,
                "funding_rate": inp.funding_rate,
                "funding_avg": inp.funding_avg,
                "oi_delta_pct": inp.oi_delta_pct,
                "rv_rank": inp.rv_rank,
                "whale_ls": inp.whale_ls,
                "price_day_move_pct": inp.price_day_move_pct,
                "exhausted": post_squeeze_exhausted(inp.rv_rank),
            }
            with self._log_path.open("a") as fh:
                fh.write(json.dumps(row, default=str) + "\n")
            self._last_logged_verdict[res.symbol] = res.verdict
            self._last_logged_ts[res.symbol] = now
        except Exception:
            pass   # fail-silent: the watchlist is observability, never a gate
