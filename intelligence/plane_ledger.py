"""
intelligence/plane_ledger.py — Execution-plane ledger (SCH-1/2/3).

CEO session 25 (2026-09-09), Governor directive "implement and redeploy
immediately": ARIA has TWO entry planes that share no join key — the GATED
standard path (SIGNAL_READY → quant filters → Kant → Nietzsche → Chancellor →
execution_decision → _bracket_task) and the cascade FASTPATH (momentum /
aftermath executors), and the fastpath ASSERTS coherence (8.0/9.0 constants)
instead of measuring it. The CEO's Sept census (n=200 deduped): fastpath =
47.5% of trades, 56.6% of losses, coherence NULL 100%. The gates govern the
smaller plane; the larger plane answers to nobody on the record.

This module is the measurement plane. One row per ENTRY ATTEMPT, appended
synchronously with the decision to logs/execution_plane_ledger.jsonl:

  identity    {ts_ms, symbol, side, attempt_id}
                attempt_id == trade_id == f"{symbol}_{opened_at_ms}" on fills
                (the trade_db join key); f"{symbol}_{decision_ts_ms}" otherwise
  plane       {plane, strategy_tag, executor, entry_path_site}
  gate_vector {coherence, base_rate, tide, quant_filters, kant_structure}
                — on the fastpath these are the BYPASSED gates, computed and
                LOGGED. Enforcing a gate on the fastpath is NOT authorised
                here; that needs the Governor. (SCH-2)
  sizing      {notional_usd, leverage, margin_usd, size_mults_applied}
  outcome_key {entry_id_uuid, trade_id, filled, fill_ts_ms, reject_reason}
                — entry_id_uuid is the journal UUID (captured at fill for the
                fastpath, at intent for the gated path); trade_db rows carry
                it from 2026-09-09 so ledger ⋈ trade_db ⋈ journal joins close.

Plane enum (amendment flagged to the CEO): {gated, fastpath, explosive,
whale_probe, unknown_adopted} — explosive/whale_probe are forward-declared
(v1 instruments gated + fastpath only); unknown_adopted labels positions the
startup sync adopted (no in-process entry path).

Department template: zero-I/O builders with injected values; one append
writer; kill switches whose False state reproduces the pre-module system
bit-for-bit (callers skip every emit and the measured-coherence read):

  EXECUTION_PLANE_LEDGER_ENABLED       (default true) — False: zero rows
  FASTPATH_GATE_VECTOR_ENABLED         (default true) — False: fastpath rows
                                       carry gate_vector=None
  FASTPATH_MEASURED_COHERENCE_ENABLED  (default true) — False: asserted-
                                       constant legacy (no measured read,
                                       no coherence stamps)

Budget: ~250 rows/day at current attempt rates (decision-depth only; the
upstream funnel stays in shadow_journal). One-bad-line doctrine: a row that
fails to serialize costs that row, never the file, never the trade path.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

import structlog

from intelligence.skeptic import base_rate_veto, VETO_MIN_N, VETO_WR_MARGIN

log = structlog.get_logger(__name__)

SCHEMA_VERSION = 1
PLANES = ("gated", "fastpath", "explosive", "whale_probe", "unknown_adopted")


# ── Kill switches ─────────────────────────────────────────────────────────────

def ledger_enabled() -> bool:
    return os.environ.get("EXECUTION_PLANE_LEDGER_ENABLED", "true").strip().lower() != "false"


def gate_vector_enabled() -> bool:
    return os.environ.get("FASTPATH_GATE_VECTOR_ENABLED", "true").strip().lower() != "false"


def measured_coherence_enabled() -> bool:
    return os.environ.get("FASTPATH_MEASURED_COHERENCE_ENABLED", "true").strip().lower() != "false"


# ── Gate-vector builders (zero-I/O; all values injected by the caller) ───────

def base_rate_vector(blended_wr, n, rr_ratio) -> dict:
    """Skeptic expectancy veto, logged not enforced (on the fastpath).

    Parity with intelligence.skeptic.base_rate_veto BY CONSTRUCTION (same
    function, same constants): breakeven = 1/(1+rr), invalid rr → 0.5;
    veto when n >= VETO_MIN_N and blended_wr < breakeven * VETO_WR_MARGIN.
    would_veto is None when the blend could not be computed (feed dark).
    """
    try:
        rr = float(rr_ratio or 0.0)
    except (TypeError, ValueError):
        rr = 0.0
    breakeven = 1.0 / (1.0 + rr) if rr > 0 else 0.5
    threshold = breakeven * VETO_WR_MARGIN
    would_veto: Optional[bool] = None
    if blended_wr is not None and n is not None:
        try:
            would_veto = bool(base_rate_veto(blended_wr, n, rr_ratio))
        except Exception:
            would_veto = None
    return {
        "blended_wr": round(float(blended_wr), 4) if blended_wr is not None else None,
        "n": int(n) if n is not None else None,
        "rr_ratio": round(rr, 4) if rr > 0 else None,
        "threshold": round(threshold, 4),
        "min_n": VETO_MIN_N,
        "would_veto": would_veto,
    }


def tide_vector(flow_usd, age_h, verdict) -> dict:
    """ETF institutional tide (SoSoValue). verdict precomputed by the caller
    via data.sosovalue_feed.tide_aligned ('aligned'|'opposed'|'neutral').
    would_veto mirrors the enforced veto on both paths: opposed AND fresh
    (tide_aligned already abstains stale >72h to 'neutral')."""
    return {
        "flow_usd": round(float(flow_usd), 0) if flow_usd is not None else None,
        "age_h": round(float(age_h), 1) if age_h is not None else None,
        "verdict": verdict,
        "would_veto": (verdict == "opposed") if verdict else None,
    }


def coherence_vector(measured, asserted, floor) -> dict:
    """SCH-3: what the path CLAIMED (asserted) vs what the signal MEASURED.
    measured=None means no measured state existed (asserted_constant source).
    would_block = measured < floor (the Kant min_coherence the standard path
    would have applied); None when unmeasurable."""
    wb: Optional[bool] = None
    if measured is not None and floor is not None:
        wb = bool(float(measured) < float(floor))
    return {
        "measured": round(float(measured), 3) if measured is not None else None,
        "asserted": round(float(asserted), 3) if asserted is not None else None,
        "floor": round(float(floor), 3) if floor is not None else None,
        "would_block": wb,
    }


def quant_filter_vector(*, side, htf, regime, coherence,
                        vc_zscore, vc_direction, vc_phase, events_60s, quiet_s,
                        is_tradfi, kant_accumulation, recovery_active,
                        atr_too_small=None, dispersion_ok=None,
                        dispersion_reason=None) -> dict:
    """The standard path's quant-filter hard blocks, replicated as pure
    predicates (main.py:~6305-6510). Evidence values ride the row so the
    cohort analysis never has to trust the flags alone.

    Replication notes (honest scope):
      - htf_counter_trend: opposed HTF blocks UNLESS confused regime (soft
        penalty), Kant ACCUMULATION probe (denied in recovery), or elite
        coherence >= 8.0 probe (denied in recovery) — all replicated.
      - quiet_market_pause: RAW predicate only; the standard path's
        aftermath/campaign/graduated bypasses are NOT replicated (the
        fastpath IS the aftermath class — the raw flag is the counterfactual
        of interest).
      - dispersion_gate: caller-evaluated via DispersionGate.should_trade;
        the standard path's rally/Hugo/micro-mode bypasses are NOT applied —
        dispersion_reason rides so the bypass classes are recoverable.
      - dead_market_atr: caller-evaluated; the momentum fastpath enforces
        its own ATR check upstream (main.py:3014).
    """
    out: dict = {}

    if htf not in ("bullish", "bearish"):
        out["htf_counter_trend"] = False
    else:
        opposed = ((htf == "bullish" and side == "short")
                   or (htf == "bearish" and side == "long"))
        if not opposed:
            out["htf_counter_trend"] = False
        elif regime == "confused":
            out["htf_counter_trend"] = False
        elif kant_accumulation and not recovery_active:
            out["htf_counter_trend"] = False
        elif (coherence or 0.0) >= 8.0 and not recovery_active:
            out["htf_counter_trend"] = False
        else:
            out["htf_counter_trend"] = True

    if vc_zscore > 2.0 and vc_direction not in ("mixed", "none", ""):
        with_dir = "short" if vc_direction == "bearish" else "long"
        out["cascade_counter_direction"] = bool(side != with_dir)
    else:
        out["cascade_counter_direction"] = False

    out["cascade_expansion_unfillable"] = bool(
        vc_phase == "expansion" and vc_zscore > 2.5)

    out["quiet_market_pause"] = bool(
        (not is_tradfi) and events_60s != 999
        and int(events_60s) < 40 and quiet_s > 1800.0)

    out["dead_market_atr"] = (bool(atr_too_small)
                              if atr_too_small is not None else None)
    out["dispersion_gate"] = ((not dispersion_ok)
                              if dispersion_ok is not None else None)
    out["dispersion_reason"] = dispersion_reason

    out["would_block_any"] = any(
        v is True for k, v in out.items()
        if k not in ("would_block_any", "dispersion_reason"))
    return out


# ── Row builder + writer ──────────────────────────────────────────────────────

def build_row(*, ts_ms, symbol, side, attempt_id, plane, strategy_tag,
              executor, entry_path_site, gate_vector, sizing,
              entry_id_uuid, trade_id, filled, fill_ts_ms,
              reject_reason) -> dict:
    """One ledger row. Pure — no validation raises; plane is free-text at the
    boundary (PLANES documents the enum; forward-declared members land before
    instrumentation does)."""
    return {
        "schema": SCHEMA_VERSION,
        "ts_ms": int(ts_ms),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_ms / 1000.0)),
        "identity": {
            "symbol": symbol,
            "side": side,
            "attempt_id": attempt_id,
        },
        "plane": {
            "plane": plane,
            "strategy_tag": strategy_tag,
            "executor": executor,
            "entry_path_site": entry_path_site,
        },
        "gate_vector": gate_vector,
        "sizing": sizing,
        "outcome_key": {
            "entry_id_uuid": entry_id_uuid,
            "trade_id": trade_id,
            "filled": bool(filled),
            "fill_ts_ms": fill_ts_ms,
            "reject_reason": reject_reason,
        },
    }


def append_row(path: str, row: dict) -> bool:
    """Append one JSON line. Never raises, never blocks the trade path.
    Kill switch False = no write (pre-module system bit-for-bit).
    One-bad-line doctrine: a serialization failure costs this row only."""
    if not ledger_enabled():
        return False
    try:
        line = json.dumps(row, default=str)
    except Exception as _se:
        log.warning("plane_ledger_serialize_failed", error=str(_se)[:120])
        return False
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception as _we:
        log.warning("plane_ledger_write_failed", error=str(_we)[:120])
        return False
