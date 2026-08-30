"""Tide-Aligned Consensus (TAC) — whale breadth × ETF tide sizing ladder.

Deploy 2026-08-30 (spec audit amendments baked in):
  - The ladder is BOUNDED PLACEHOLDER sizing: 1.00 / 1.05 / 1.15 / 1.25.
    Confidence is NOT profitability — EV = p·avgWin − (1−p)·avgLoss is
    learned from the shadow journal (gate "tide_consensus") before any
    rung may move. The ladder is a hypothesis under test, not a claim.
  - Breadth is EFFECTIVE breadth (whale_evidence.effective_breadth:
    40% leviathan cap + venue-cluster sqrt(n) deflation), never wallet
    count — correlated whales are one risk factor.
  - The ETF tide is an OBSERVED state (daily-lagged institutional flow),
    not proof of accumulation. It may AMPLIFY strong consensus (1.15→1.25)
    or ABSTAIN the boost when opposed (×1.00 — not a veto; the tide veto
    lives on the entry paths). It never creates a boost alone.
  - Shadow-scored from birth: every verdict carries a SignalEvidence
    packet; the calibrated predictive layer (E[R | event, context])
    accrues in the shadow journal, not here.

Zero-I/O brain: flows and tide state are injected; clock injected.
"""
import time

from intelligence.whale_evidence import SignalEvidence, effective_breadth

# Bounded placeholder ladder (audit amendment #2). TAC_ENABLED=false at the
# splice reproduces the legacy ×1.25/×1.5 whale_mirror ladder bit-for-bit.
LADDER = {"none": 1.00, "weak": 1.05, "strong": 1.15, "tide_strong": 1.25}

STRONG_FLOOR = 2.0   # effective breadth (independent-bet equivalents)


def tac_rung(eff_breadth: float, tide_state: str,
             strong_floor: float = STRONG_FLOOR) -> tuple:
    """(rung_name, multiplier). Opposed tide abstains (×1.00) BEFORE the
    breadth rungs — never amplify into the institutional tide. Tide alone
    (breadth < 1) earns nothing."""
    if tide_state == "opposed":
        return "abstain_opposed_tide", LADDER["none"]
    if eff_breadth >= strong_floor:
        if tide_state == "aligned":
            return "strong_tide_aligned", LADDER["tide_strong"]
        return "strong", LADDER["strong"]
    if eff_breadth >= 1.0:
        return "weak", LADDER["weak"]
    return "none", LADDER["none"]


class TideConsensus:
    """One verdict per sizing-chain call. Never raises."""

    def __init__(self, strong_floor: float = STRONG_FLOOR,
                 leviathan_cap_frac: float = 0.40, time_fn=time.time):
        self._floor = float(strong_floor)
        self._cap = float(leviathan_cap_frac)
        self._time = time_fn

    def verdict(self, symbol: str, direction: str, flows: list,
                tide_state: str = "neutral",
                freshness_s: float | None = None) -> tuple:
        """flows: opening-class whale flows on (symbol, direction) inside
        the consensus window (WhaleMirror.consensus_flows). tide_state ∈
        {aligned, opposed, neutral} from the SoSoValue feed (stale feed
        passes neutral). Returns (multiplier, SignalEvidence)."""
        try:
            flows = flows or []
            eb = effective_breadth(flows, self._cap)
            rung, mult = tac_rung(eb, tide_state or "neutral", self._floor)
            ev = SignalEvidence(
                event_type="tide_consensus", symbol=symbol,
                direction=direction, confidence=None,  # learned — shadow
                freshness_s=freshness_s, effective_breadth=round(eb, 3),
                sample_size=0,
                features={"rung": rung, "tide_state": tide_state,
                          "n_flows": len(flows),
                          "direct_flows": sum(1 for f in flows
                                              if isinstance(f, dict)
                                              and f.get("quality") == "direct")})
            return mult, ev
        except Exception:
            return 1.0, None
