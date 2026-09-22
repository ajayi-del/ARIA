"""BTC-beta estimator — the low-beta survival plane (2026-09-22, Governor directive).

Observation: low-BTC-beta alts (XMR 0.45, SEI 0.52, ENA 0.60) survive
correlated BTC ticks and hold to full moves; high-beta majors (ETH 0.95,
SOL 0.92) get stopped in minutes. This module measures that beta; the
shadow sizing layer (someone else's task) consumes it.

Doctrine:
  Sharpe (1964) — beta = cov(r_i, r_m) / var(r_m). OLS slope of the symbol's
    returns on BTC's. No intercept handling needed at hourly frequency.
  Aronson — winsorize the inputs: one liquidation wick (a 20-sigma tick)
    must not dominate a 7-day estimate. Clip both series at +/-3 sigma of
    the BTC series (BTC sigma is the market's own vol ruler).
  ARIA skeptic doctrine — empirical-Bayes shrinkage toward the market prior
    (beta = 1.0) with k=20, same constant as the Skeptic base rate: small-n
    symbols read near-market, large-n symbols earn their own estimate.
  Fail-open neutral: missing data NEVER blocks (beta_gate_ok abstains None)
  and NEVER distorts (size_multiplier returns 1.0 on None).

Zero I/O (department template): no network, no files, no logging. All data
injected; all knobs constructor-injected with defaults. Clock injected for
telemetry timestamps.
"""

from collections import deque
from typing import Callable, Optional, Sequence

__all__ = [
    "estimate_beta",
    "RollingBeta",
    "classify",
    "size_multiplier",
    "beta_gate_ok",
]

# ---------------------------------------------------------------------------
# Classification thresholds (injectable at call sites)
# ---------------------------------------------------------------------------
_LOW_MAX = 0.65
_HIGH_MIN = 0.80


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs)


def _median(xs: Sequence[float]) -> float:
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


# A flat tape is degenerate well above float-noise; 1e-12 std is a 1e-10%
# hourly move — no beta information at any realistic scale.
_MIN_STD = 1e-12


def _robust_std(btc: Sequence[float]) -> float:
    """MAD-based sigma of the BTC series.

    A plain std is contaminated by the very wick we are winsorizing against
    (a 20-sigma wick inflates sigma ~2.7x on a 60-sample window, loosening
    its own clip). MAD is immune to a handful of outliers; scaled by 1.4826
    it equals the std on Gaussian data. Falls back to plain std when the
    median absolute deviation is zero (e.g. a mostly-flat tape).
    """
    med = _median(btc)
    mad = _median([abs(b - med) for b in btc])
    if mad > 0.0:
        return 1.4826 * mad
    mu = _mean(btc)
    return (sum((b - mu) ** 2 for b in btc) / len(btc)) ** 0.5


def _winsorize(
    values: Sequence[float], center: float, half_width: float
) -> list:
    lo, hi = center - half_width, center + half_width
    return [min(max(v, lo), hi) for v in values]


def estimate_beta(
    symbol_returns: Sequence[float],
    btc_returns: Sequence[float],
    min_samples: int = 20,
    winsor_sigma: float = 3.0,
) -> Optional[float]:
    """OLS slope of symbol returns on BTC returns (cov/var).

    Both series are winsorized at +/-winsor_sigma * (robust sigma of the BTC
    series) around the BTC mean, so a single liquidation wick cannot dominate
    — and cannot inflate its own clip bound. Returns None when paired samples
    < min_samples or BTC variance is degenerate (a flat BTC tape carries no
    beta information).
    """
    n = min(len(symbol_returns), len(btc_returns))
    if n < min_samples:
        return None
    sym = [float(v) for v in symbol_returns[-n:]]
    btc = [float(v) for v in btc_returns[-n:]]

    std_b = _robust_std(btc)
    if std_b <= _MIN_STD:
        return None

    if winsor_sigma > 0.0:
        mu_b_raw = _mean(btc)
        hw = winsor_sigma * std_b
        btc = _winsorize(btc, mu_b_raw, hw)
        sym = _winsorize(sym, mu_b_raw, hw)

    mu_b = _mean(btc)
    mu_s = _mean(sym)
    var_b = sum((b - mu_b) ** 2 for b in btc)
    if var_b <= 0.0:
        return None
    cov = sum((s - mu_s) * (b - mu_b) for s, b in zip(sym, btc))
    return cov / var_b


