"""Equity colony (2026-09-22, Governor: "find subsystems that connect with
the new coin additions like an internal colony — use ants as a guide").

Ant-colony optimization over the equity-perp rotation circuit. The colony
is the EDGE SET: leader->follower trails between sub-families, discovered
by registration (the subsystem graph) and REINFORCED by measured outcomes
(stigmergy — pheromone is realized PnL, not narrative). Trails that make
money strengthen; trails that lose decay; everything evaporates slowly
back toward neutral so stale paths are forgotten.

Sub-families (Governor framework 2026-09-22):
  INDEX          USTECH100, SPCX           — the macro anchor
  TECH_MEGA      GOOGL, AMZN, MSFT, META, AAPL
  AI_SEMIS       AMD, TSM, SMCI, SAMSUNG, SKHX, DRAM, NVDA, LITE
  CRYPTO_ADJ     COIN, HOOD                — bridge to the crypto complex

Rotation circuit (leader -> followers):
  INDEX  -> TECH_MEGA, AI_SEMIS
  MEGA   -> AI_SEMIS                       (GOOGL/AMZN confirm, semis lag)
  SEMIS  -> CRYPTO_ADJ                     (risk appetite reaches the bridge)

A leader whose |day move| >= leader_move_pct arms trails to every
follower in its downstream sub-families: the follower earns a bounded
SIZE boost in the leader's direction (mult = 1 + boost_max * weight,
hard cap 1 + boost_max). Direction-aware both ways (leader down ->
follower SHORT boost). SIZE-side only — never a gate, never coherence.

Funding-carry leg (the SKHX case): the exchange pays the crowded side's
counterparty. funding <= -carry_threshold + LONG, or >= +threshold +
SHORT, earns x(1 + carry_boost), weight-adjusted, same cap.

Gap-fade leg (framework playbook): overnight gap vs the rolling 16:00
ET anchor, faded after the 30-min post-open settle (never in PRE_MARKET
— the gap is still forming). Bucket-scaled by |gap|: 1-3% -> 0.4x,
3-7% -> 0.75x, >7% -> 1.0x of gap_boost_max. Counter-gap direction
only; with-gap candidates abstain.

Pair-convergence leg (COIN/HOOD bridge): when a CRYPTO_ADJ peer runs
>= pair_spread_pct ahead of the symbol on the day, the laggard in the
convergence direction earns x(1 + pair_boost), weight-adjusted, same cap.

Pheromone mechanics (bounded, deterministic):
  weight(edge) in [w_min, w_max], init 1.0
  close on a boosted follower: win -> w *= win_mult, loss -> w *= loss_mult
  every evaporate pass: w -> 1 + (w-1) * evap   (slow return to neutral)
  persistence: atomic JSON mirror, restored at boot (one-bad-line)

Kill switch: config.equity_colony_enabled=False = pre-module sizing
bit-for-bit. Pure brain: all market data injected, zero I/O except the
explicit persistence helpers.
"""

from __future__ import annotations

import json
import os
import time

SUBFAMILIES: dict[str, tuple[str, ...]] = {
    "INDEX":      ("USTECH100-USD", "SPCX-USD"),
    "TECH_MEGA":  ("GOOGL-USD", "AMZN-USD", "MSFT-USD", "META-USD",
                   "AAPL-USD"),
    "AI_SEMIS":   ("AMD-USD", "TSM-USD", "SMCI-USD", "SAMSUNG-USD",
                   "SKHX-USD", "DRAM-USD", "NVDA-USD", "LITE-USD"),
    "CRYPTO_ADJ": ("COIN-USD", "HOOD-USD"),
}

# leader sub-family -> follower sub-families (the rotation circuit)
CIRCUIT: dict[str, tuple[str, ...]] = {
    "INDEX":      ("TECH_MEGA", "AI_SEMIS"),
    "TECH_MEGA":  ("AI_SEMIS",),
    "AI_SEMIS":   ("CRYPTO_ADJ",),
}

