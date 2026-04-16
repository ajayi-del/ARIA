"""
ARIA ML Classifier — cold path only.

Predicts P(win) for a trade candidate using a GradientBoostingClassifier
(sklearn) when available, or a simple win-rate fraction as fallback.

Public API
----------
TradeClassifier   — trains on journal data, predicts P(win)
ClassifierCache   — per-symbol P(win) cache
ml_size_multiplier — maps P(win) → position size multiplier
"""

from __future__ import annotations

import math
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional sklearn import — graceful fallback if not installed
# ---------------------------------------------------------------------------
try:
    from sklearn.ensemble import GradientBoostingClassifier as _GBC
    _SKLEARN_AVAILABLE = True
except ImportError:
    _GBC = None  # type: ignore[assignment,misc]
    _SKLEARN_AVAILABLE = False
    log.info("sklearn not available — TradeClassifier will use win-rate fallback")

# ---------------------------------------------------------------------------
# Personality → index mapping (order matches spec exactly)
# ---------------------------------------------------------------------------
_PERSONALITY_IDX: Dict[str, int] = {
    "SHIELD":    0,
    "AFTERMATH": 1,
    "APEX":      2,
    "COIL":      3,
    "FLOW":      4,
    "SCOUT":     5,
}

# cascade_phase → index
_CASCADE_PHASE_IDX: Dict[str, int] = {
    "idle":      0,
    "blocked":   1,
    "primed":    2,
    "momentum":  3,
    "aftermath": 4,
}

# regime → index
_REGIME_IDX: Dict[str, int] = {
    "confused":   0,
    "risk_on":    1,
    "risk_off":   2,
    "rotational": 3,
}

# funding_class → index
_FUNDING_CLASS_IDX: Dict[str, int] = {
    "neutral":         0,
    "positive":        1,
    "negative":        2,
    "extreme_positive": 3,
    "extreme_negative": 4,
}

_NUM_FEATURES = 20


def _safe_float(value: Any, default: float = 0.0, lo: float = -1e9, hi: float = 1e9) -> float:
    """Convert *value* to float, replacing None/NaN/Inf with *default*, then clamp."""
    try:
        v = float(value)
        if not math.isfinite(v):
            return default
        return max(lo, min(hi, v))
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, mapping: Dict[str, int], default: int = 0) -> float:
    """Look up *value* in *mapping*; if it is an enum-like, try .value and .name first."""
    # Accept raw string or enum-like objects
    key = None
    if isinstance(value, str):
        key = value.upper()
    elif hasattr(value, "value"):
        key = str(value.value).upper()
    elif hasattr(value, "name"):
        key = str(value.name).upper()
    if key is not None:
        # Try exact match, then strip any prefix (e.g. "Personality.FLOW" → "FLOW")
        if key in mapping:
            return float(mapping[key])
        bare = key.split(".")[-1]
        if bare in mapping:
            return float(mapping[bare])
    return float(default)


# ---------------------------------------------------------------------------
# TradeClassifier
# ---------------------------------------------------------------------------

