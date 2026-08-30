"""Whale Absorption Signal (WAS) — true vs false absorption discrimination.

Deploy 2026-08-30 (spec audit amendments baked in). SHADOW-ONLY: this
module emits shadow-journal candidates (gate "whale_absorption"); it never
creates, sizes, or vetoes a live order.

THE DOCTRINE. A liquidation cascade is forced flow — sellers (or buyers)
who MUST transact regardless of price. The question that pays is whether
someone is ABSORBING that flow. TRUE absorption: forced selling + a large
buyer (identity evidence: whale flows on the absorbing side) + price-impact
compression (the forced notional barely moves price) + depth replenishment
(the absorbing wall refills after each hit) + post-event stabilization
(price holds once the forcing stops). FALSE absorption: the same burst
with no buyer identity and no stabilization — a falling knife that keeps
falling. Every leg is a FEATURE recorded for the shadow journal; the
calibrated layer learns which bands pay. No hard thresholds on
absorption_ratio (audit amendment #6 — it is shadowed across the 0.02-0.50
band, never gated).

GRADUATION GATE (constants, not code — live influence requires ALL):
  n ≥ 50 scored shadow records AND cost-adjusted EV > +0.15R AND lower
  95% CI > 0 AND profit factor > 1.15 AND no catastrophic regime bucket
  AND MAE compatible with the proposed stop AND slippage < edge AND an
  out-of-sample split passes. If ever graduated: initial max risk
  0.25-0.5× an ordinary entry (audit amendment — a new signal class earns
  size, it is not granted size).

THESIS HALF-LIFE (metadata carried on every emission — the eventual stop
is structural + volatility + thesis-decayed, NOT a fixed clock):
  0-15 min full weight / 15-60 min decay / 60-180 min reduced /
  >180 min stale — requires fresh evidence.

State machine per symbol: IDLE → ARMED (forced window observed) →
STABILIZING (forcing stopped; knife check) → emit → COOLDOWN.
Zero-I/O brain: every external read is an injected callable.
"""
import time

from intelligence.whale_evidence import SignalEvidence, effective_breadth

# Forced-window arming (liq_phase_engine readings)
_ARM_PHASES = ("expansion", "exhaustion")
_ARM_MIN_Z = 2.5
# Stabilization (the anti-falling-knife confirmation)
_STAB_WINDOW_S = 300.0
_STAB_TOLERANCE_PCT = 0.004   # 0.4% adverse drift during stabilization = knife
# Materiality: a forced window under this notional is noise, not a cascade
_MIN_FORCED_NOTIONAL_USD = 250_000.0
# Footprint leg: absorbing-side depth must refill by this ratio vs arm-time
_REPLENISH_RATIO = 1.2
# Emission classes
CLASS_TRUE = "true_absorption"          # identity leg present (whales absorbed)
CLASS_FOOTPRINT = "footprint_only"      # book refill without whale identity
_COOLDOWN_S = 1800.0
# Thesis half-life bands (metadata for the eventual graduated consumer)
THESIS_BANDS = ((900.0, "full"), (3600.0, "decay"),
                (10800.0, "reduced"), (float("inf"), "stale"))


def thesis_band(age_s: float) -> str:
    for ceiling, name in THESIS_BANDS:
        if age_s <= ceiling:
            return name
    return "stale"


