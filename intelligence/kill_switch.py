"""Runtime kill switches for tunable mechanisms (2026-09-17 quant-filter set).

Reads state/kill_switches.json with a 5s cache so the Governor can arm/disarm
modules live without a restart. Fail-safe: missing or corrupt file falls back
to DEFAULTS (behavior-changing modules OFF, measure-only shadows ON). Never
raises.
"""
import json
import time
from pathlib import Path

_PATH = Path(__file__).resolve().parent.parent / "state" / "kill_switches.json"
_CACHE_TTL_S = 5.0

DEFAULTS = {
    "veto_freshness": False,        # Q1: regime-keyed veto invalidation + TTL/decay
    "veto_loo_report": True,        # Q2: leave-one-out marginal-cost shadow report
    "mover_relief_v2": False,       # Q3: two-stage ALERT/RELIEF arming
    "sizing_decorrelation": False,  # Q4: one-vol-responder multiplier audit
    "rr_shadow_cohort": True,       # Q5: fixed-TP vs rerunged-TP shadow (measure-only)
    "trim_price_confirm": False,    # Q6: adverse-price confirm before exit trims
    "breakout_coherence_shadow": True,  # 4-pillar coherence scorer (measure-only)
    "coherence_veto_override": False,   # coherence >= threshold overrides named vetoes
    "squeeze_scanner_shadow": True,     # squeeze-pipeline watchlist (measure-only)
}

_cache: dict = {}
_cache_ts: float = 0.0


def enabled(name: str) -> bool:
    global _cache, _cache_ts
    now = time.monotonic()
    if now - _cache_ts > _CACHE_TTL_S:
        try:
            _cache = json.loads(_PATH.read_text())
            if not isinstance(_cache, dict):
                _cache = {}
        except Exception:
            _cache = {}
        _cache_ts = now
    return bool(_cache.get(name, DEFAULTS.get(name, False)))


def reset_cache() -> None:
    global _cache_ts
    _cache_ts = 0.0
