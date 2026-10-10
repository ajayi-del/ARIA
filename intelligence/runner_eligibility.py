"""
intelligence/runner_eligibility.py — RES: Runner Eligibility Score brain
(2026-10-10, Governor order: "build all paths live and in shadow").

Wave-2 runner-engineering lane. Math spec: /tmp/aria_wave1_quant.md §1
(every constant cites its code field). The September census verdict: entries
find edge, exits amputate the right tail (91.6% of stopped trades went green
first; the right tail carries 92% of gross positive PnL). RES is the ex-ante
classifier that decides which positions earn the ROE-ratchet runner doctrine
(rungs 3/6/9/15 → BE/45/60/70% trail, intelligence/roe_ratchet.py:54-61)
versus standard exits — and it is the measurement stamp that makes the
separation test answerable in 4-6 weeks (outcomes.db entry features are
degenerate; RES components must be logged at entry going forward).

Department shape (docs/DEPARTMENT_TEMPLATE.md):
  - Zero-I/O brain: every market read (tide verdict, regime confidence,
    whale percentile, pillar warmup, funding view) arrives as an argument;
    verdicts leave as dicts. The main.py splice owns all I/O (feed caches,
    regime engine, logging). This module imports nothing from main.py and
    reads no clock, no file, no env.
  - Kill switch: runner_eligibility_enabled=False → every verdict returns
    decision="disabled" with zero component scores and zero state mutation
    (the splice skips every call — pre-module system bit-for-bit).
    runner_demotion_enabled=False (DARK, Governor-gated) → demotion and
    abort floors are shadow-logged only (runner_res_reeval would_demote /
    runner_abort_would_fire); no live exit-path effect.
  - Fail-closed: any internal fault → decision="standard", res_score=None,
    reason=internal_error. The hot path must never feel this module.

RES math (spec §1, verified):
  Hard vetoes (any one → runner-ineligible; the trade may still proceed as
  standard):
    V1: tide_align == "opposed"          (sosovalue_feed.py:177-190)
    V2: whale_pct_against >= 90          (whale_ratio_feed.py:83-109;
                                          1.8:1 crowding ≈ pct 90)
  Bounded score RES ∈ [0,1]:
    RES = 0.25·T + 0.20·R + 0.15·C + 0.15·W + 0.15·P + 0.10·F
    T tide:     aligned 1.0 / neutral 0.4 / opposed → VETO
    R RS conf:  conf≥0.72→1.0 / ≥0.60→0.6 / else 0.2
                (RegimeState.confidence, relative_strength.py:437; gate idiom
                 main.py:11782)
    C category: symbol's category == leading→1.0 / == lagging→0.0 / else 0.4
                (rotation.py:60-69 pattern over ASSET_CATEGORIES)
    W whale:    AGAINST-direction crowding percentile ≤20→1.0 / 20-80→0.5 /
                ≥80→0.0 / None→0.5 (same axis as V2 — the veto is the hard
                extreme of the W axis)
    P pillars:  warm fraction ∈ {0,1/3,2/3,1} clamped [0,1]
                (main.py:1778-1788 _pyramid_warmup_legs; OI abstains)
    F funding:  favors 1.0 / neutral 0.5 / opposed 0.0
                (position.funding_aligned idiom; coherence.py:235-240)
  Decision: RES ≥ 0.70 → "runner"; 0.45–0.70 → "standard";
            < 0.45 → "standard_tight" (standard exits + tight-exit flag).
  Re-eval at each ratchet arm: fresh RES < 0.45 → would_demote (demotes to
  standard exits immediately once runner_demotion_enabled is armed).

Abort floors for runners (abort_verdict):
  A1 regime collapse       (fresh regime confidence < 0.60 — the
                            _regime_flip_monitor state, main.py:17941-17948;
                            the monitor's own 2-read hysteresis stays the
                            live exit path, this brain reads the fresh conf)
  A2 tide flip opposed     (tide_aligned == "opposed" on the fresh read)
  A3 whale ≥1.8:1 against  (raw ratio preferred: short → ratio ≥ 1.8,
                            long → ratio ≤ 1/1.8; percentile fallback
                            pct_against ≥ 90 when the raw ratio is dark)
  A4 funding sign flip     (funding view at entry was "favors" and the fresh
                            read is "opposed"; entry-view None abstains)

Pyramid add-path shadow (runner_plan_shadow — the parallel runner plan):
  tp1_cleared=True (shadow-only override of the pyramid.py:273 gate),
  warmup bar shadow-override fr-only (funding-rate leg present ⇒ warm),
  fixed-pct add triggers +4.5%/+9%/+12% at sizes 0.30/0.20/0.10 of base
  (replacing the ATR step), 25% runner kept at TP1 (stamped
  runner_keep_frac), whale mult overridden 0→1.0 (stamped, with the live
  percentile for context), funding-sign-flip abort blocks adds, breakeven
  floor after L1 mirrored BY CONSTRUCTION from aster_swing_floor_price
  (main.py — combined VWAP ∓ buffer, tighten-only). ALL SHADOW: the splice
  extends the pyramid_add_shadow record; zero live add effect.

Config knobs (core/config.py, injected cfg via getattr — FLOOR-style
add-only):
  runner_eligibility_enabled   True   — shadow tagging; safe from birth
  runner_demotion_enabled      False  — LIVE exit-path effect; DARK,
                                        Governor-gated (flipping requires
                                        endorsement)
  runner_res_runner            0.70   — RES_RUNNER threshold
  runner_res_tight             0.45   — RES_TIGHT threshold
  runner_plan_shadow_enabled   True   — pyramid add-path counterfactual
"""
from __future__ import annotations

