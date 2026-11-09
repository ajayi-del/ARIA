"""HL equity candle + map-only surplus plane (ATS-72).

Governor 2026-11-10: Arcus feed may be CONNECTED (marks/OB) while equity
SIGNAL emits stay dark — AMD Arcus-only had 0 signal_ready/outbox; movers
were off the ARIA universe. Peer ATS-71/#32 owns surgical config.assets
registration (SNDK/MU/INTC/NBIS/MRVL/ZHIPU/AMD). This plane owns:

  1. 24/7 Hyperliquid xyz: candle ownership for prop-mapped equities.
  2. Map-only names (CBRS — HL-listed, NOT SoDEX) → mark-store writes +
     rate-limited capital_block surplus emits into prop_outbox (turbo2).

Doctrine (preferences + hl-variational-execution-efficiency):
  - Hyperliquid public info = REFERENCE / candle / map-only mark plane.
  - Propr remains authoritative for equity, uPnL, fills.
  - Variational NOT wired (no unique equity vs HL for this cohort).
  - Equities stay on xyz: → turbo2 (tradfi). Never crypto turbo sleeve.
  - No Kant / Nietzsche / Chancellor / tiers 1–6 rewrite.

Kill switches:
  HL_EQUITY_PLANE_ENABLED / config.hl_equity_plane_enabled (default ON)
    OFF = legacy hl_kline_assets bit-for-bit (no dynamic union, no
    map-only surplus).
  HL_MAP_ONLY_SURPLUS_ENABLED / config.hl_map_only_surplus_enabled
    (default ON, also requires plane ON + PROP capital surplus).

Fail-closed: missing map / HL listing / mark / geometry → skip; never raise.
"""

from __future__ import annotations

import os
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


# Mover + Arcus-migrated equity cohort that must light on HL when listed.
HL_EQUITY_SEED: tuple[str, ...] = (
    "SNDK-USD", "MU-USD", "INTC-USD",
    "NBIS-USD", "MRVL-USD", "ZHIPU-USD",
    "AMD-USD",
    # Map-only (no SoDEX listing) — #32 handoff
    "CBRS-USD",
    # Remaining Arcus-migrated stocks with xyz: mids
    "NVDA-USD", "TSLA-USD", "META-USD", "ORCL-USD",
    "MSFT-USD", "AAPL-USD", "AMZN-USD", "GOOGL-USD",
    "HOOD-USD", "COIN-USD", "CRCL-USD", "MSTR-USD", "SPCX-USD",
)

# Default map-only cohort: prop-mapped xyz: but NOT in config.assets /
# arcus_assets (SoDEX absent). Peer #32 pins CBRS here.
HL_MAP_ONLY_DEFAULT: tuple[str, ...] = ("CBRS-USD",)

# Per-symbol last successful map-only surplus emit (monotonic seconds).
_MAP_ONLY_EMIT_TS: Dict[str, float] = {}

DEFAULT_MAP_ONLY_RATE_S = 300.0
# Multi-horizon equity move gates (1m bars). Short windows catch impulse;
# 240 (~4h) catches the slow HL equity drift CBRS showed at ship (+0.63%).
DEFAULT_MAP_ONLY_HORIZONS: tuple[tuple[int, float], ...] = (
    (15, 0.25),
    (60, 0.35),
    (240, 0.50),
)
DEFAULT_MAP_ONLY_COH = 3.5


def _env_bool(name: str) -> Optional[bool]:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return None
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return None


def plane_enabled(config: Any = None) -> bool:
    """Kill switch. Default ON. OFF = pre-plane hl_kline_assets only."""
    env = _env_bool("HL_EQUITY_PLANE_ENABLED")
    if env is not None:
        return env
    if config is not None:
        return bool(getattr(config, "hl_equity_plane_enabled", True))
    return True