class TradeClassifier:
    """
    Trains a GradientBoostingClassifier (or simple win-rate fallback) on
    closed journal entries and predicts P(win) for a given trade candidate.

    Parameters
    ----------
    min_samples : int
        Minimum number of closed journal entries required before training.
        ``train()`` returns False when fewer entries are available.
    """

    def __init__(self, min_samples: int = 50) -> None:
        self.min_samples = min_samples
        self._trained = False
        self._model: Optional[Any] = None          # sklearn model when available
        self._fallback_win_rate: float = 0.50      # simple fraction fallback

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def train(self, journal) -> bool:
        """
        Fit the classifier on closed trades from *journal*.

        Returns False if fewer than ``min_samples`` closed entries exist;
        True on success.
        """
        try:
            closed = self._closed_entries(journal)
        except Exception as exc:
            log.warning("TradeClassifier.train: could not read journal — %s", exc)
            return False

        if len(closed) < self.min_samples:
            log.debug(
                "TradeClassifier.train: only %d closed entries (need %d)",
                len(closed), self.min_samples,
            )
            return False

        labels = [1 if e.get("outcome") == "win" else 0 for e in closed]
        win_count = sum(labels)
        self._fallback_win_rate = win_count / len(labels)

        if _SKLEARN_AVAILABLE:
            features = [self._journal_entry_to_features(e) for e in closed]
            # Verify no NaN/Inf slipped through
            features = [
                [0.0 if not math.isfinite(f) else f for f in row]
                for row in features
            ]
            try:
                model = _GBC(
                    n_estimators=100,
                    max_depth=3,
                    learning_rate=0.1,
                    subsample=0.8,
                    random_state=42,
                )
                model.fit(features, labels)
                self._model = model
                log.info(
                    "TradeClassifier trained on %d samples (win-rate %.1f%%) — sklearn GBC",
                    len(closed), self._fallback_win_rate * 100,
                )
            except Exception as exc:
                log.warning("TradeClassifier sklearn fit failed — using fallback: %s", exc)
                self._model = None
        else:
            log.info(
                "TradeClassifier trained on %d samples (win-rate %.1f%%) — fallback",
                len(closed), self._fallback_win_rate * 100,
            )

        self._trained = True
        return True

    def predict(self, candidate, personality_state, context) -> float:
        """
        Return P(win) in [0.0, 1.0].

        Falls back to 0.50 if the classifier has not been trained yet.
        """
        if not self._trained:
            return 0.50

        features = self._candidate_to_features(candidate, personality_state, context)

        if _SKLEARN_AVAILABLE and self._model is not None:
            try:
                proba = self._model.predict_proba([features])[0]
                # proba[1] = P(win); classes are [0, 1]
                classes = list(self._model.classes_)
                win_idx = classes.index(1) if 1 in classes else 1
                p_win = float(proba[win_idx])
                return max(0.0, min(1.0, p_win))
            except Exception as exc:
                log.warning("TradeClassifier.predict sklearn error — using fallback: %s", exc)

        return max(0.0, min(1.0, self._fallback_win_rate))

    def _candidate_to_features(
        self,
        candidate,
        personality_state,
        context,
    ) -> List[float]:
        """
        Build the 20-feature vector for a live trade candidate.

        All values are clamped and NaN/Inf-safe.
        """
        # ── Candidate fields ──────────────────────────────────────────────
        coherence_score = _safe_float(
            getattr(candidate, "coherence_score", None), default=0.0, lo=0.0, hi=10.0
        )
        coherence_normalized = coherence_score / 10.0

        atr_ratio = _safe_float(
            getattr(candidate, "atr_ratio", None), default=1.0, lo=0.0, hi=3.0
        )
        rr_ratio = _safe_float(
            getattr(candidate, "rr_ratio", None), default=0.0, lo=0.0, hi=10.0
        )
        size_mult = _safe_float(
            getattr(candidate, "size_multiplier", None), default=1.0, lo=0.0, hi=2.0
        )

        # Microstructure fields (may not exist on all candidates)
        volume_surge = _safe_float(
            getattr(candidate, "volume_surge", None), default=1.0, lo=1.0, hi=5.0
        )
        imbalance_abs = abs(_safe_float(
            getattr(candidate, "imbalance", None), default=0.0, lo=-1.0, hi=1.0
        ))
        vpin = _safe_float(
            getattr(candidate, "vpin", None), default=0.0, lo=0.0, hi=1.0
        )
        funding_class_raw = getattr(candidate, "funding_class", "neutral")
        funding_class_idx = _safe_int(funding_class_raw, _FUNDING_CLASS_IDX, default=0)

        # ── Personality fields ────────────────────────────────────────────
        personality_name = getattr(personality_state, "name", None)
        personality_idx = _safe_int(personality_name, _PERSONALITY_IDX, default=5)  # default SCOUT
        personality_size_mult = _safe_float(
            getattr(personality_state, "size_mult", getattr(personality_state, "size_multiplier", None)),
            default=1.0, lo=0.0, hi=2.0,
        )

        # ── Context fields ────────────────────────────────────────────────
        cascade_phase_raw = getattr(context, "cascade_phase", "idle")
        cascade_phase_idx = float(
            _CASCADE_PHASE_IDX.get(str(cascade_phase_raw).lower(), 0)
        )

        regime_raw = getattr(context, "regime", "confused")
        regime_idx = float(
            _REGIME_IDX.get(str(regime_raw).lower(), 0)
        )

        rpc_health = _safe_float(
            getattr(context, "rpc_health_score", None), default=1.0, lo=0.0, hi=1.0
        )
        daily_pnl_pct = _safe_float(
            getattr(context, "daily_pnl_pct", None), default=0.0, lo=-0.2, hi=0.2
        )
        win_rate = _safe_float(
            getattr(context, "session_win_rate", None), default=0.5, lo=0.0, hi=1.0
        )
        basis_stress_count = _safe_float(
            getattr(context, "basis_stress_count", None), default=0.0, lo=0.0, hi=5.0
        )

        # ── Cyclical time encoding ────────────────────────────────────────
        now_utc = datetime.now(tz=timezone.utc)
        hour = now_utc.hour + now_utc.minute / 60.0  # fractional hour
        day_of_week = now_utc.weekday()              # Monday=0 … Sunday=6
        hour_sin = math.sin(2 * math.pi * hour / 24.0)
        hour_cos = math.cos(2 * math.pi * hour / 24.0)
        dow_sin  = math.sin(2 * math.pi * day_of_week / 7.0)
        dow_cos  = math.cos(2 * math.pi * day_of_week / 7.0)

        # ── Assemble exactly 20 features ─────────────────────────────────
        features: List[float] = [
            coherence_score,        #  1 — coherence_score (0-10)
            coherence_normalized,   #  2 — coherence_normalized (0-1)
            atr_ratio,              #  3 — atr_vs_baseline (0-3)
            rr_ratio,               #  4 — rr_ratio (0-10)
            size_mult,              #  5 — size_mult (0-2)
            personality_idx,        #  6 — personality_idx
            cascade_phase_idx,      #  7 — cascade_phase_idx
            regime_idx,             #  8 — regime_idx
            rpc_health,             #  9 — rpc_health (0-1)
            daily_pnl_pct,          # 10 — daily_pnl_pct (-0.2–0.2)
            win_rate,               # 11 — win_rate (0-1)
            hour_sin,               # 12 — hour_sin
            hour_cos,               # 13 — hour_cos
            dow_sin,                # 14 — day_of_week_sin
            dow_cos,                # 15 — day_of_week_cos
            funding_class_idx,      # 16 — funding_class_idx
            volume_surge,           # 17 — volume_surge (1-5)
            imbalance_abs,          # 18 — imbalance_abs (0-1)
            vpin,                   # 19 — vpin (0-1)
            basis_stress_count,     # 20 — basis_stress_count (0-5)
        ]

        assert len(features) == _NUM_FEATURES, (
            f"Feature vector length mismatch: {len(features)} != {_NUM_FEATURES}"
        )

        # Final NaN/Inf guard (should never trigger given _safe_float usage above)
        features = [0.0 if not math.isfinite(f) else f for f in features]
        return features

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _closed_entries(journal) -> List[dict]:
        """Return a list of closed (win/loss) entries from *journal*."""
        if hasattr(journal, "closed_trades"):
            return list(journal.closed_trades())
        if hasattr(journal, "entries"):
            entries = journal.entries
            if callable(entries):
                entries = entries()
            return [e for e in entries if e.get("outcome") in ("win", "loss")]
        if hasattr(journal, "__iter__"):
            return [e for e in journal if e.get("outcome") in ("win", "loss")]
        return []

    @staticmethod
    def _journal_entry_to_features(entry: dict) -> List[float]:
        """
        Build a feature vector from a historical journal entry dict.

        Fields that were not recorded in older entries are defaulted safely.
        """
        coherence_score = _safe_float(
            entry.get("coherence_score"), default=0.0, lo=0.0, hi=10.0
        )
        atr_ratio = _safe_float(entry.get("atr_ratio", 1.0), default=1.0, lo=0.0, hi=3.0)
        rr_ratio  = _safe_float(entry.get("rr_ratio", 0.0),  default=0.0, lo=0.0, hi=10.0)
        size_mult = _safe_float(entry.get("size_multiplier", 1.0), default=1.0, lo=0.0, hi=2.0)

        personality_raw = entry.get("personality", "SCOUT")
        personality_idx = _safe_int(personality_raw, _PERSONALITY_IDX, default=5)

        cascade_phase_raw = entry.get("cascade_phase", "idle")
        cascade_phase_idx = float(
            _CASCADE_PHASE_IDX.get(str(cascade_phase_raw).lower(), 0)
        )

        regime_raw = entry.get("regime", "confused")
        regime_idx = float(_REGIME_IDX.get(str(regime_raw).lower(), 0))

        rpc_health       = _safe_float(entry.get("rpc_health_score",  1.0), 1.0, 0.0, 1.0)
        daily_pnl_pct    = _safe_float(entry.get("daily_pnl_pct",     0.0), 0.0, -0.2, 0.2)
        win_rate         = _safe_float(entry.get("session_win_rate",  0.5), 0.5, 0.0, 1.0)
        funding_class_raw = entry.get("funding_class", "neutral")
        funding_class_idx = _safe_int(funding_class_raw, _FUNDING_CLASS_IDX, default=0)
        volume_surge     = _safe_float(entry.get("volume_surge",      1.0), 1.0, 1.0, 5.0)
        imbalance_abs    = abs(_safe_float(entry.get("imbalance",     0.0), 0.0, -1.0, 1.0))
        vpin             = _safe_float(entry.get("vpin",              0.0), 0.0, 0.0, 1.0)
        basis_stress     = _safe_float(entry.get("basis_stress_count", 0), 0.0, 0.0, 5.0)

        # Reconstruct time features from recorded timestamp when available
        ts_ms = entry.get("timestamp_ms")
        if ts_ms:
            try:
                dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
                hour = dt.hour + dt.minute / 60.0
                day_of_week = dt.weekday()
            except (OSError, OverflowError, ValueError):
                hour, day_of_week = 12.0, 0
        else:
            hour, day_of_week = 12.0, 0

        hour_sin = math.sin(2 * math.pi * hour / 24.0)
        hour_cos = math.cos(2 * math.pi * hour / 24.0)
        dow_sin  = math.sin(2 * math.pi * day_of_week / 7.0)
        dow_cos  = math.cos(2 * math.pi * day_of_week / 7.0)

        features = [
            coherence_score,
            coherence_score / 10.0,
            atr_ratio,
            rr_ratio,
            size_mult,
            personality_idx,
            cascade_phase_idx,
            regime_idx,
            rpc_health,
            daily_pnl_pct,
            win_rate,
            hour_sin,
            hour_cos,
            dow_sin,
            dow_cos,
            funding_class_idx,
            volume_surge,
            imbalance_abs,
            vpin,
            basis_stress,
        ]

        # Final guard
        features = [0.0 if not math.isfinite(f) else f for f in features]
        return features


# ---------------------------------------------------------------------------
# ClassifierCache
# ---------------------------------------------------------------------------

class ClassifierCache:
    """
    Per-symbol P(win) cache.

    ``get()`` returns 0.50 for any symbol not yet updated.
    """

    def __init__(self) -> None:
        self._cache: Dict[str, float] = {}

    def get(self, symbol: str) -> float:
        """Return cached P(win) for *symbol*, defaulting to 0.50."""
        return self._cache.get(symbol, 0.50)

    def update(self, symbol: str, prob: float) -> None:
        """Store P(win) for *symbol*."""
        self._cache[symbol] = max(0.0, min(1.0, prob))


# ---------------------------------------------------------------------------
# ml_size_multiplier
# ---------------------------------------------------------------------------

def ml_size_multiplier(p_win: float) -> float:
    """
    Map P(win) to a position size multiplier.

    p_win >= 0.55  → 1.00  (full size)
    p_win >= 0.50  → 0.75  (reduced size)
    p_win >= 0.45  → 0.50  (half size)
    p_win <  0.45  → 0.00  (skip trade)
    """
    if p_win >= 0.55:
        return 1.0
    if p_win >= 0.50:
        return 0.75
    if p_win >= 0.45:
        return 0.50
    return 0.0
