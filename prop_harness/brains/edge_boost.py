"""Edge-boost brain — the profit mirror of loss_veto (2026-11-17).

The same artifact that vetoes toxic cohorts carries the positive side:
an (asset|family, side) cohort whose shrunk expectancy is positive over
enough closes has EARNED more size. k=20 shrinkage (loss_stats) keeps a
lucky handful from boosting; the multiplier is bounded [1.0, cap] and
the rho-box / headroom clamps downstream still bind — a boost can never
exceed headroom, it can only refuse to undersize a proven edge.

Fail-OPEN to 1.0 everywhere: missing artifact, thin cohort, bad row,
stale publish — any doubt means legacy sizing bit-for-bit.

Pure module: verdict math only. Artifact IO lives in the caller.
"""

from __future__ import annotations

from typing import Any

DEFAULT_MIN_N = 5
DEFAULT_MIN_EXPECTANCY = 0.5   # shrunk $/trade before any boost arms
DEFAULT_CAP = 1.5              # hard ceiling on the size multiplier
DEFAULT_MIN_R = 0.10           # shrunk R/trade floor for the R-based path
DEFAULT_FULL_N = 20            # n at which the n-ramp pays the full mult


def _f(x: Any) -> float | None:
    try:
        v = float(x)
        return v if v == v else None   # NaN guard
    except (TypeError, ValueError):
        return None


def boost_mult(row: dict | None, *, min_n: int = DEFAULT_MIN_N,
               min_expectancy: float = DEFAULT_MIN_EXPECTANCY,
               cap: float = DEFAULT_CAP) -> float:
    """Bounded size multiplier for a cohort row, 1.0 = no boost.

    Saturating ramp: full cap at shrunk_expectancy >= 2x min_expectancy.
    cap < 1.0 is clamped to 1.0 (a boost knob can never become a veto).
    """
    if not isinstance(row, dict):
        return 1.0
    try:
        n = int(row.get("n"))
    except (TypeError, ValueError):
        return 1.0
    if n < min_n:
        return 1.0
    se = _f(row.get("shrunk_expectancy"))
    if se is None or se < min_expectancy:
        return 1.0
    cap = max(1.0, float(cap))
    if min_expectancy <= 0:
        return 1.0
    frac = min(1.0, se / (2.0 * min_expectancy))
    return 1.0 + (cap - 1.0) * frac


def boost_mult_r(row: dict | None, *, min_n: int = DEFAULT_MIN_N,
                 min_r: float = DEFAULT_MIN_R,
                 cap: float = DEFAULT_CAP,
                 full_n: int = DEFAULT_FULL_N) -> float:
    """C12 R-based path: gate on shrunk_expectancy_r (size-invariant —
    a lucky streak of 5 at $5 risk can never hit the cap), then ramp the
    multiplier by evidence depth: full mult at n >= full_n, linear
    between min_n and full_n. Defaults reproduce the $-path shape when
    the caller passes matching knobs; the $ path above is untouched.
    """
    if not isinstance(row, dict):
        return 1.0
    try:
        n_r = int(row.get("n_r") or 0)
    except (TypeError, ValueError):
        return 1.0
    if n_r < min_n:
        return 1.0
    se_r = _f(row.get("shrunk_expectancy_r"))
    if se_r is None or se_r < min_r:
        return 1.0
    cap = max(1.0, float(cap))
    if min_r <= 0:
        return 1.0
    frac = min(1.0, se_r / (2.0 * min_r))
    full_n = max(int(full_n), int(min_n))
    if n_r < full_n:
        frac *= (n_r - min_n + 1) / (full_n - min_n + 1)
    return 1.0 + (cap - 1.0) * frac


def boost_for(stats: dict[str, dict[str, Any]], key: str, *,
              min_n: int = DEFAULT_MIN_N,
              min_expectancy: float = DEFAULT_MIN_EXPECTANCY,
              cap: float = DEFAULT_CAP) -> tuple[float, dict | None]:
    """(multiplier, cohort row) for stats[key]; (1.0, None) when unearned."""
    row = (stats or {}).get(key)
    mult = boost_mult(row, min_n=min_n, min_expectancy=min_expectancy,
                      cap=cap)
    if mult > 1.0:
        return mult, row
    return 1.0, None


def boost_for_r(stats: dict[str, dict[str, Any]], key: str, *,
                min_n: int = DEFAULT_MIN_N,
                min_r: float = DEFAULT_MIN_R,
                cap: float = DEFAULT_CAP,
                full_n: int = DEFAULT_FULL_N) -> tuple[float, dict | None]:
    """R-based twin of boost_for (C12)."""
    row = (stats or {}).get(key)
    mult = boost_mult_r(row, min_n=min_n, min_r=min_r, cap=cap,
                        full_n=full_n)
    if mult > 1.0:
        return mult, row
    return 1.0, None


def vetoed_recently(artifact: dict | None, key: str, *,
                    now: float, quarantine_s: float) -> bool:
    """C12 quarantine: a cohort the artifact VETOED within quarantine_s may
    not be boosted — the same evidence cannot punish and reward at once
    across a publisher/consumer lag. The publisher carries vetoed_ts (the
    LATEST ts the row was observed in veto, kept after the row leaves veto
    so the quarantine extends past the final veto observation) inside the
    artifact; missing map or stale entry fail OPEN (boost allowed) —
    quarantine is a belt, not a gate.
    """
    if not isinstance(artifact, dict) or not key or quarantine_s <= 0:
        return False
    vt = artifact.get("vetoed_ts")
    if not isinstance(vt, dict):
        return False
    ts = _f(vt.get(key))
    if ts is None:
        return False
    return (now - ts) <= quarantine_s


def key_for(artifact: dict | None, *, asset: str | None, side: str | None,
            family: str | None, asset_grain: bool) -> str | None:
    """Cohort key matching the artifact's own schema, else None (abstain).

    Mirrors _loss_veto's grain discipline: schema==2 artifacts are keyed
    (asset, side) and must ONLY be read at asset grain; legacy (family,
    side) artifacts ONLY at family grain. A mismatch abstains — a boost
    keyed on the wrong field is worse than no boost.
    """
    if not isinstance(artifact, dict) or not side:
        return None
    schema2 = artifact.get("schema") == 2
    if asset_grain:
        if not schema2 or not asset:
            return None
        return f"{asset}|{side}"
    if schema2 or not family:
        return None
    return f"{family}|{side}"
