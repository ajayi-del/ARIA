"""Whale mirror — fresh-flow detection from watched-address snapshot diffs.

Deploy 5 (2026-08-29), operator directive: LIVE from day one — SIZE is the
differentiator, and the loop closes both ways:
  entry side  — fresh whale agreement boosts size on already-gated
                candidates (×1.25 single direct / ×1.5 consensus ≥2), and
                fires the small 50x probe class on consensus (bounded
                margin, native stop, 15-min time-stop).
  exit side   — a DIRECT-leg whale closing the side we hold ends the
                informed thesis we mirrored (O'Hara PIN: holding after
                the informed exit is adverse selection) → greedy partial
                harvest while green (Freeman-Shor: bank, keep the runner).
Zero I/O — snapshots and price moves are injected. Doctrines:

  Hasbrouck: only FRESH changes carry information — an aged bag is noise.
             Every leg here is a DIFF between consecutive snapshots; a
             static position, however large, emits nothing. Momentum
             ignition decays in minutes → the probe time-stops at 15.
  Grinold-Kahn: breadth compounds — ≥2 INDEPENDENT whales agreeing inside
             a time window is a different signal class than one whale.
             Both are emitted (n_whales on the record) so the shadow
             review can measure them separately (Aronson: the data argues).
  Thorp/Vince: leverage is not risk — risk is size × stop distance. The
             probe's 50x on a fixed $15 margin risks ≈ margin × leverage ×
             stop_pct ≈ $5.40, bounded a priori, never equity-derived.

Legs:
  SoDEX  — direct signed-size position diffs. Direction certain.
  Aster  — campaign leaderboard per-symbol (pnl, volume) deltas crossed
           with the price move over the same window: sign(Δpnl) agreeing
           with sign(Δprice) → net long; disagreeing → net short. This is
           an INFERENCE (quality tier "inferred") — churn (volume up,
           |Δpnl| below the noise floor) abstains.
"""
import math
import time

# Flow kinds
OPENED = "opened"
ADDED = "added"
TRIMMED = "trimmed"
CLOSED = "closed"
FLIPPED = "flipped"


def _sign(x: float) -> int:
    return 1 if x > 0 else (-1 if x < 0 else 0)