from typing import Dict, List, Optional

SCHEMA_VERSION = 1

# Score weights (spec §1 — do not retune without a fresh quant review).
W_TIDE, W_RS, W_CAT, W_WHALE, W_PILLAR, W_FUNDING = (
    0.25, 0.20, 0.15, 0.15, 0.15, 0.10)

# Decision thresholds (defaults; the splice injects config overrides).
RES_RUNNER = 0.70
RES_TIGHT = 0.45

# Hard-veto / abort constants (spec §1).
WHALE_VETO_PCT = 90.0        # V2: against-direction crowding percentile
WHALE_ABORT_RATIO = 1.8      # A3: ≥1.8:1 against (raw ratio axis)
REGIME_COLLAPSE_CONF = 0.60  # A1: fresh regime confidence floor

# Runner-plan shadow constants (prior verified plan).
RUNNER_ADD_TRIGGERS_PCT = (4.5, 9.0, 12.0)   # fixed-pct price move rungs
RUNNER_ADD_SIZES = (0.30, 0.20, 0.10)        # fraction of base per rung
RUNNER_KEEP_FRAC = 0.25                       # 25% runner kept at TP1
RUNNER_BE_BUFFER = 0.004                      # aster_swing_floor_price idiom
RUNNER_WARMUP_BAR = 0.34                      # fr-only warmup override bar

DECISIONS = frozenset({"runner", "standard", "standard_tight", "disabled"})
FUNDING_VIEWS = frozenset({"favors", "neutral", "opposed"})