_SYM_FAMILY: dict[str, str] = {
    sym: fam for fam, syms in SUBFAMILIES.items() for sym in syms
}

ALL_COLONY_SYMBOLS: frozenset[str] = frozenset(_SYM_FAMILY)


class EquityColony:
    """Pheromone-weighted leader->follower boost engine."""

    def __init__(self, leader_move_pct: float = 1.0,
                 boost_max: float = 0.25,
                 carry_threshold: float = 0.0001,
                 carry_boost: float = 0.10,
                 gap_min_pct: float = 1.0,
                 gap_boost_max: float = 0.20,
                 gap_settle_min: int = 30,
                 gap_settle_small_min: int = 5,
                 gap_settle_mid_min: int = 15,
                 pair_spread_pct: float = 2.0,
                 pair_boost: float = 0.12,
                 trail_halflife_s: float = 1800.0,
                 trail_min_scale: float = 0.10,
                 win_mult: float = 1.05, loss_mult: float = 0.95,
                 evap: float = 0.999,
                 w_min: float = 0.5, w_max: float = 2.0,
                 clock=time.time):
        self.leader_move_pct = float(leader_move_pct)
        self.boost_max = float(boost_max)
        self.carry_threshold = float(carry_threshold)
        self.carry_boost = float(carry_boost)
        self.gap_min_pct = float(gap_min_pct)
        self.gap_boost_max = float(gap_boost_max)
        self.gap_settle_min = int(gap_settle_min)
        self.gap_settle_small_min = int(gap_settle_small_min)
        self.gap_settle_mid_min = int(gap_settle_mid_min)
        self.pair_spread_pct = float(pair_spread_pct)
        self.pair_boost = float(pair_boost)
        self.trail_halflife_s = float(trail_halflife_s)
        self.trail_min_scale = float(trail_min_scale)
        self.win_mult = float(win_mult)
        self.loss_mult = float(loss_mult)
        self.evap = float(evap)
        self.w_min = float(w_min)
        self.w_max = float(w_max)
        self._clock = clock
        self.weights: dict[str, float] = {}     # "LEADER->FOLLOWER" -> w
        self._armed_at: dict[str, float] = {}   # trail key -> arm epoch

    # ── trails ─────────────────────────────────────────────────────────

    @staticmethod
    def _edge(leader: str, follower: str) -> str:
        return f"{leader}->{follower}"

    def _weight(self, edge: str) -> float:
        return float(self.weights.get(edge, 1.0))

    def downstream(self, leader: str) -> list[str]:
        fam = _SYM_FAMILY.get(leader)
        if fam is None:
            return []
        out: list[str] = []
        for ffam in CIRCUIT.get(fam, ()):
            out.extend(s for s in SUBFAMILIES[ffam] if s != leader)
        return out

    def _decay_scale(self, key: str) -> float:
        """Pheromone trail recency: 0.5^(elapsed/halflife). A leader move
        transmits its signal fast — without decay the colony is a
        persistent bias injector, not a rotation signal. Below
        trail_min_scale the trail is dead."""
        try:
            t0 = self._armed_at.get(key)
            if t0 is None:
                return 1.0
            elapsed = max(0.0, self._clock() - float(t0))
            return 0.5 ** (elapsed / self.trail_halflife_s)
        except Exception:
            return 1.0

    def _gap_window_open(self, gp_abs: float) -> bool:
        """Fade windows only: post-settle CORE + AFTER_HOURS. PRE_MARKET
        (gap still forming) abstains, and the post-open settle is TIERED
        by gap size — never fade at gap detection; large gaps need the
        full 30 min of opening volume (the wait is load-bearing).
        Fail-open False."""
        from intelligence import equity_session as _es
        m = _es.et_minute_of_day(self._clock())
        if m is None:
            return False
        if 4 * 60 <= m < 9 * 60 + 30:           # PRE_MARKET
            return False
        settle = (self.gap_settle_small_min if gp_abs < 3.0
                  else self.gap_settle_mid_min if gp_abs < 7.0
                  else self.gap_settle_min)
        if 9 * 60 + 30 <= m < 9 * 60 + 30 + settle:
            return False
        return True

    # ── reads ──────────────────────────────────────────────────────────

    def boost(self, symbol: str, direction: str,
              day_moves: dict, funding_rate: float | None,
              gaps: dict | None = None) -> tuple:
        """(mult, trail) for a candidate. trail is None when no trail fired
        (mult 1.0 — the candidate is invisible to the colony).
        day_moves: {symbol: pct-from-midnight} for leader symbols (injected).
        gaps: {symbol: signed overnight-gap pct vs the 16:00 ET anchor}.
        Fail-open: any malformed input abstains with (1.0, None)."""
        try:
            if symbol not in _SYM_FAMILY:
                return 1.0, None
            direction = str(direction).lower()
            gaps = gaps or {}
            # trail arming pass (candidate-independent): a leader arms when
            # its |day move| crosses the threshold with downstream followers;
            # it disarms below threshold — and a GAPPED leader never arms
            # (a gap is event noise, not rotation; without this guard the
            # colony arms bullish trails on the very name being faded).
            for leader, move in (day_moves or {}).items():
                try:
                    mv = float(move)
                except (TypeError, ValueError):
                    continue
                key = f"L:{leader}"
                try:
                    _gl = gaps.get(leader)
                    gapped = (_gl is not None
                              and abs(float(_gl)) >= self.gap_min_pct)
                except (TypeError, ValueError):
                    gapped = False
                if (abs(mv) < self.leader_move_pct or gapped
                        or not self.downstream(leader)):
                    self._armed_at.pop(key, None)
                elif key not in self._armed_at:
                    self._armed_at[key] = self._clock()
            best_score, best_trail = 0.0, None
            # leader->follower trails (recency-decayed)
            for leader, move in (day_moves or {}).items():
                try:
                    mv = float(move)
                except (TypeError, ValueError):
                    continue
                if abs(mv) < self.leader_move_pct:
                    continue
                if f"L:{leader}" not in self._armed_at:
                    continue              # disarmed or gap-suppressed
                if symbol not in self.downstream(leader):
                    continue
                want = "long" if mv > 0 else "short"
                if want != direction:
                    continue
                scale = self._decay_scale(f"L:{leader}")
                if scale < self.trail_min_scale:
                    continue
                edge = self._edge(leader, symbol)
                score = self._weight(edge) * scale
                if score > best_score:
                    best_score, best_trail = score, ("leadlag", leader,
                                                     round(mv, 2))
            # funding-carry trail
            try:
                fr = None if funding_rate is None else float(funding_rate)
            except (TypeError, ValueError):
                fr = None
            if fr is not None and ((direction == "long"
                                    and fr <= -self.carry_threshold) or
                                   (direction == "short"
                                    and fr >= self.carry_threshold)):
                edge = self._edge("CARRY", symbol)
                score = self._weight(edge)          # funding persists — no decay
                if score > best_score:
                    best_score, best_trail = score, ("carry", "CARRY",
                                                     round(fr, 6))
            # gap-fade trail (counter-gap direction, fade windows only)
            try:
                gp = None if gaps is None else gaps.get(symbol)
                gp = None if gp is None else float(gp)
            except (TypeError, ValueError, AttributeError):
                gp = None
            if (gp is not None and abs(gp) >= self.gap_min_pct
                    and self._gap_window_open(abs(gp))):
                want = "short" if gp > 0 else "long"
                if want == direction:
                    edge = self._edge("GAP", symbol)
                    score = self._weight(edge)      # self-decaying: the gap
                    if score > best_score:          # shrinks as price retraces
                        best_score, best_trail = score, ("gapfade", "GAP",
                                                         round(gp, 2))
            # pair-convergence trail (CRYPTO_ADJ bridge: laggard catches up)
            if _SYM_FAMILY.get(symbol) == "CRYPTO_ADJ":
                try:
                    my_mv = float((day_moves or {}).get(symbol))
                except (TypeError, ValueError):
                    my_mv = None
                if my_mv is not None:
                    for peer in SUBFAMILIES["CRYPTO_ADJ"]:
                        if peer == symbol:
                            continue
                        try:
                            pmv = float((day_moves or {}).get(peer))
                        except (TypeError, ValueError):
                            continue
                        spread = pmv - my_mv
                        pkey = f"P:{peer}->{symbol}"
                        if abs(spread) < self.pair_spread_pct:
                            self._armed_at.pop(pkey, None)
                            continue
                        if pkey not in self._armed_at:
                            self._armed_at[pkey] = self._clock()
                        want = "long" if spread > 0 else "short"
                        if want != direction:
                            continue
                        scale = self._decay_scale(pkey)
                        if scale < self.trail_min_scale:
                            continue
                        edge = self._edge(peer, symbol)
                        score = self._weight(edge) * scale
                        if score > best_score:
                            best_score, best_trail = score, ("pairconv", peer,
                                                             round(spread, 2))
            if best_trail is None:
                return 1.0, None
            kind = best_trail[0]
            if kind == "leadlag":
                base = self.boost_max
            elif kind == "carry":
                base = self.carry_boost
            elif kind == "gapfade":
                ag = abs(float(best_trail[2]))
                scale = 0.4 if ag < 3.0 else (0.75 if ag < 7.0 else 1.0)
                base = self.gap_boost_max * scale
            else:                                   # pairconv
                base = self.pair_boost
            mult = 1.0 + min(base * best_score, self.boost_max)
            return mult, best_trail
        except Exception:
            return 1.0, None

    # ── pheromone writes ───────────────────────────────────────────────

    def reinforce(self, symbol: str, trail, won: bool) -> None:
        """Outcome feedback on the exact trail that boosted the entry."""
        try:
            kind, leader, _ = trail
            edge = self._edge(leader, symbol)
            w = self._weight(edge) * (self.win_mult if won else self.loss_mult)
            self.weights[edge] = min(self.w_max, max(self.w_min, w))
        except Exception:
            pass

    def evaporate(self) -> None:
        """Slow return to neutral — the colony forgets stale paths."""
        for e, w in list(self.weights.items()):
            nw = 1.0 + (w - 1.0) * self.evap
            if abs(nw - 1.0) < 1e-4:
                self.weights.pop(e, None)
            else:
                self.weights[e] = nw
        # housekeeping: drop trails dead beyond the decay floor (bounds the
        # map when a symbol leaves the universe)
        import math as _m
        horizon = self.trail_halflife_s * _m.log2(1.0 / self.trail_min_scale)
        now = self._clock()
        for k, t0 in list(self._armed_at.items()):
            try:
                if now - float(t0) > horizon:
                    self._armed_at.pop(k, None)
            except (TypeError, ValueError):
                self._armed_at.pop(k, None)

    # ── persistence (atomic mirror, one-bad-line) ──────────────────────

    def save(self, path: str) -> None:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"ts": self._clock(), "weights": self.weights,
                           "armed_at": self._armed_at}, fh)
            os.replace(tmp, path)
        except Exception:
            pass

    def load(self, path: str) -> None:
        try:
            with open(path) as fh:
                raw = json.load(fh)
            for e, w in (raw.get("weights") or {}).items():
                try:
                    wv = float(w)
                except (TypeError, ValueError):
                    continue          # one bad line kills one weight
                self.weights[str(e)] = min(self.w_max, max(self.w_min, wv))
            now = self._clock()
            for k, t0 in (raw.get("armed_at") or {}).items():
                try:
                    tv = float(t0)
                except (TypeError, ValueError):
                    continue          # one bad line kills one trail
                if tv <= now:         # future stamps are corrupt — drop
                    self._armed_at[str(k)] = tv
        except FileNotFoundError:
            pass
        except Exception:
            pass
