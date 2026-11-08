"""Prop surplus capital-block emit (management-layer-schema §7).

Thesis + geometry OK but ARIA capital cannot post → labeled
`block_class=aria_capital_block` on `signals/prop_outbox.jsonl` so the
harness may take surplus. True risk/structure refuses never enter this
path (those stay fail-closed as `aria_risk_block` telemetry only, or
omit).

Today's approved emit (main.py execution_decision) is unlabeled =
legacy `aria_exec`. S1 tags `block_class=aria_exec` when enabled.

Tradfi fascia (Governor 2026-11-08): anticipator `fast_cycle_entry_approved`
and Arcus `below_min_notional` standdowns were dead-wires into the outbox
— crypto `execution_decision` + Arcus fill-broadcast only. `emit_exec_signal`
and `emit_arcus_notional_block` close those splices under kill switches.

Kill switches (False / PROP_*=0 = pre-build bit-for-bit):
  PROP_BLOCK_CLASS / config.prop_block_class_enabled
  PROP_CAPITAL_SURPLUS / config.prop_capital_surplus_enabled
  PROP_OUTBOX_FAST_CYCLE_EMIT (default ON) — anticipator → aria_exec
  PROP_OUTBOX_ARCUS_NOTIONAL_EMIT (default ON) — Arcus dust → capital_block
  PROP_SURPLUS_COHERENCE_UNITS (default ON) — capital_block coh in 0–10
    ARIA units (prefer measured aria_coherence; else strength*10).
    OFF = legacy strength-as-coherence / xpr omit.
  PROP_OUTBOX_ENABLED / PROP_OUTBOX_MAP_HYGIENE still bind on new emits

Fail-open: emit errors never raise into the ARIA trade path.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

BLOCK_EXEC = "aria_exec"
BLOCK_CAPITAL = "aria_capital_block"
BLOCK_RISK = "aria_risk_block"

# Surplus-eligible capital_reason vocabulary (§7 table). Opaque to Army.
CAPITAL_REASONS = frozenset({
    "venue_equity_insufficient",
    "venue_equity_unknown",
    "notional_floor",
    "margin_budget",
    "margin_preflight",
    "margin_unknown",
    "campaign_terminal_floor_unaffordable",
})


def _env_bool(name: str) -> Optional[bool]:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return None
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return None


def block_class_enabled(config: Any = None) -> bool:
    """S1 — tag `block_class=aria_exec` on the approved outbox emit."""
    env = _env_bool("PROP_BLOCK_CLASS")
    if env is not None:
        return env
    if config is not None:
        return bool(getattr(config, "prop_block_class_enabled", True))
    return True


def capital_surplus_enabled(config: Any = None) -> bool:
    """S2 — emit capital_block rows at ARIA capital standdowns."""
    env = _env_bool("PROP_CAPITAL_SURPLUS")
    if env is not None:
        return env
    if config is not None:
        return bool(getattr(config, "prop_capital_surplus_enabled", True))
    return True


def outbox_master_enabled() -> bool:
    """Master gate shared with the M4 execution_decision writer."""
    return os.environ.get("PROP_OUTBOX_ENABLED", "true").strip().lower() != "false"


def fast_cycle_emit_enabled(config: Any = None) -> bool:
    """Anticipator fast_cycle_entry_approved → aria_exec fascia. Default ON."""
    env = _env_bool("PROP_OUTBOX_FAST_CYCLE_EMIT")
    if env is not None:
        return env
    if config is not None:
        return bool(getattr(config, "prop_outbox_fast_cycle_emit_enabled", True))
    return True


def arcus_notional_emit_enabled(config: Any = None) -> bool:
    """Arcus below_min_notional → aria_capital_block fascia. Default ON."""
    env = _env_bool("PROP_OUTBOX_ARCUS_NOTIONAL_EMIT")
    if env is not None:
        return env
    if config is not None:
        return bool(getattr(config, "prop_outbox_arcus_notional_emit_enabled", True))
    return True


def surplus_coherence_units_enabled(config: Any = None) -> bool:
    """Option B — stamp capital_block coherence in ARIA 0–10 units.

    Default ON. PROP_SURPLUS_COHERENCE_UNITS=0 / config False = legacy
    strength-as-coherence (ant) / omit (xpr) bit-for-bit.
    """
    env = _env_bool("PROP_SURPLUS_COHERENCE_UNITS")
    if env is not None:
        return env
    if config is not None:
        return bool(getattr(config, "prop_surplus_coherence_units_enabled", True))
    return True


def resolve_surplus_coherence(
    *,
    aria_coherence: Optional[float] = None,
    strength: Optional[float] = None,
    config: Any = None,
) -> Optional[float]:
    """Capital_block coherence for prop eval (0–10 ladder), not OrderSpec 0..1.

    Cite: live-signals-cybernetic-audit.md §2.2 / P0 option B.

    Kill ON (default):
      1. Prefer true ARIA coherence when present (finite).
      2. Else strength*10 when strength is present — never omit so the
         risk ladder still sees usable coh.
      3. Else None (no strength on xpr CounterProbeSpec).

    Kill OFF: return strength unchanged (legacy ant mis-scale) or None
    when strength absent (legacy xpr omit). aria_coherence ignored.
    """
    if not surplus_coherence_units_enabled(config):
        if strength is None:
            return None
        try:
            return float(strength)
        except (TypeError, ValueError):
            return None

    if aria_coherence is not None:
        try:
            coh = float(aria_coherence)
        except (TypeError, ValueError):
            coh = None
        else:
            if coh == coh:  # finite (not NaN)
                return max(0.0, min(10.0, coh))

    if strength is None:
        return None
    try:
        st = float(strength)
    except (TypeError, ValueError):
        return None
    if st != st:  # NaN
        return None
    # Documented fallback: anticipator strength 0..1 → ARIA coh 0..10.
    return max(0.0, min(10.0, st * 10.0))


def map_hygiene_allows(symbol: str) -> bool:
    """PROP_OUTBOX_MAP_HYGIENE — refuse unmapped emits (fail-open on import)."""
    if os.environ.get("PROP_OUTBOX_MAP_HYGIENE", "true").strip().lower() == "false":
        return True
    try:
        from prop_harness.io.signal_tap import SYMBOL_MAP
        return str(symbol) in SYMBOL_MAP
    except Exception:
        return True


def default_outbox_path(base_dir: Optional[str] = None) -> str:
    root = base_dir or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, "signals", "prop_outbox.jsonl")


def build_signal(
    *,
    symbol: str,
    side: str,
    entry_ref: float,
    stop_price: float,
    ts_ms: Optional[int] = None,
    tp1_price: Optional[float] = None,
    coherence: Optional[float] = None,
    src: str = "capital_standdown",
    block_class: Optional[str] = None,
    capital_reason: Optional[str] = None,
    aria_venue: Optional[str] = None,
    thesis_id: Optional[str] = None,
) -> Optional[dict]:
    """M4 outbox contract + §7 surplus fields. None = naked/degenerate."""
    side_l = str(side or "").strip().lower()
    if side_l not in ("long", "short", "buy", "sell"):
        return None
    # Normalize buy/sell → long/short for the outbox (tap accepts both).
    if side_l == "buy":
        side_l = "long"
    elif side_l == "sell":
        side_l = "short"
    try:
        e, s = float(entry_ref), float(stop_price)
    except (TypeError, ValueError):
        return None
    if e <= 0 or s <= 0:
        return None
    if side_l == "long" and s >= e:
        return None
    if side_l == "short" and s <= e:
        return None
    ts = int(ts_ms if ts_ms is not None else time.time() * 1000)
    tp: Optional[float] = None
    if tp1_price is not None:
        try:
            t = float(tp1_price)
            if t > 0 and ((side_l == "long" and t > e) or (side_l == "short" and t < e)):
                tp = round(t, 8)
        except (TypeError, ValueError):
            tp = None
    rec: dict = {
        "id": f"{symbol}_{side_l}_{ts}",
        "symbol": symbol,
        "side": side_l,
        "entry_ref": round(e, 8),
        "stop_price": round(s, 8),
        "tp1_price": tp,
        "ts_ms": ts,
        "src": src,
    }
    if coherence is not None:
        try:
            rec["coherence"] = round(float(coherence), 2)
        except (TypeError, ValueError):
            pass
    if block_class:
        rec["block_class"] = str(block_class)
    if capital_reason:
        rec["capital_reason"] = str(capital_reason)
    if aria_venue:
        rec["aria_venue"] = str(aria_venue)
    if thesis_id:
        rec["thesis_id"] = str(thesis_id)
    else:
        # Stable-enough day key for KingdomView reservation (S4 later).
        day = time.strftime("%Y-%m-%d", time.gmtime(ts / 1000.0))
        rec["thesis_id"] = f"{symbol}:{side_l}:{day}"
    return rec


def tag_exec_block_class(rec: dict, config: Any = None) -> dict:
    """S1 — mutate a copy with block_class=aria_exec when enabled."""
    out = dict(rec)
    if block_class_enabled(config):
        out.setdefault("block_class", BLOCK_EXEC)
    return out


def append_outbox(path: str, rec: dict) -> bool:
    """Append one JSONL row. Fail-open → False, never raises."""
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        return True
    except Exception:
        return False


def emit_capital_block(
    *,
    symbol: str,
    side: str,
    entry_ref: float,
    stop_price: float,
    capital_reason: str,
    tp1_price: Optional[float] = None,
    coherence: Optional[float] = None,
    aria_venue: Optional[str] = None,
    src: str = "capital_standdown",
    config: Any = None,
    outbox_path: Optional[str] = None,
    ts_ms: Optional[int] = None,
) -> Optional[dict]:
    """S2 — emit surplus row when kill switch allows + geometry intact.

    Returns the record written, or None when skipped / failed.
    """
    if not capital_surplus_enabled(config):
        return None
    reason = str(capital_reason or "").strip()
    if not reason:
        return None
    rec = build_signal(
        symbol=symbol,
        side=side,
        entry_ref=entry_ref,
        stop_price=stop_price,
        tp1_price=tp1_price,
        coherence=coherence,
        src=src,
        block_class=BLOCK_CAPITAL,
        capital_reason=reason,
        aria_venue=aria_venue,
        ts_ms=ts_ms,
    )
    if rec is None:
        return None
    path = outbox_path or default_outbox_path()
    if not append_outbox(path, rec):
        return None
    return rec


def geometry_from_candidate(candidate: Any) -> Optional[tuple]:
    """(entry, stop, tp1) from a Candidate-like object, else None."""
    try:
        entry = float(getattr(candidate, "entry_price", 0.0) or 0.0)
        stop = float(getattr(candidate, "stop_price", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    if entry <= 0 or stop <= 0:
        return None
    tp1 = getattr(candidate, "tp1_price", None)
    try:
        tp1_f = float(tp1) if tp1 is not None else None
    except (TypeError, ValueError):
        tp1_f = None
    return (entry, stop, tp1_f)


def emit_from_candidate(
    candidate: Any,
    *,
    capital_reason: str,
    side: Optional[str] = None,
    symbol: Optional[str] = None,
    coherence: Optional[float] = None,
    aria_venue: Optional[str] = None,
    src: str = "capital_standdown",
    config: Any = None,
    outbox_path: Optional[str] = None,
) -> Optional[dict]:
    """Standdown-site convenience: pull geometry off candidate, emit surplus."""
    geo = geometry_from_candidate(candidate)
    if geo is None:
        return None
    entry, stop, tp1 = geo
    sym = symbol or getattr(candidate, "symbol", None)
    sd = side or getattr(candidate, "side", None)
    if not sym or not sd:
        return None
    coh = coherence
    if coh is None:
        try:
            coh = float(getattr(candidate, "coherence_score", None) or 0.0) or None
        except (TypeError, ValueError):
            coh = None
    return emit_capital_block(
        symbol=str(sym),
        side=str(sd),
        entry_ref=entry,
        stop_price=stop,
        tp1_price=tp1,
        coherence=coh,
        capital_reason=capital_reason,
        aria_venue=aria_venue,
        src=src,
        config=config,
        outbox_path=outbox_path,
    )


def emit_exec_signal(
    *,
    symbol: str,
    side: str,
    entry_ref: float,
    stop_price: float,
    tp1_price: Optional[float] = None,
    coherence: Optional[float] = None,
    aria_venue: Optional[str] = None,
    src: str = "fast_cycle_approved",
    config: Any = None,
    outbox_path: Optional[str] = None,
    ts_ms: Optional[int] = None,
) -> Optional[dict]:
    """Fascia — anticipator approval → aria_exec outbox row.

    Kill: PROP_OUTBOX_FAST_CYCLE_EMIT=false / PROP_OUTBOX_ENABLED=false.
    Map hygiene refuses unmapped symbols (turbo2 cursor hygiene).
    """
    if not outbox_master_enabled():
        return None
    if not fast_cycle_emit_enabled(config):
        return None
    if not map_hygiene_allows(symbol):
        return None
    rec = build_signal(
        symbol=symbol,
        side=side,
        entry_ref=entry_ref,
        stop_price=stop_price,
        tp1_price=tp1_price,
        coherence=coherence,
        src=src,
        block_class=BLOCK_EXEC if block_class_enabled(config) else None,
        aria_venue=aria_venue,
        ts_ms=ts_ms,
    )
    if rec is None:
        return None
    path = outbox_path or default_outbox_path()
    if not append_outbox(path, rec):
        return None
    return rec


def emit_arcus_notional_block(
    *,
    symbol: str,
    side: str,
    entry_ref: float,
    stop_price: float,
    tp1_price: Optional[float] = None,
    coherence: Optional[float] = None,
    config: Any = None,
    outbox_path: Optional[str] = None,
    ts_ms: Optional[int] = None,
) -> Optional[dict]:
    """Fascia — Arcus below_min_notional → aria_capital_block (notional_floor).

    Thesis+geometry OK; sleeve dust cannot post. Harness may surplus-route.
    Kill: PROP_OUTBOX_ARCUS_NOTIONAL_EMIT=false; also PROP_CAPITAL_SURPLUS /
    PROP_OUTBOX_ENABLED / map hygiene.
    """
    if not outbox_master_enabled():
        return None
    if not arcus_notional_emit_enabled(config):
        return None
    if not map_hygiene_allows(symbol):
        return None
    return emit_capital_block(
        symbol=symbol,
        side=side,
        entry_ref=entry_ref,
        stop_price=stop_price,
        tp1_price=tp1_price,
        coherence=coherence,
        capital_reason="notional_floor",
        aria_venue="arcus",
        src="arcus_below_min_notional",
        config=config,
        outbox_path=outbox_path,
        ts_ms=ts_ms,
    )
