"""Sizing-chain decorrelation (Q4) — one responder per risk factor.

Incident (2026-09-17, HYPE long): six individually-bounded multiplicative
discounts composed into ~0.007x utilization on a Chancellor-APPROVED trade.
Several multipliers keyed on the SAME volatility regime, so vol risk was
priced three times (risk-parity, ATR-floor-resize, coherence-trim).

This module enforces a one-responder-per-factor doctrine over a collected
multiplier dict: walking a priority order, the FIRST discount (< 1.0) that
claims a factor keeps its value; any later discount sharing an already-
claimed factor is neutralized to 1.0. Multipliers >= 1.0 (boosts) never
claim factors — a boost is never a reason to silence a risk response.

HYPOTHESIS FLAG: MULTIPLIER_FACTORS is a factor-attribution hypothesis,
not ground truth. It was built from the incident narrative and validated
against main.py (see wiring spec in the Q4 report), but each mapping is a
judgment call about what a multiplier "responds to". Wrong attributions
neutralize the wrong knob. The audit trail exists so misclassifications
are discoverable from logs/sizing_decorrelation.jsonl before this ever
arms live sizing.

Stdlib only. Fail-silent everywhere: a defect here must never block or
distort a live trade.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

# ── Factor map (HYPOTHESIS — see module docstring) ──────────────────────────
# Factors: vol (volatility regime / stop distance), trend (directional
# regime), signal_quality (coherence/conviction/will stack), session
# (time-of-day quality).
MULTIPLIER_FACTORS: dict[str, frozenset[str]] = {
    # Starter map (semantic names from the incident waterfall):
    "tape_mult":       frozenset({"vol", "trend"}),
    "risk_parity":     frozenset({"vol"}),
    "conviction":      frozenset({"signal_quality"}),
    "will_gate":       frozenset({"signal_quality"}),
    "atr_floor_resize": frozenset({"vol"}),
    "coherence_decay": frozenset({"vol", "session"}),
    # Extended map — REAL multiplier sites found in main.py (2026-09-17
    # recon, standard path). Keys are the semantic names the wiring spec
    # collects under; comments give the main.py anchor.
    "regime_size":        frozenset({"trend"}),            # regime_size_mult ~:6182
    "streak":             frozenset({"signal_quality"}),   # _streak_mult ~:6195
    "guardian_tier":      frozenset({"signal_quality"}),   # _late_g.size_mult ~:6238
    "dd_combined":        frozenset({"drawdown", "session"}),  # _combined_mult ~:6276
    "calendar_earnings":  frozenset({"event"}),            # x0.5 ~:6291
    "correlation_cap":    frozenset({"correlation"}),      # _corr_mult ~:6316
    "whale_tac":          frozenset({"evidence"}),         # _whale_mult ~:6355 (boost-only)
    "etf_tide":           frozenset({"trend"}),            # _etf_mult ~:6413
    "emerging_trend":     frozenset({"trend"}),            # _emergent_mult ~:6439 (boost-only)
    "session_mult":       frozenset({"session"}),          # _sess_mult ~:6481
    "aftermath_probe":    frozenset({"event"}),            # x0.50 ~:6644
    "ecs_recovery":       frozenset({"drawdown"}),         # _ecs_size_mult ~:6718
    "recovery_cap":       frozenset({"drawdown"}),         # _rec_size_cap ~:6799
    "liq_phase":          frozenset({"vol"}),              # _phase_size_mult ~:6816
    "nietzsche":          frozenset({"signal_quality", "drawdown"}),  # ~:8470
    "kelly_corr":         frozenset({"correlation"}),      # _kelly_corr_mult ~:8399
    "will_scale":         frozenset({"signal_quality"}),   # _w_verdict.size_scale ~:8511
    "convergence":        frozenset({"event"}),            # x0.5 ~:8617
    "vol_stop_resize":    frozenset({"vol"}),              # _orig_dist/_new_dist ~:20128
}

# Default priority: the authoritative vol knob first (ATR-floor resize
# enforces constant dollar risk — it is the vol responder that must never
# be neutralized), then the remaining vol readers, then quality stack.
DEFAULT_PRIORITY: list[str] = [
    "atr_floor_resize",
    "vol_stop_resize",
    "risk_parity",
    "liq_phase",
    "tape_mult",
    "coherence_decay",
    "dd_combined",
    "ecs_recovery",
    "recovery_cap",
    "regime_size",
    "etf_tide",
    "guardian_tier",
    "nietzsche",
    "conviction",
    "will_scale",
    "will_gate",
    "streak",
    "correlation_cap",
    "kelly_corr",
    "session_mult",
    "calendar_earnings",
    "aftermath_probe",
    "convergence",
]

_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "sizing_decorrelation.jsonl"


def decorrelation_enabled() -> bool:
    """Kill-switch wrapper (state/kill_switches.json key sizing_decorrelation).

    Fail-silent: any import/read error -> False (module disarmed).
    """
    try:
        from intelligence import kill_switch
        return bool(kill_switch.enabled("sizing_decorrelation"))
    except Exception:
        return False


def decorrelate(
    multipliers: dict[str, float],
    priority: Optional[list[str]] = None,
) -> tuple[dict[str, float], dict]:
    """Neutralize duplicate factor responders in a multiplier dict.

    Walk in priority order (then any names not in the priority list, in
    input order). The first multiplier < 1.0 claiming a factor keeps its
    value; later multipliers (< 1.0) sharing a claimed factor are set to
    1.0. Multipliers >= 1.0 never claim factors. Unknown names (no factor
    entry) never claim and are never neutralized.

    Returns (adjusted, audit). Never raises.
    """
    try:
        order: list[str] = list(priority) if priority else list(DEFAULT_PRIORITY)
        order += [n for n in multipliers if n not in order]
        adjusted: dict[str, float] = dict(multipliers)
        claimed: dict[str, str] = {}        # factor -> keeper name
        neutralized: list[dict] = []
        keepers: list[str] = []
        for name in order:
            if name not in multipliers:
                continue
            try:
                value = float(multipliers[name])
            except Exception:
                continue
            if value >= 1.0:
                continue  # boosts never claim, never neutralized
            factors = MULTIPLIER_FACTORS.get(name, frozenset())
            overlap = sorted(f for f in factors if f in claimed)
            if overlap:
                adjusted[name] = 1.0
                neutralized.append({
                    "name": name,
                    "value": value,
                    "shared_factors": overlap,
                    "claimed_by": sorted({claimed[f] for f in overlap}),
                    "reason": "factor_already_claimed",
                })
            else:
                for f in factors:
                    claimed[f] = name
                if factors:
                    keepers.append(name)
        audit = {
            "priority": order,
            "keepers": keepers,
            "claimed": dict(claimed),
            "neutralized": neutralized,
            "raw_composite": composite(multipliers),
            "adjusted_composite": composite(adjusted),
        }
        return adjusted, audit
    except Exception:
        return dict(multipliers), {"error": "decorrelate_failed"}


def composite(m: dict[str, float]) -> float:
    """Product of all multiplier values. Fail-silent (bad values skipped)."""
    product = 1.0
    try:
        for v in m.values():
            try:
                product *= float(v)
            except Exception:
                continue
    except Exception:
        pass
    return product


def apply(
    base_size: float,
    multipliers: dict[str, float],
    priority: Optional[list[str]] = None,
) -> tuple[float, dict]:
    """Decorrelate and apply to a base size. Returns (final_size, audit).

    Fail-silent: on any error the RAW composite is applied (pre-module
    behavior) and the audit carries the error.
    """
    try:
        adjusted, audit = decorrelate(multipliers, priority)
        final_size = float(base_size) * composite(adjusted)
        audit["base_size"] = float(base_size)
        audit["final_size"] = final_size
        return final_size, audit
    except Exception:
        raw = float(base_size) * composite(multipliers)
        return raw, {"error": "apply_failed", "final_size": raw}


def log_audit(symbol: str, raw: dict[str, float], adjusted: dict[str, float],
              audit: Optional[dict] = None) -> None:
    """Append one shadow record to logs/sizing_decorrelation.jsonl.

    Fail-silent: logging must never raise into the sizing path.
    """
    try:
        neutralized = (audit or {}).get("neutralized", [])
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "symbol": symbol,
            "raw": {k: float(v) for k, v in raw.items()},
            "adjusted": {k: float(v) for k, v in adjusted.items()},
            "raw_composite": composite(raw),
            "adjusted_composite": composite(adjusted),
            "neutralized": [n.get("name") for n in neutralized],
        }
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception:
        pass