def map_only_surplus_enabled(config: Any = None) -> bool:
    """Map-only → prop surplus emits. Requires plane ON. Default ON."""
    if not plane_enabled(config):
        return False
    env = _env_bool("HL_MAP_ONLY_SURPLUS_ENABLED")
    if env is not None:
        return env
    if config is not None:
        return bool(getattr(config, "hl_map_only_surplus_enabled", True))
    return True


def _prop_xyz_map() -> dict[str, str]:
    """ARIA symbol → xyz: coin. Fail-closed empty on import defect."""
    try:
        from prop_harness.io.signal_tap import SYMBOL_MAP
        return {
            aria: coin for aria, coin in SYMBOL_MAP.items()
            if isinstance(coin, str) and coin.startswith("xyz:")
        }
    except Exception:
        return {}


def _dedupe(seq: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for s in seq:
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


def map_only_symbols(config: Any = None) -> List[str]:
    """Prop-mapped xyz: names that must NOT join SoDEX/Arcus universes.

    Uses config.hl_map_only_assets when set; else HL_MAP_ONLY_DEFAULT,
    filtered to SYMBOL_MAP xyz: entries and excluded from assets/arcus.
    Fail-closed empty.
    """
    if not plane_enabled(config):
        return []
    xyz_map = _prop_xyz_map()
    try:
        raw = list(getattr(config, "hl_map_only_assets", None) or HL_MAP_ONLY_DEFAULT)
    except Exception:
        raw = list(HL_MAP_ONLY_DEFAULT)
    assets = set(getattr(config, "assets", []) or []) if config is not None else set()
    arcus = set(getattr(config, "arcus_assets", []) or []) if config is not None else set()
    out: List[str] = []
    for sym in _dedupe(raw):
        if sym not in xyz_map:
            continue
        if sym in assets or sym in arcus:
            continue  # peer registered → normal path; not map-only
        out.append(sym)
    return out


def write_marks_to_store(symbol: str, config: Any = None) -> bool:
    """True when HL candle closes must update mark_price_stores.

    Map-only names have no SoDEX rebased synthetic — HL mid IS the mark
    (scale doctrine carve-out). Global HL_MARKS_ENABLED still covers the
    rest of the universe.
    """
    if not plane_enabled(config):
        return False
    return str(symbol) in set(map_only_symbols(config))


def resolve_hl_kline_symbols(
    config: Any,
    *,
    hl_symbols: Optional[dict] = None,
    seed: Sequence[str] = HL_EQUITY_SEED,
) -> List[str]:
    """Symbols the HyperliquidFeed should own candles for.

    Always starts from config.hl_kline_assets (legacy base). When the plane
    is ON, unions assets∩xyz:, arcus∩xyz:, map-only, and HL_EQUITY_SEED.
    Plane OFF → exact legacy list (copy). Never raises.
    """
    try:
        base = list(getattr(config, "hl_kline_assets", []) or [])
    except Exception:
        base = []
    if not plane_enabled(config):
        return list(base)

    xyz_map = _prop_xyz_map()
    if hl_symbols is None:
        try:
            from data.hyperliquid_feed import HL_SYMBOLS as _HL
            hl_symbols = _HL
        except Exception:
            hl_symbols = xyz_map

    candidates: List[str] = list(base)
    try:
        candidates.extend(getattr(config, "assets", []) or [])
    except Exception:
        pass
    try:
        candidates.extend(getattr(config, "arcus_assets", []) or [])
    except Exception:
        pass
    candidates.extend(list(seed or ()))
    candidates.extend(map_only_symbols(config))

    resolved: List[str] = []
    for sym in _dedupe(candidates):
        if sym not in xyz_map:
            continue
        if hl_symbols is not None and sym not in hl_symbols:
            continue
        resolved.append(sym)
    return resolved


def symbols_needing_candle_buffers(
    config: Any,
    existing_buffers: Optional[dict] = None,
    *,
    resolved: Optional[Sequence[str]] = None,
) -> List[str]:
    """HL-resolved symbols that lack candle buffers."""
    try:
        want = list(resolved) if resolved is not None else resolve_hl_kline_symbols(config)
    except Exception:
        return []
    have = existing_buffers if isinstance(existing_buffers, dict) else {}
    out: List[str] = []
    for sym in want:
        buf = have.get(sym) or {}
        if "1m" not in buf:
            out.append(sym)
    return out


def direction_from_closes(
    closes: Sequence[float],
    *,
    horizons: Sequence[Tuple[int, float]] = DEFAULT_MAP_ONLY_HORIZONS,
    lookback: Optional[int] = None,
    move_pct: Optional[float] = None,
) -> Optional[Tuple[str, float]]:
    """(side, move_pct_abs) from 1m closes, else None.

    Tries each (lookback, min_move_pct) horizon; first clear hit wins
    (shortest first). Optional lookback/move_pct override → single gate
    (unit-test compatibility). Fail-closed on sparse/degenerate series.
    """
    try:
        xs = [float(c) for c in closes if c is not None]
    except (TypeError, ValueError):
        return None
    gates: List[Tuple[int, float]]
    if lookback is not None and move_pct is not None:
        gates = [(int(lookback), float(move_pct))]
    else:
        gates = [(int(lb), float(mp)) for lb, mp in (horizons or ())]
    best: Optional[Tuple[str, float]] = None
    for lb, mp in gates:
        if len(xs) < lb + 1 or lb <= 0 or mp <= 0:
            continue
        a, b = xs[-(lb + 1)], xs[-1]
        if a <= 0 or b <= 0:
            continue
        pct = (b / a - 1.0) * 100.0
        if abs(pct) < mp:
            continue
        cand = ("long" if pct > 0 else "short", abs(pct))
        # Prefer the shortest horizon that clears (already ordered); return.
        return cand
    return best


def _rate_allows(symbol: str, rate_s: float) -> bool:
    if rate_s <= 0:
        return True
    last = _MAP_ONLY_EMIT_TS.get(str(symbol), 0.0)
    return (time.monotonic() - last) >= rate_s


def _rate_stamp(symbol: str) -> None:
    _MAP_ONLY_EMIT_TS[str(symbol)] = time.monotonic()


def emit_map_only_surplus(
    symbol: str,
    *,
    mark: float,
    closes: Sequence[float],
    config: Any = None,
    outbox_path: Optional[str] = None,
    rate_s: Optional[float] = None,
    lookback: Optional[int] = None,
    move_pct: Optional[float] = None,
    coherence: float = DEFAULT_MAP_ONLY_COH,
) -> Optional[dict]:
    """CBRS-class map-only → aria_capital_block on HL mid momentum.

    Never places SoDEX/Arcus orders — outbox only. Fail-closed None.
    """
    if not map_only_surplus_enabled(config):
        return None
    if str(symbol) not in set(map_only_symbols(config)):
        return None
    try:
        rate = float(rate_s if rate_s is not None else getattr(
            config, "hl_map_only_surplus_rate_s", DEFAULT_MAP_ONLY_RATE_S) or DEFAULT_MAP_ONLY_RATE_S)
    except (TypeError, ValueError):
        rate = DEFAULT_MAP_ONLY_RATE_S
    if not _rate_allows(symbol, rate):
        return None
    if lookback is not None and move_pct is not None:
        verdict = direction_from_closes(
            closes, lookback=lookback, move_pct=move_pct)
    else:
        verdict = direction_from_closes(closes)
    if verdict is None:
        return None
    side, _moved = verdict
    try:
        from intelligence.prop_capital_block import (
            emit_capital_block,
            synthetic_surplus_geometry,
        )
    except Exception:
        return None
    geo = synthetic_surplus_geometry(side, mark)
    if geo is None:
        return None
    entry, stop = geo
    rec = emit_capital_block(
        symbol=symbol,
        side=side,
        entry_ref=entry,
        stop_price=stop,
        coherence=coherence,
        capital_reason="hl_map_only",
        aria_venue="hyperliquid",
        src="hl_map_only_surplus",
        config=config,
        outbox_path=outbox_path,
    )
    if rec is not None:
        _rate_stamp(symbol)
    return rec