def classify(
    beta: Optional[float],
    low_max: float = _LOW_MAX,
    high_min: float = _HIGH_MIN,
) -> str:
    """Beta bucket: 'low' / 'mid' / 'high'; 'unknown' on None (abstain)."""
    if beta is None:
        return "unknown"
    if beta < low_max:
        return "low"
    if beta > high_min:
        return "high"
    return "mid"


def size_multiplier(
    beta: Optional[float],
    pivot: float = 0.70,
    slope: float = 2.0,
    cap: float = 1.5,
) -> float:
    """Governor's formula: 1 + max(0, (pivot - beta) * slope), clamped [1.0, cap].

    None beta -> 1.0 (fail-open neutral: missing data never changes size).
    """
    if beta is None:
        return 1.0
    mult = 1.0 + max(0.0, (pivot - beta) * slope)
    return min(max(mult, 1.0), cap)


def beta_gate_ok(beta: Optional[float], max_beta: float = 0.70) -> Optional[bool]:
    """Entry gate: True/False when beta is known; None = abstain.

    Missing data must never block a trade — None abstains rather than False.
    """
    if beta is None:
        return None
    return beta <= max_beta


class RollingBeta:
    """Bounded rolling beta per symbol over paired (symbol, BTC) returns.

    Zero I/O: the caller appends paired 1h returns; the brain estimates.
    Default window 168 samples = 7d of 1h bars. Shrinkage toward the market
    prior 1.0 with pseudo-count k (empirical-Bayes, same doctrine as the
    Skeptic base rate): shrunk = (n*raw + k*1.0) / (n+k).
    """

    def __init__(
        self,
        window: int = 168,
        min_samples: int = 20,
        shrink_k: float = 20.0,
        prior: float = 1.0,
        winsor_sigma: float = 3.0,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if window < 2:
            raise ValueError("window must be >= 2")
        self.window = int(window)
        self.min_samples = int(min_samples)
        self.shrink_k = float(shrink_k)
        self.prior = float(prior)
        self.winsor_sigma = float(winsor_sigma)
        self._clock = clock if clock is not None else (lambda: 0.0)
        # symbol -> deque of (symbol_ret, btc_ret, ts)
        self._pairs: dict = {}

    def update(
        self, symbol: str, symbol_ret: float, btc_ret: float, ts: float
    ) -> None:
        """Append one paired 1h return observation. Non-finite inputs ignored."""
        try:
            s = float(symbol_ret)
            b = float(btc_ret)
            t = float(ts)
        except (TypeError, ValueError):
            return
        if s != s or b != b:  # NaN guard
            return
        buf = self._pairs.get(symbol)
        if buf is None:
            buf = deque(maxlen=self.window)
            self._pairs[symbol] = buf
        buf.append((s, b, t))

    def _raw(self, symbol: str) -> tuple:
        """(raw_beta, n) — raw None when unidentifiable."""
        buf = self._pairs.get(symbol)
        n = len(buf) if buf is not None else 0
        if buf is None or n < self.min_samples:
            return None, n
        sym = [p[0] for p in buf]
        btc = [p[1] for p in buf]
        raw = estimate_beta(
            sym, btc,
            min_samples=self.min_samples,
            winsor_sigma=self.winsor_sigma,
        )
        return raw, n

    def beta(self, symbol: str) -> Optional[float]:
        """Shrunk beta, or None when the raw estimate is unidentifiable."""
        raw, n = self._raw(symbol)
        if raw is None:
            return None
        k = self.shrink_k
        return (n * raw + k * self.prior) / (n + k)

    def snapshot(self, symbol: str) -> dict:
        """Telemetry row: raw / shrunk / n / window / clock read."""
        raw, n = self._raw(symbol)
        shrunk = None
        if raw is not None:
            k = self.shrink_k
            shrunk = (n * raw + k * self.prior) / (n + k)
        return {
            "symbol": symbol,
            "raw": raw,
            "shrunk": shrunk,
            "n": n,
            "window": self.window,
            "min_samples": self.min_samples,
            "shrink_k": self.shrink_k,
            "prior": self.prior,
            "class": classify(shrunk),
            "ts": self._clock(),
        }
