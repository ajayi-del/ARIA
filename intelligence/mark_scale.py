"""Mark-scale split sentinel — venue-data-defect quarantine (Workstream B).

The defect: SoDEX's WS markPrice channel served SPCX at the pre-rebase scale
(769.35) while klines/book/fills served ~140 — a PERSISTENT 5.48x split. A
tick-jump quarantine can never arm (the mark never jumps; it is just wrong),
and the entry-vs-mark guard only protects CLOSE triggers on positions whose
entry sits at the right scale — entries priced FROM the bad mark see no
split at all, so the campaign heartbeat and cascade paths kept manufacturing
positions whose stops, TPs, treasury, conviction review, and trailing were
silently disarmed, and whose closes journaled phantom PnL (+$792/+$799
against an untouched balance, 2026-08-22).

Doctrine: a persistent mark/kline scale split is a venue-data defect →
quarantine the SYMBOL — entries blocked on every path, mark-derived
accounting suppressed — until the channels agree again. The 1m kline close
is the independent reference: a perp mark and its own kline close cannot
legitimately diverge beyond the band for three consecutive observations.
Missing/stale/zero inputs fail OPEN (no observation, state unchanged) —
off-hours equities and warmup must never quarantine.
"""

LOW_RATIO = 0.70    # symmetric ~30% guard band
HIGH_RATIO = 1.43   # ≈ 1 / 0.70
PERSIST_N = 3       # consecutive split observations to arm (3 × 30s = 90s)
HEAL_N = 3          # consecutive in-band observations to heal


class MarkScaleSentinel:
    """Zero-I/O brain: consumes (mark, kline_close) observations and owns the
    armed/healed state machine. The executor loop feeds it; param_store
    carries the verdict to consumers (TTL'd, so it survives restarts)."""

    def __init__(self, low: float = LOW_RATIO, high: float = HIGH_RATIO,
                 persist_n: int = PERSIST_N, heal_n: int = HEAL_N) -> None:
        self.low = float(low)
        self.high = float(high)
        self.persist_n = int(persist_n)
        self.heal_n = int(heal_n)
        self._split_n: dict = {}
        self._heal_n: dict = {}
        self._armed: set = set()

    def observe(self, sym: str, mark, kline_close):
        """One observation → (quarantined, transition, ratio).

        transition ∈ {"armed", "healed", None} — the caller logs and
        writes/clears the param_store key on transitions (plus periodic TTL
        refreshes while armed). Invalid inputs are no-ops (fail-open)."""
        try:
            m = float(mark or 0.0)
            k = float(kline_close or 0.0)
        except (TypeError, ValueError):
            return sym in self._armed, None, 0.0
        if m <= 0.0 or k <= 0.0:
            return sym in self._armed, None, 0.0
        ratio = m / k
        if ratio < self.low or ratio > self.high:
            self._split_n[sym] = self._split_n.get(sym, 0) + 1
            self._heal_n[sym] = 0
            if sym not in self._armed and self._split_n[sym] >= self.persist_n:
                self._armed.add(sym)
                return True, "armed", ratio
            return sym in self._armed, None, ratio
        self._split_n[sym] = 0
        if sym in self._armed:
            self._heal_n[sym] = self._heal_n.get(sym, 0) + 1
            if self._heal_n[sym] >= self.heal_n:
                self._armed.discard(sym)
                self._heal_n.pop(sym, None)
                return False, "healed", ratio
        else:
            self._heal_n[sym] = 0
        return sym in self._armed, None, ratio

    def quarantined(self, sym: str) -> bool:
        return sym in self._armed
