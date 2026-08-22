"""Venue price discovery — Hasbrouck (1995) information share.

For the same asset quoted on two venues (e.g. SoDEX mark vs Aster mark), the
information share answers: which venue's innovations move the COMMON efficient
price? Estimated via a VAR on the aligned log-price changes, inverted to its
vector moving-average form; the long-run multiplier vector (shared under
rank-1 cointegration) distributes the innovation variance across venues.
Cholesky under both orderings gives Hasbrouck's upper/lower bounds; the
midpoint is the working estimate.

Polymathic use in ARIA: if Aster's share on a dual-listed major is small
(Aster FOLLOWS), entries on Aster-routed alts should anchor to the fast
leading feed with the local book as confirmation — the cascade design already
assumes this; the share number makes it measured instead of assumed.
Gonzalo-Granger (1995) permanent-transitory shares are the cross-check
(deferred — same VAR machinery, different normalization).

Pure module: callers inject aligned, equal-length price arrays (same sampling
cadence, same clock).
"""
from __future__ import annotations

from typing import Optional, Sequence

import numpy as np


def hasbrouck_information_share(px_a: Sequence[float],
                                px_b: Sequence[float],
                                lags: int = 5) -> Optional[dict]:
    """Return {"is_a_mid", "is_a_lo", "is_a_hi", "n"} or None on thin/bad data.

    is_a_* = venue A's share of price discovery (0..1); venue B's = 1 − that.
    """
    try:
        a = np.asarray(px_a, dtype=float)
        b = np.asarray(px_b, dtype=float)
        if len(a) != len(b) or len(a) < lags * 12 + 2:
            return None
        if (a <= 0).any() or (b <= 0).any():
            return None
        da = np.diff(np.log(a))
        db = np.diff(np.log(b))
        n = len(da)
        T = n - lags
        if T < lags * 10:
            return None
        Y = np.column_stack([da[lags:], db[lags:]])
        cols = [np.ones(T)]
        for L in range(1, lags + 1):
            cols.append(da[lags - L:n - L])
            cols.append(db[lags - L:n - L])
        X = np.column_stack(cols)
        B, *_ = np.linalg.lstsq(X, Y, rcond=None)
        resid = Y - X @ B
        dof = max(T - X.shape[1], 1)
        Sigma = resid.T @ resid / dof
        A_sum = np.zeros((2, 2))
        for L in range(lags):
            A_sum += B[1 + 2 * L:3 + 2 * L].T
        Psi = np.linalg.inv(np.eye(2) - A_sum)
        psi = Psi.mean(axis=0)                    # common-trend multipliers
        denom = float(psi @ Sigma @ psi)
        if denom <= 0:
            return None

        def _is_first(S: np.ndarray) -> Optional[float]:
            try:
                F = np.linalg.cholesky(S)
            except np.linalg.LinAlgError:
                return None
            v = psi @ F
            return float(v[0] ** 2) / float(psi @ S @ psi)

        hi = _is_first(Sigma)                              # A ordered first
        swap = Sigma[[1, 0]][:, [1, 0]]
        hi_b = _is_first(swap)                             # B ordered first
        if hi is None or hi_b is None:
            return None
        lo = 1.0 - hi_b
        lo = min(max(lo, 0.0), 1.0)
        hi = min(max(hi, 0.0), 1.0)
        if hi < lo:
            lo, hi = hi, lo
        return {"is_a_mid": 0.5 * (lo + hi), "is_a_lo": lo,
                "is_a_hi": hi, "n": int(T)}
    except (np.linalg.LinAlgError, ValueError, TypeError, FloatingPointError):
        return None