class WhaleAbsorption:
    """One supervised tick per symbol; emits SignalEvidence candidates for
    the shadow splice. Never raises."""

    def __init__(self, liq_snap_fn, forced_notional_fn, whale_flows_fn,
                 book_depth_fn, price_fn, price_change_fn,
                 min_forced_notional_usd: float = _MIN_FORCED_NOTIONAL_USD,
                 time_fn=time.time):
        self._snap = liq_snap_fn            # symbol → LiqPhaseSnapshot|None
        self._forced = forced_notional_fn   # (symbol, liq_dir, window_s) → usd
        self._flows = whale_flows_fn        # (symbol, direction) → [flows]
        self._depth = book_depth_fn         # (symbol, side) → usd|None
        self._price = price_fn              # symbol → mark|None
        self._px_chg = price_change_fn      # (symbol, window_s) → pct|None
        self._min_forced = float(min_forced_notional_usd)
        self._time = time_fn
        self._state = {}                    # symbol → dict

    # ── State helpers ─────────────────────────────────────────────────────

    def _st(self, symbol: str) -> dict:
        return self._state.setdefault(symbol, {"phase": "IDLE"})

    def _reset(self, symbol: str, cooldown: bool = False) -> None:
        self._state[symbol] = {
            "phase": "COOLDOWN" if cooldown else "IDLE",
            "until": (self._time() + _COOLDOWN_S) if cooldown else None}

    # ── Main tick ─────────────────────────────────────────────────────────

    def tick(self, symbol: str) -> dict | None:
        """Advance the per-symbol machine. Returns a SignalEvidence dict on
        emission (TRUE or FOOTPRINT class), else None. Never raises."""
        try:
            return self._tick_inner(symbol)
        except Exception:
            return None

    def _tick_inner(self, symbol: str) -> dict | None:
        now = self._time()
        st = self._st(symbol)
        phase = st["phase"]
        if phase == "COOLDOWN":
            if st.get("until") and now >= st["until"]:
                self._reset(symbol)
            return None

        snap = self._snap(symbol)
        snap_phase = str(getattr(snap, "phase", "")).split(".")[-1].lower() \
            if snap is not None else ""
        z = abs(float(getattr(snap, "zscore", 0.0) or 0.0))
        liq_dir = getattr(snap, "last_direction", "none") if snap else "none"
        armed = snap_phase in _ARM_PHASES and z >= _ARM_MIN_Z \
            and liq_dir in ("bearish", "bullish")

        if phase == "IDLE":
            if not armed:
                return None
            # Forced selling (bearish liqs) → absorption candidate is LONG
            absorb_dir = "long" if liq_dir == "bearish" else "short"
            st.update({
                "phase": "ARMED", "armed_at": now, "liq_dir": liq_dir,
                "absorb_dir": absorb_dir,
                "price_at_arm": self._price(symbol),
                "depth_at_arm": self._depth(symbol, absorb_dir),
                "z_at_arm": z, "phase_at_arm": snap_phase,
            })
            return None

        if phase == "ARMED":
            if armed:
                return None            # forced window still running
            # Forcing stopped → stabilization watch begins
            forced_usd = self._forced(symbol, st["liq_dir"],
                                      now - st["armed_at"] + 60.0)
            if forced_usd < self._min_forced:
                self._reset(symbol)    # noise burst, not a cascade
                return None
            st.update({"phase": "STABILIZING", "stab_start": now,
                       "stab_price": self._price(symbol),
                       "forced_usd": forced_usd})
            return None

        if phase == "STABILIZING":
            px = self._price(symbol)
            ref = st.get("stab_price")
            if px is None or ref is None:
                self._reset(symbol)
                return None
            adverse = ((ref - px) / ref) if st["absorb_dir"] == "long" \
                else ((px - ref) / ref)
            if adverse > _STAB_TOLERANCE_PCT:
                # Falling knife: stabilization FAILED — no candidate, and
                # the FALSE-class observation is as valuable as a true one
                self._reset(symbol, cooldown=True)
                self._state[symbol]["false_absorption"] = True
                return None
            if now - st["stab_start"] < _STAB_WINDOW_S:
                return None
            # Stabilization survived → classify and emit
            ev = self._emit(symbol, st, now, px)
            self._reset(symbol, cooldown=True)
            return ev
        return None

    # ── Emission ──────────────────────────────────────────────────────────

    def _emit(self, symbol: str, st: dict, now: float,
              px: float) -> dict | None:
        absorb = st["absorb_dir"]
        flows = self._flows(symbol, absorb) or []
        whale_notional = 0.0
        for f in flows:
            try:
                whale_notional += abs(float(f.get("notional_delta_usd")
                                            or 0.0))
            except (TypeError, ValueError):
                continue
        forced = max(float(st.get("forced_usd") or 0.0), 1.0)
        absorption_ratio = (whale_notional / forced) if whale_notional > 0 \
            else None
        # Impact efficiency: how much price the forced notional bought.
        # Compression (LOW efficiency despite HIGH forced notional) IS the
        # absorption signature. Per $1M of forced flow for readability.
        p0, p1 = st.get("price_at_arm"), st.get("stab_price")
        impact_pct = (abs(p1 - p0) / p0 * 100.0) if p0 and p1 else None
        impact_eff = (impact_pct / (forced / 1e6)) \
            if impact_pct is not None else None
        # Footprint: did the absorbing wall refill?
        d0 = st.get("depth_at_arm")
        d1 = self._depth(symbol, absorb)
        replenish = (d1 / d0) if d0 and d1 and d0 > 0 else None
        identity = len(flows) > 0
        footprint = replenish is not None and replenish >= _REPLENISH_RATIO
        if not identity and not footprint:
            return None                  # no absorbing evidence at all
        klass = CLASS_TRUE if identity else CLASS_FOOTPRINT
        breadth = effective_breadth(flows) if flows else 0.0
        ev = SignalEvidence(
            event_type="whale_absorption", symbol=symbol, direction=absorb,
            confidence=None,             # learned — shadow journal
            freshness_s=round(now - st["stab_start"], 1),
            effective_breadth=round(breadth, 3),
            features={
                "class": klass,
                "absorption_ratio": (round(absorption_ratio, 4)
                                     if absorption_ratio is not None else None),
                "impact_efficiency_per_1m": (round(impact_eff, 4)
                                             if impact_eff is not None else None),
                "replenishment_ratio": (round(replenish, 3)
                                        if replenish is not None else None),
                "forced_notional_usd": round(forced, 0),
                "whale_notional_usd": round(whale_notional, 0),
                "n_whale_flows": len(flows),
                "z_at_arm": round(st.get("z_at_arm", 0.0), 2),
                "phase_at_arm": st.get("phase_at_arm"),
                "armed_at": st.get("armed_at"),
                "stabilization_s": _STAB_WINDOW_S,
                "thesis_half_life": {name: ceil for ceil, name in THESIS_BANDS
                                     if ceil != float("inf")},
                "entry_ref_price": px,
            })
        return ev.to_dict()
