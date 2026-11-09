"""HL equity candle plane — surgical data-plane for prop-mapped xyz: equities.

Governor 2026-11-10 (ATS-72): Arcus feed may be CONNECTED (marks/OB) while
equity SIGNAL emits stay dark — AMD Arcus-only had 0 signal_ready/outbox;
movers (SNDK/MU/INTC/NBIS/MRVL/ZHIPU) were off the ARIA universe entirely.
Peer ATS-71 owns surgical config.assets registration. This plane owns the
24/7 Hyperliquid xyz: candle ownership so once a name is scoreable it is
not starved by thin SoDEX/Yahoo session klines.

Doctrine (preferences + hl-variational-execution-efficiency):
  - Hyperliquid public info = REFERENCE / candle plane only.
  - Propr remains authoritative for equity, uPnL, fills.
  - Variational adds NO unique coverage for the mover cohort (all have
    xyz: mids on HL) and is unused on the fill path → not wired here.
  - Equities stay on xyz: → turbo2 (tradfi). Never crypto turbo sleeve.
  - No Kant / Nietzsche / Chancellor / tiers 1–6 rewrite.

Kill switch: HL_EQUITY_PLANE_ENABLED (default ON) or
config.hl_equity_plane_enabled. OFF = legacy hl_kline_assets bit-for-bit.

Fail-closed: missing SYMBOL_MAP / missing xyz: listing / missing candle
buffer → skip that symbol; never raise into boot.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, List, Optional, Sequence


# Mover + Arcus-migrated equity cohort that must light on HL when listed.
# Peer ATS-71 registers movers into config.assets; Arcus-migrated names may
# remain arcus-only until restored. This seed is additive union material —
# resolve() still requires a live xyz: map entry + HL universe membership
# at feed boot-verify time.
HL_EQUITY_SEED: tuple[str, ...] = (
    # Movers (prop-mapped turbo2; universe peer lane)
    "SNDK-USD", "MU-USD", "INTC-USD",
    "NBIS-USD", "MRVL-USD", "ZHIPU-USD",
    # AMD emit gap specimen (Arcus-only → 0 signal_ready on 2026-11-09)
    "AMD-USD",
    # Remaining Arcus-migrated stocks with xyz: mids (candle plane only;
    # scoring still requires config.assets membership)
    "NVDA-USD", "TSLA-USD", "META-USD", "ORCL-USD",
    "MSFT-USD", "AAPL-USD", "AMZN-USD", "GOOGL-USD",
    "HOOD-USD", "COIN-USD", "CRCL-USD", "MSTR-USD", "SPCX-USD",
)


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


def resolve_hl_kline_symbols(
    config: Any,
    *,
    hl_symbols: Optional[dict] = None,
    seed: Sequence[str] = HL_EQUITY_SEED,
) -> List[str]:
    """Symbols the HyperliquidFeed should own candles for.

    Always starts from config.hl_kline_assets (legacy base). When the plane
    is ON, unions:
      - every config.assets name with an xyz: prop map entry
      - every arcus_assets name with an xyz: prop map entry
      - the HL_EQUITY_SEED cohort (movers + Arcus equities)
    filtered to names present in HL_SYMBOLS (or provided hl_symbols).

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
            hl_symbols = xyz_map  # map presence ≈ listable; boot-verify still gates

    candidates: List[str] = list(base)
    try:
        for s in (getattr(config, "assets", []) or []):
            candidates.append(s)
    except Exception:
        pass
    try:
        for s in (getattr(config, "arcus_assets", []) or []):
            candidates.append(s)
    except Exception:
        pass
    candidates.extend(list(seed or ()))

    resolved: List[str] = []
    for sym in _dedupe(candidates):
        if sym not in xyz_map:
            continue
        # HL_SYMBOLS keys are ARIA symbols (loaded from SYMBOL_MAP). Boot
        # self-verify still drops coins absent from the live xyz dex.
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
    """HL-resolved symbols that lack candle buffers (Arcus-only class).

    Arcus store-repair creates OB+mark only — no candles — so Arcus equities
    cannot be HL-owned until buffers exist. Fail-closed empty list on error.
    """
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