def _f(x) -> Optional[float]:
    """Coerce to float; None on any garbage (fail-closed idiom)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def tide_component(tide_align) -> float:
    """T ∈ {1.0, 0.4} — opposed never reaches this function (V1 veto)."""
    return 1.0 if str(tide_align or "").lower() == "aligned" else 0.4


def rs_component(regime_conf) -> float:
    """R — conf≥0.72→1.0 / ≥0.60→0.6 / else 0.2 (None = 0.2, conservative)."""
    c = _f(regime_conf)
    if c is None:
        return 0.2
    if c >= 0.72:
        return 1.0
    if c >= 0.60:
        return 0.6
    return 0.2


def category_component(category, leading_category, lagging_category) -> float:
    """C — leading 1.0 / lagging 0.0 / else 0.4 (rotation.py:60-69 pattern;
    'none'/'unknown' leaders never match)."""
    cat = str(category or "").strip()
    lead = str(leading_category or "").strip()
    lag = str(lagging_category or "").strip()
    if cat and lead and cat == lead and lead not in ("none", "unknown"):
        return 1.0
    if cat and lag and cat == lag and lag not in ("none", "unknown"):
        return 0.0
    return 0.4


def whale_component(whale_pct_against) -> float:
    """W — the SAME axis as veto V2 (against-direction crowding percentile):
    ≤20→1.0 / 20-80→0.5 / ≥80→0.0 / None→0.5 (dark-plane abstain)."""
    p = _f(whale_pct_against)
    if p is None:
        return 0.5
    if p <= 20.0:
        return 1.0
    if p >= 80.0:
        return 0.0
    return 0.5


def pillar_component(pillar_warm) -> float:
    """P — warm fraction clamped [0,1]; None = 0 (fail-closed: an unknown
    pillar plane earns nothing)."""
    w = _f(pillar_warm)
    if w is None:
        return 0.0
    return min(1.0, max(0.0, w))


def funding_component(funding_view) -> float:
    """F — favors 1.0 / neutral 0.5 / opposed 0.0; None/unknown = 0.5
    (dark-plane abstain, never penalizes)."""
    v = str(funding_view or "").lower()
    if v == "favors":
        return 1.0
    if v == "opposed":
        return 0.0
    return 0.5


def funding_view_for(side: str, funding_rate) -> str:
    """Classify a raw 8h funding rate for a held side. Positive rate =
    longs pay shorts (crowd long): favors shorts, opposed to longs.
    None → 'neutral' (dark-plane abstain). A dead-zero rate is neutral."""
    r = _f(funding_rate)
    if r is None or r == 0.0 or side not in ("long", "short"):
        return "neutral"
    if side == "long":
        return "opposed" if r > 0 else "favors"
    return "opposed" if r < 0 else "favors"


def whale_pct_against(side: str, ratio_percentile) -> Optional[float]:
    """Against-direction crowding percentile from the long-crowding
    percentile (ratio>1 = crowd long). Short → pct as-is; long → 100−pct.
    None propagates (thin history / dark plane)."""
    p = _f(ratio_percentile)
    if p is None or side not in ("long", "short"):
        return None
    p = min(100.0, max(0.0, p))
    return p if side == "short" else round(100.0 - p, 1)


def whale_ratio_against(side: str, ratio) -> Optional[float]:
    """The against-direction raw ratio. Short → ratio (crowd longs per
    short); long → 1/ratio (crowd shorts per long). None propagates."""
    r = _f(ratio)
    if r is None or r <= 0 or side not in ("long", "short"):
        return None
    return r if side == "short" else 1.0 / r


class RunnerEligibility:
    """The RES brain. Stateless across calls (the splice owns any position-
    keyed registries); constructor takes nothing I/O."""

    def __init__(self) -> None:
        pass

    # ── RES verdict ──────────────────────────────────────────────────────

    def evaluate(self, *, cfg, symbol: str, side: str,
                 tide_align=None, regime_conf=None,
                 category=None, leading_category=None,
                 lagging_category=None, whale_pct_against=None,
                 pillar_warm=None, funding_view=None) -> dict:
        """Full RES verdict for one candidate/position. Never raises.

        Returns {"enabled", "decision", "veto", "res_score", components
        res_T..res_F, thresholds used, "stamp" (the dense entry-stamp row
        the splice logs/attaches), "reason"}."""
        try:
            if not bool(getattr(cfg, "runner_eligibility_enabled", True)):
                return {"enabled": False, "decision": "disabled", "veto": None,
                        "res_score": None, "reason": "disabled", "stamp": {}}
            res_runner = float(getattr(cfg, "runner_res_runner", RES_RUNNER)
                               or RES_RUNNER)
            res_tight = float(getattr(cfg, "runner_res_tight", RES_TIGHT)
                              or RES_TIGHT)
            if side not in ("long", "short"):
                return {"enabled": True, "decision": "standard", "veto": None,
                        "res_score": None, "reason": "no_direction",
                        "stamp": {}}

            # ── Hard vetoes ──
            t_align = str(tide_align or "neutral").lower()
            if t_align == "opposed":
                veto = "V1_tide_opposed"
            else:
                veto = None
            wpa = _f(whale_pct_against)
            if veto is None and wpa is not None and wpa >= WHALE_VETO_PCT:
                veto = "V2_whale_crowding"
            if veto is not None:
                stamp = self._stamp(symbol, side, None, None, None, None,
                                    None, None, None, t_align, wpa,
                                    pillar_warm, regime_conf)
                return {"enabled": True, "decision": "standard", "veto": veto,
                        "res_score": None, "reason": veto, "stamp": stamp}

            # ── Components ──
            t = tide_component(t_align)
            r = rs_component(regime_conf)
            c = category_component(category, leading_category,
                                   lagging_category)
            w = whale_component(wpa)
            p = pillar_component(pillar_warm)
            f = funding_component(funding_view)
            res = (W_TIDE * t + W_RS * r + W_CAT * c + W_WHALE * w
                   + W_PILLAR * p + W_FUNDING * f)
            res = round(min(1.0, max(0.0, res)), 4)

            if res >= res_runner:
                decision = "runner"
            elif res >= res_tight:
                decision = "standard"
            else:
                decision = "standard_tight"
            stamp = self._stamp(symbol, side, res, t, r, c, w, p, f,
                                t_align, wpa, pillar_warm, regime_conf)
            return {"enabled": True, "decision": decision, "veto": None,
                    "res_score": res,
                    "res_T": t, "res_R": r, "res_C": c, "res_W": w,
                    "res_P": p, "res_F": f,
                    "res_runner_threshold": res_runner,
                    "res_tight_threshold": res_tight,
                    "would_demote": res < res_tight,
                    "reason": "", "stamp": stamp}
        except Exception:
            # Fail-closed: the hot path must never feel this module.
            return {"enabled": True, "decision": "standard", "veto": None,
                    "res_score": None, "reason": "internal_error", "stamp": {}}

    @staticmethod
    def _stamp(symbol, side, res, t, r, c, w, p, f,
               tide_align, whale_pct, pillar_warm, regime_conf) -> dict:
        """The dense entry-stamp row (spec §3 logging contract): one record,
        every RES input named — the separation test's feature set. Density
        law: no prose."""
        return {
            "schema": SCHEMA_VERSION,
            "symbol": symbol, "side": side,
            "res_score": res,
            "res_T": t, "res_R": r, "res_C": c, "res_W": w,
            "res_P": p, "res_F": f,
            "tide_align": tide_align,
            "whale_pct": whale_pct,
            "pillar_warm": (_f(pillar_warm)
                            if _f(pillar_warm) is not None else None),
            "regime_conf_live": (_f(regime_conf)
                                 if _f(regime_conf) is not None else None),
        }

    # ── Re-eval helper (ratchet arm) ─────────────────────────────────────

    def reeval(self, *, cfg, symbol: str, side: str, **kwargs) -> dict:
        """RES re-evaluation at a ratchet arm. Same math as evaluate plus the
        demotion read: fresh RES < RES_TIGHT ⇒ would_demote (live effect
        only under runner_demotion_enabled — DARK). Never raises."""
        out = self.evaluate(cfg=cfg, symbol=symbol, side=side, **kwargs)
        try:
            tight = float(getattr(cfg, "runner_res_tight", RES_TIGHT)
                          or RES_TIGHT)
            res = out.get("res_score")
            out["would_demote"] = bool(
                out.get("enabled") and res is not None and res < tight)
            out["demotion_live"] = bool(
                out["would_demote"]
                and getattr(cfg, "runner_demotion_enabled", False))
        except Exception:
            out["would_demote"] = False
            out["demotion_live"] = False
        return out

    # ── Abort floors ─────────────────────────────────────────────────────

    def abort_verdict(self, *, cfg, side: str, tide_align=None,
                      whale_ratio=None, whale_pct_against=None,
                      funding_view_entry=None, funding_view_now=None,
                      regime_conf_now=None) -> dict:
        """The four runner abort floors (shadow-logged runner_abort_would_
        fire; live effect only under runner_demotion_enabled). Never raises.
        Returns {"abort", "reasons", "checks", "demotion_live"}."""
        reasons: List[str] = []
        checks: Dict[str, bool] = {}
        try:
            if not bool(getattr(cfg, "runner_eligibility_enabled", True)):
                return {"abort": False, "reasons": [], "checks": {},
                        "demotion_live": False}

            # A1 regime collapse — fresh confidence read (the monitor's
            # hysteresis stays the live exit path; this is the floor read).
            conf = _f(regime_conf_now)
            checks["regime_collapse"] = bool(
                conf is not None and conf < REGIME_COLLAPSE_CONF)

            # A2 tide flip opposed.
            checks["tide_opposed"] = (
                str(tide_align or "").lower() == "opposed")

            # A3 whale ≥1.8:1 against — raw ratio preferred, percentile
            # fallback when the raw plane is dark.
            ra = whale_ratio_against(side, whale_ratio)
            if ra is not None:
                checks["whale_against"] = ra >= WHALE_ABORT_RATIO
            else:
                wpa = _f(whale_pct_against)
                checks["whale_against"] = bool(
                    wpa is not None and wpa >= WHALE_VETO_PCT)

            # A4 funding sign flip — entry "favors" → now "opposed".
            # Entry-view None abstains (no baseline, never invent a flip).
            fe = str(funding_view_entry or "").lower()
            fn = str(funding_view_now or "").lower()
            checks["funding_flip"] = (fe == "favors" and fn == "opposed")

            names = {"regime_collapse": "regime_collapse",
                     "tide_opposed": "tide_flip_opposed",
                     "whale_against": "whale_crowding_against",
                     "funding_flip": "funding_sign_flip"}
            for k, fired in checks.items():
                if fired:
                    reasons.append(names[k])
            return {"abort": bool(reasons), "reasons": reasons,
                    "checks": checks,
                    "demotion_live": bool(
                        reasons and getattr(cfg, "runner_demotion_enabled",
                                            False))}
        except Exception:
            return {"abort": False, "reasons": [], "checks": checks,
                    "demotion_live": False}


# ── Pyramid add-path shadow: the parallel runner plan ────────────────────


def runner_breakeven_floor(side: str, pre_size, pre_entry, add_qty, add_px,
                           buffer=RUNNER_BE_BUFFER) -> Optional[float]:
    """Breakeven floor after an add — mirrored BY CONSTRUCTION from
    aster_swing_floor_price (main.py; intelligence/ never imports main.py):
    combined VWAP ∓ buffer. Returns None on degenerate input (fail-closed).
    """
    ps, pe, aq, ap = (_f(pre_size), _f(pre_entry), _f(add_qty), _f(add_px))
    b = _f(buffer)
    if (ps is None or pe is None or aq is None or ap is None or b is None
            or ps <= 0 or pe <= 0 or aq <= 0 or ap <= 0
            or side not in ("long", "short")):
        return None
    comb = ps + aq
    if comb <= 0:
        return None
    vwap = (ps * pe + aq * ap) / comb
    return vwap * (1.0 - b) if side == "long" else vwap * (1.0 + b)


def runner_plan_shadow(*, cfg, side: str, base_entry, base_qty, mark,
                       rungs_fired, funding_view_entry=None,
                       funding_view_now=None, fr_leg_present=False,
                       whale_pct_live=None, res_score=None,
                       mfe_pct=None) -> dict:
    """The parallel runner-plan verdict for one pyramid-tracked position.

    rungs_fired: iterable of rung indices (0,1,2) the shadow already counts
    as fired — the SPLICE owns that registry (position-identity keyed).
    Returns {"would_add", "rung", "runner_qty", "add_px", "warmup_ok",
    "funding_abort", "floor_after_l1", "whale_mult", "whale_pct_live",
    "res_score", "mfe_since_entry", "runner_keep_frac", "reason"}.
    Never raises; zero live add effect by construction."""
    out = {"would_add": False, "rung": None, "runner_qty": None,
           "add_px": None, "warmup_ok": False, "funding_abort": False,
           "floor_after_l1": None, "whale_mult": 1.0,
           "whale_pct_live": _f(whale_pct_live),
           "res_score": _f(res_score),
           "mfe_since_entry": _f(mfe_pct),
           "runner_keep_frac": RUNNER_KEEP_FRAC, "reason": ""}
    try:
        if not bool(getattr(cfg, "runner_plan_shadow_enabled", True)):
            out["reason"] = "disabled"
            return out
        if not bool(getattr(cfg, "runner_eligibility_enabled", True)):
            out["reason"] = "disabled"
            return out
        if side not in ("long", "short"):
            out["reason"] = "no_direction"
            return out
        be, bq, mk = _f(base_entry), _f(base_qty), _f(mark)
        if be is None or bq is None or mk is None or be <= 0 or bq <= 0 \
                or mk <= 0:
            out["reason"] = "bad_geometry"
            return out

        # Funding-sign-flip abort blocks adds (A4 on the runner plan).
        fe = str(funding_view_entry or "").lower()
        fn = str(funding_view_now or "").lower()
        if fe == "favors" and fn == "opposed":
            out["funding_abort"] = True
            out["reason"] = "funding_sign_flip_abort"
            return out

        # Warmup bar shadow-override: fr-only (funding-rate leg present ⇒
        # warm — replaces the 0.80 multi-pillar bar for the runner plan).
        warm_fr = 1.0 if fr_leg_present else 0.0
        out["warmup_ok"] = warm_fr >= RUNNER_WARMUP_BAR
        if not out["warmup_ok"]:
            out["reason"] = "warmup_fr_dark"
            return out

        # Fixed-pct triggers (replacing the ATR step): direction-aware move.
        move_pct = ((mk - be) / be if side == "long"
                    else (be - mk) / be) * 100.0
        fired = set()
        try:
            fired = {int(x) for x in (rungs_fired or ())}
        except Exception:
            fired = set()
        for i, trig in enumerate(RUNNER_ADD_TRIGGERS_PCT):
            if i in fired:
                continue
            if move_pct >= trig:
                size_frac = RUNNER_ADD_SIZES[i]
                out["would_add"] = True
                out["rung"] = i
                out["runner_qty"] = round(bq * size_frac, 8)
                out["add_px"] = (be * (1.0 + trig / 100.0) if side == "long"
                                 else be * (1.0 - trig / 100.0))
                out["reason"] = f"trigger_{trig}pct"
                # Breakeven floor after THIS add (L1 idiom — tighten-only
                # is the splice's concern; the brain prices the floor).
                out["floor_after_l1"] = runner_breakeven_floor(
                    side, bq, be, bq * size_frac, mk)
                return out
            break  # triggers ascend — first un-fired un-hit rung ends it
        out["reason"] = (out["reason"] or "trigger_not_hit") if not fired \
            else "rungs_exhausted_or_not_hit"
        return out
    except Exception:
        out.update({"would_add": False, "rung": None, "runner_qty": None,
                    "add_px": None, "reason": "internal_error"})
        return out
