"""
ARIA Calibration Engine — computes optimal parameters from trade history.

Runs every 20 trades (triggered by main.py) and at midnight UTC.
Reads recent trade history from TradeDatabase.
Writes calibrated parameters to ParamStore (blended 30% toward target).
Saves full calibration output to logs/calibration.json.

What it learns:
  1. Stop multipliers — from P90 MAE distribution per asset
     Formula: (P90_mae_pct / assumed_atr_pct) × 1.25 safety buffer
  2. Coherence threshold — win rate per coherence band
     Threshold = highest band below 50% win rate (conservative — don't lower below proven floor)
  3. Session weights — profit factor per session

Minimum 10 trades required before any calibration runs.
Minimum 5 samples per dimension (per-asset or per-band) to update that dimension.
"""

import json
import time
import structlog
from collections import defaultdict
from pathlib import Path

log = structlog.get_logger(__name__)

CAL_PATH = Path("logs/calibration.json")
MIN_SAMPLE = 10         # global minimum
MIN_DIMENSION = 5       # per-asset / per-band minimum
BLEND_FACTOR = 0.30     # 30% toward calibrated target per update
BASE_ATR_PCT = 0.004    # 0.4% — assumed ATR for back-calculating stop mult from MAE


class CalibrationEngine:
    """
    ARIA's learning brain. Reads trade outcomes and asks:
    "What parameters would have produced better results?"

    Returns calibration dict and applies updates to ParamStore.
    """

    def __init__(self, trade_db) -> None:
        self.db = trade_db
        self._last_run_ts: float = 0.0
        self._last_result: dict = self._load_existing()

    def _load_existing(self) -> dict:
        if CAL_PATH.exists():
            try:
                return json.loads(CAL_PATH.read_text())
            except Exception:
                pass
        return {}

    def _save(self, result: dict) -> None:
        try:
            CAL_PATH.write_text(json.dumps(result, indent=2))
        except Exception as e:
            log.warning("calibration_save_error", error=str(e))

    def get_last_result(self) -> dict:
        return self._last_result.copy()

    # ── Public interface ──────────────────────────────────────────────────────

    def run(self) -> dict:
        """
        Full calibration pass over last 200 trades.
        Returns calibration dict. Does NOT write to ParamStore —
        caller (main.py _apply_calibration) handles the write.
        """
        trades = self.db.get_recent(200)

        if len(trades) < MIN_SAMPLE:
            log.info("calibration_skipped", trades=len(trades), min_required=MIN_SAMPLE)
            return self._last_result

        log.info("calibration_running", trade_count=len(trades))

        result: dict = {
            "calibrated_at": time.time(),
            "trade_count": len(trades),
            "stop_multipliers": self._calibrate_stops(trades),
            "coherence_thresholds": self._calibrate_coherence(trades),
            "session_weights": self._calibrate_sessions(trades),
        }

        self._save(result)
        self._last_result = result
        self._last_run_ts = time.time()

        log.info("calibration_complete",
                 stop_assets=len(result["stop_multipliers"]),
                 optimal_coherence=result["coherence_thresholds"].get("optimal_threshold"),
                 sessions=len(result["session_weights"]))

        return result

    # ── Private computations ──────────────────────────────────────────────────

    def _calibrate_stops(self, trades: list) -> dict:
        """
        Optimal stop multiplier per asset based on P90 MAE.

        P90 MAE / BASE_ATR_PCT × 1.25 = multiplier that covers 90% of adverse moves.
        Example: P90 MAE = 0.80%, ATR = 0.40% → mult = (0.80/0.40) × 1.25 = 2.5

        Interpretation: 90% of trades survive without touching this stop.
        The 10% that hit it would have been stopped regardless — stop was correct.
        """
        by_asset: dict = defaultdict(list)
        for t in trades:
            mae = t.get("mae_pct", 0.0)
            sym = t.get("symbol", "")
            if mae > 0 and sym:
                by_asset[sym].append(mae)

        result = {}
        for sym, maes in by_asset.items():
            if len(maes) < MIN_DIMENSION:
                continue
            maes_sorted = sorted(maes)
            p90 = maes_sorted[int(len(maes_sorted) * 0.90)]
            optimal = round(max(1.0, min(4.0, (p90 / 100) / BASE_ATR_PCT * 1.25)), 2)
            result[sym] = optimal
            log.info("stop_calibrated",
                     symbol=sym, sample=len(maes),
                     p90_mae_pct=round(p90, 3), optimal_mult=optimal)
        return result

    def _calibrate_coherence(self, trades: list) -> dict:
        """
        Win rate per coherence band.

        Conservative approach: optimal threshold = highest band with < 50% win rate.
        This raises the bar only when evidence of poor performance at that level exists.
        Never pushes threshold below 1.5 (hard safety floor in ParamStore).
        """
        bands: dict = {
            "2.0-2.5": [], "2.5-3.0": [], "3.0-3.5": [], "3.5-4.0": [], "4.0+": []
        }
        for t in trades:
            coh = t.get("coherence_score", 0.0)
            win = t.get("win", False)
            if   coh < 2.5: bands["2.0-2.5"].append(win)
            elif coh < 3.0: bands["2.5-3.0"].append(win)
            elif coh < 3.5: bands["3.0-3.5"].append(win)
            elif coh < 4.0: bands["3.5-4.0"].append(win)
            else:            bands["4.0+"].append(win)

        result: dict = {}
        optimal = 2.0
        for band, outcomes in bands.items():
            if len(outcomes) < MIN_DIMENSION:
                continue
            wr = sum(outcomes) / len(outcomes)
            result[band] = {"win_rate": round(wr, 3), "sample": len(outcomes)}
            # Walk the optimal threshold up past bands that lose money
            band_lo = float(band.split("-")[0].replace("+", ""))
            if wr < 0.50 and band_lo >= optimal:
                optimal = band_lo + 0.5  # skip past this losing band

        optimal = min(optimal, 4.0)  # hard cap
        result["optimal_threshold"] = round(optimal, 2)
        log.info("coherence_calibrated",
                 bands_with_data=len([b for b in result if b != "optimal_threshold"]),
                 optimal_threshold=optimal)
        return result

    def _calibrate_sessions(self, trades: list) -> dict:
        """
        Profit factor per session → recommended multiplier.

        PF ≥ 1.5 → 1.20×   (high-quality session, size up)
        PF ≥ 1.0 → 1.00×   (break-even or better, no change)
        PF ≥ 0.7 → 0.80×   (losing but not catastrophic, size down)
        PF < 0.7 → 0.60×   (consistently losing, significant reduction)
        """
        by_session: dict = defaultdict(list)
        for t in trades:
            sess = t.get("session_name", "unknown")
            pnl = t.get("net_pnl", 0.0)
            by_session[sess].append(pnl)

        result = {}
        for sess, pnls in by_session.items():
            if len(pnls) < MIN_DIMENSION:
                continue
            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p <= 0]
            pf = sum(wins) / abs(sum(losses)) if losses and sum(losses) != 0 else 999.0
            wr = len(wins) / len(pnls)
            if   pf >= 1.5: mult = 1.20
            elif pf >= 1.0: mult = 1.00
            elif pf >= 0.7: mult = 0.80
            else:           mult = 0.60
            result[sess] = {
                "profit_factor": round(pf, 3),
                "win_rate": round(wr, 3),
                "recommended_mult": mult,
                "sample": len(pnls),
            }
            log.info("session_calibrated",
                     session=sess, pf=round(pf, 3), mult=mult, sample=len(pnls))
        return result