class WhaleMirror:
    """Snapshot-diff classifier + consensus tracker.

    Injected:
      price_change_pct(symbol, window_s) -> float | None  — percent move
          over the trailing window (main wires candle buffers). None =
          unknown → Aster direction inference abstains.
      min_pnl_delta_usd, min_price_move_pct, consensus_window_s — knobs
          (config.whale_*).
      time_fn — injectable clock (department template).
    """

    def __init__(self, price_change_pct, min_pnl_delta_usd: float = 50.0,
                 min_price_move_pct: float = 0.05,
                 consensus_window_s: int = 1800, time_fn=time.time):
        self._px = price_change_pct
        self._min_dpnl = float(min_pnl_delta_usd)
        self._min_dpx = float(min_price_move_pct)
        self._window = int(consensus_window_s)
        self._time = time_fn
        self._prev_sodex: dict = {}   # address -> {symbol: signed_size}
        self._prev_aster: dict = {}   # (address, symbol) -> {pnl, volume}
        self._flows: list = []        # recent flow events for consensus

    # ── SoDEX leg: direct position diffs ─────────────────────────────────

    def diff_sodex(self, address: str, positions: dict) -> list:
        """positions: {symbol: signed_size} (negative = short). Returns a list
        of flow events {venue, address, symbol, direction, kind, size,
        prev_size, ts}. Emits only on CHANGE — aged bags are silent."""
        now = self._time()
        prev = self._prev_sodex.get(address, {})
        out = []
        for sym in set(prev) | set(positions):
            cur = float(positions.get(sym, 0.0) or 0.0)
            old = float(prev.get(sym, 0.0) or 0.0)
            if cur == old:
                continue
            kind, direction = None, None
            if old == 0.0 and cur != 0.0:
                kind, direction = OPENED, ("long" if cur > 0 else "short")
            elif cur == 0.0:
                kind, direction = CLOSED, ("long" if old > 0 else "short")
            elif _sign(cur) != _sign(old):
                kind, direction = FLIPPED, ("long" if cur > 0 else "short")
            elif abs(cur) > abs(old):
                kind, direction = ADDED, ("long" if cur > 0 else "short")
            else:
                kind, direction = TRIMMED, ("long" if cur > 0 else "short")
            ev = {"venue": "sodex", "address": address, "symbol": sym,
                  "direction": direction, "kind": kind,
                  "size": abs(cur), "prev_size": abs(old),
                  "quality": "direct", "ts": now}
            out.append(ev)
            self._record_flow(ev)
        self._prev_sodex[address] = {s: float(v) for s, v in positions.items()
                                     if float(v or 0.0) != 0.0}
        return out

    # ── Aster leg: leaderboard (pnl, volume) deltas × price move ─────────

    def diff_aster_rank(self, address: str, symbol: str,
                        pnl: float | None, volume: float | None,
                        poll_window_s: float = 300.0) -> dict | None:
        """One flow event on CHANGE, else None. Direction inferred from
        sign(Δpnl) vs sign(Δprice over the poll window). First sighting
        establishes the baseline without emitting (aged-bag filter — the
        existing period pnl predates our watch)."""
        now = self._time()
        key = (address, symbol)
        prev = self._prev_aster.get(key)
        cur = {"pnl": float(pnl or 0.0), "volume": float(volume or 0.0)}
        self._prev_aster[key] = cur
        if prev is None:
            return None                       # baseline, not a flow
        d_pnl = cur["pnl"] - prev["pnl"]
        d_vol = cur["volume"] - prev["volume"]
        if abs(d_pnl) < self._min_dpnl:
            return None                       # churn / market-making — abstain
        d_px = self._px(symbol, poll_window_s)
        if d_px is None or abs(d_px) < self._min_dpx:
            return None                       # direction unidentifiable
        direction = "long" if _sign(d_pnl) == _sign(d_px) else "short"
        kind = ADDED if d_vol > 0 else CLOSED
        ev = {"venue": "aster", "address": address, "symbol": symbol,
              "direction": direction, "kind": kind,
              "size": abs(d_pnl), "prev_size": 0.0,
              "quality": "inferred", "ts": now}
        self._record_flow(ev)
        return ev

    # ── Consensus (Grinold-Kahn breadth) ─────────────────────────────────

    def _record_flow(self, ev: dict) -> None:
        self._flows.append(ev)
        cutoff = self._time() - 2 * self._window
        self._flows = [f for f in self._flows if f["ts"] >= cutoff]

    def consensus(self, symbol: str, direction: str) -> dict:
        """Distinct addresses with an OPENING-class flow (opened/added/
        flipped) on (symbol, direction) inside the consensus window."""
        cutoff = self._time() - self._window
        addrs = {f["address"] for f in self._flows
                 if f["symbol"] == symbol and f["direction"] == direction
                 and f["kind"] in (OPENED, ADDED, FLIPPED)
                 and f["ts"] >= cutoff}
        freshest = max((f["ts"] for f in self._flows
                        if f["symbol"] == symbol
                        and f["direction"] == direction
                        and f["address"] in addrs), default=None)
        return {"symbol": symbol, "direction": direction,
                "n_whales": len(addrs), "addresses": sorted(addrs),
                "freshness_s": (self._time() - freshest)
                if freshest is not None else None}

    def has_direct_flow(self, symbol: str, direction: str) -> bool:
        """A fresh opening-class flow from the DIRECT leg (SoDEX position
        diffs — direction certain) inside the consensus window. The live
        size boost requires this for single-whale agreement; inferred-only
        flows need breadth ≥2."""
        cutoff = self._time() - self._window
        return any(f["symbol"] == symbol and f["direction"] == direction
                   and f["quality"] == "direct"
                   and f["kind"] in (OPENED, ADDED, FLIPPED)
                   and f["ts"] >= cutoff for f in self._flows)

    def candidates(self, flows: list) -> list:
        """Flow events → mirror candidates with consensus attached. Every
        opening-class flow yields a candidate (n_whales≥1); the shadow
        review slices accuracy by n_whales."""
        out = []
        for ev in flows:
            if ev["kind"] not in (OPENED, ADDED, FLIPPED):
                continue
            cons = self.consensus(ev["symbol"], ev["direction"])
            out.append({**ev, "n_whales": cons["n_whales"],
                        "freshness_s": cons["freshness_s"]})
        return out

    # ── Exit side: direct-leg reversal (O'Hara PIN) ─────────────────────

    def reversal_flows(self, symbol: str, held_direction: str) -> list:
        """DIRECT-leg flows inside the window where a watched whale exited
        the side we currently hold. FLIPPED carries the NEW side, so a
        reversal of a held LONG = CLOSED-long OR FLIPPED-short (mirror for
        shorts). Trims and inferred-leg moves abstain — a trim is profit-
        taking, not thesis exit; an inferred reversal is noise."""
        closing_side = held_direction          # CLOSED-long closes our long
        flipped_side = "short" if held_direction == "long" else "long"
        cutoff = self._time() - self._window
        return [f for f in self._flows
                if f["symbol"] == symbol and f["quality"] == "direct"
                and f["ts"] >= cutoff
                and ((f["kind"] == CLOSED and f["direction"] == closing_side)
                     or (f["kind"] == FLIPPED and f["direction"] == flipped_side))]

    # ── Probe bracket (Thorp/Vince: risk = notional × stop, not leverage) ──

    @staticmethod
    def whale_probe_bracket(mark: float, side: str, margin_usd: float,
                            leverage: float, stop_pct: float,
                            tp1_pct: float, tp2_pct: float,
                            step: float, min_qty: float) -> dict | None:
        """Pure sizing for the consensus probe. qty = margin×leverage/mark
        floored to the spec step; None when below min_qty or $1 notional.
        stop/tp1/tp2 are price levels mirrored by side. Worst case is
        bounded a priori: notional × stop_pct (+ fees), never equity-
        derived."""
        if mark <= 0 or margin_usd <= 0 or leverage <= 0 or step <= 0:
            return None
        qty = math.floor((margin_usd * leverage / mark) / step) * step
        if qty < min_qty or qty * mark < 1.0:
            return None
        if side == "long":
            stop = mark * (1 - stop_pct / 100.0)
            tp1 = mark * (1 + tp1_pct / 100.0)
            tp2 = mark * (1 + tp2_pct / 100.0)
        else:
            stop = mark * (1 + stop_pct / 100.0)
            tp1 = mark * (1 - tp1_pct / 100.0)
            tp2 = mark * (1 - tp2_pct / 100.0)
        return {"qty": qty, "stop": stop, "tp1": tp1, "tp2": tp2,
                "notional": qty * mark,
                "risk_usd": qty * mark * stop_pct / 100.0}
