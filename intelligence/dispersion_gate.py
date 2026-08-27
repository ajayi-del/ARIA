"""
intelligence/dispersion_gate.py — Dispersion-Gated Asset Selection
ARIA Execution Alpha Patch — Component 4 (v2)

Uses RegimeState.dispersion (cross-sectional std of momentum scores, already computed
by the regime classifier) to gate which assets are tradeable:

  Low  (<0.015): assets correlated → alts have no independent edge, only BTC/ETH
  Mid  (0.015–0.040): normal market → all assets tradeable
  High (>0.040): strong divergence → only leading sector + large caps

Thresholds calibrated to observed 48-minute crypto momentum dispersion:
  Typical cross-asset std in crypto = 0.01–0.05+
  Old 0.002/0.004 bands were inside bid-ask noise → 90%+ of market conditions
  registered as "high dispersion", blocking alts unless in leading sector.

Self-move exemption (2026-08-27) — the low-disp doctrine "correlated ⇒ no
independent alt edge" is false for a symbol with its OWN catalyst (2026-08-27:
LIT +12.08%, SOL +12.84%, FARTCOIN +10.78%, BONK +10.19%, PENGU +10.06% all
blocked at dispersion ≤0.01497). A flat %-moved threshold is vol-blind — 8%
is 1σ noise in BONK but 2.5σ in SOL — so the exemption is three vol-aware
legs, first hit wins:

  fast  — Raschke/Connors expansion trigger: |1h return| ≥ 2×σ_1h with
          volume ratio ≥ 2 (detect at +1.5–3%, not after the move traveled)
  z     — Clenow/Carver vol-standardized day move: |day move| ≥ 1.75× the
          elapsed-fraction daily σ (Brownian time scaling, √t)
  rank  — Murphy relative strength: |day move| in the top decile of the
          crypto complex right now (regime-adaptive)

Steenbarger participation veto: z and rank legs require volume ratio ≥ 1.5 —
a mover on dying volume is a fade candidate, not a sponsored rotation
(fast leg implies it via its own ≥2.0). All evidence None = legacy
bit-for-bit. The gate is zero-I/O: call sites gather and pass the evidence.
"""
from __future__ import annotations

import math

import structlog

log = structlog.get_logger(__name__)

LOW_DISP  = 0.015   # was 0.002 — 1.5% cross-sectional std
HIGH_DISP = 0.040   # was 0.004 — 4.0% cross-sectional std

# Self-move exemption doctrine constants (2026-08-27).
FAST_SIGMA_MIN = 2.0            # |ret_1h| / σ_1h for the Raschke leg
FAST_VOLRATIO_MIN = 2.0         # expansion must be volume-sponsored
SIGMA_1H_FLOOR_PCT = 0.10       # denominator floor — dead books can't z-explode
MOVE_Z_MIN = 1.75               # Clenow/Carver vol-standardized day-move z
SIGMA_DAILY_FLOOR_PCT = 0.5     # daily-σ denominator floor
MIN_DAY_ELAPSED_S = 3600.0      # z leg needs ≥1h of elapsed window
RANK_PCTILE_MIN = 0.90          # Murphy top-decile backstop
PARTICIPATION_VOLRATIO_MIN = 1.5  # Steenbarger sponsorship floor (z/rank legs)

_LARGE_CAP = frozenset({"BTC-USD", "ETH-USD"})
# Non-crypto assets trade on their own fundamentals / macro drivers;
# crypto-alt correlation gating does not apply to them.
_ALWAYS_TRADE_CATEGORIES = frozenset({
    "equity", "equity_index",
    "commodity", "commodity_energy", "commodity_precious", "commodity_industrial",
})


def self_move_leg(
    day_move_pct: float | None,
    day_elapsed_s: float,
    daily_sigma_pct: float | None,
    ret_1h_pct: float | None,
    sigma_1h_pct: float | None,
    vol_ratio: float | None,
    move_rank_pctile: float | None,
) -> str:
    """Which exemption leg fired, or "" — pure, all doctrine constants local."""
    # Raschke fast leg: expansion trigger with sponsorship built in.
    if ret_1h_pct is not None and sigma_1h_pct is not None:
        z1h = abs(ret_1h_pct) / max(sigma_1h_pct, SIGMA_1H_FLOOR_PCT)
        if (z1h >= FAST_SIGMA_MIN
                and vol_ratio is not None and vol_ratio >= FAST_VOLRATIO_MIN):
            return f"fast_z{z1h:.2f}"
    # Steenbarger veto binds the slower legs.
    sponsored = vol_ratio is not None and vol_ratio >= PARTICIPATION_VOLRATIO_MIN
    # Clenow/Carver vol-z leg, Brownian elapsed-time scaling.
    if (sponsored and day_move_pct is not None
            and daily_sigma_pct is not None
            and day_elapsed_s >= MIN_DAY_ELAPSED_S):
        sig_el = (max(daily_sigma_pct, SIGMA_DAILY_FLOOR_PCT)
                  * math.sqrt(day_elapsed_s / 86400.0))
        z = abs(day_move_pct) / sig_el
        if z >= MOVE_Z_MIN:
            return f"z{z:.2f}"
    # Murphy rank leg: extreme vs the complex right now.
    if (sponsored and move_rank_pctile is not None
            and move_rank_pctile >= RANK_PCTILE_MIN):
        return f"rank{move_rank_pctile:.2f}"
    return ""


class DispersionGate:
    """Filter asset tradability based on current cross-sectional momentum dispersion."""

    def should_trade(
        self,
        symbol:         str,
        dispersion:     float,
        leading_sector: str = "",   # RegimeState.leading_category
        asset_category: str = "",   # ASSET_CONFIG[symbol]["category"]
        campaign_symbol: str = "",  # Campaign bypass — volume-generation mode
        # Self-move evidence bundle (2026-08-27); all None = bit-for-bit legacy.
        day_move_pct:      float | None = None,
        day_elapsed_s:     float = 0.0,
        daily_sigma_pct:   float | None = None,
        ret_1h_pct:        float | None = None,
        sigma_1h_pct:      float | None = None,
        vol_ratio:         float | None = None,
        move_rank_pctile:  float | None = None,
    ) -> tuple[bool, str]:
        """
        Returns (allowed: bool, reason: str).
        Always allows large caps and non-crypto assets.
        Filters crypto alts in low/high dispersion regimes.
        """
        is_large_cap = symbol in _LARGE_CAP
        is_always_trade = asset_category in _ALWAYS_TRADE_CATEGORIES

        # Campaign symbol bypass — tournament volume generation takes priority
        if campaign_symbol and symbol == campaign_symbol:
            return True, "campaign_symbol_bypass"

        # Non-crypto assets trade on macro/oracle fundamentals, not crypto correlation
        if is_always_trade:
            return True, "non_crypto_exempt"

        if dispersion < LOW_DISP:
            if not is_large_cap:
                leg = self_move_leg(
                    day_move_pct, day_elapsed_s, daily_sigma_pct,
                    ret_1h_pct, sigma_1h_pct, vol_ratio, move_rank_pctile)
                if leg:
                    return True, f"self_move_exempt_{leg}"
                return False, f"low_dispersion_{round(dispersion, 4)}_alts_no_edge"
            return True, "large_cap_always_ok"

        if dispersion > HIGH_DISP:
            if not is_large_cap and asset_category != leading_sector and leading_sector:
                return (
                    False,
                    f"high_dispersion_not_leader_{asset_category}_vs_{leading_sector}",
                )
            return True, "leader_or_large_cap"

        return True, "mid_dispersion_normal"
