"""Loss-as-signal brain (Governor build order 2026-11-08).

Doctrine: the demo account is the probe — its realized losses are DATA.
A cohort (theme family x direction) whose shrunk expectancy is negative
over enough closes is toxic: the veto artifact tells every consuming
account to refuse new risk in that cohort. k=20 shrinkage toward the
no-edge prior (expectancy 0) — the same empirical-Bayes doctrine as
ARIA's skeptic base rate — so a handful of unlucky closes can never
veto, and a real bleed needs no narrator.

Cohort key is (family, side), not (asset, side): the rotation policy
already treats same-family assets as interchangeable representatives, and
family-level aggregation accrues n fast enough at $5 risk units for the
veto to ever bind.

Pure module: verdict math only. IO lives in the caller (registry scan,
artifact publish/consume).
"""

from __future__ import annotations

import time
from typing import Any

K_SHRINKAGE = 20.0          # prior weight, no-edge prior expectancy = $0
DEFAULT_MIN_N = 5           # closes before a veto may bind
DEFAULT_WINDOW_S = 7 * 86400.0
DEFAULT_MAX_AGE_S = 6 * 3600.0


def _f(x: Any) -> float | None:
    try:
        v = float(x)
        return v if v == v else None   # NaN guard
    except (TypeError, ValueError):
        return None


def parse_executed_at(raw: Any) -> float | None:
    """ISO-8601 venue timestamp → epoch seconds. Never raises."""
    if not isinstance(raw, str) or not raw:
        return None
    from datetime import datetime
    try:
        return datetime.fromisoformat(
            raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def close_pnl_from_trades(trades: list[dict], opened_ts: float) -> float | None:
    """Sum realizedPnl over the closing legs (type reduce/close) executed at
    or after the bracket opened. Returns None when no closing leg parses —
    caller falls back to the partials already booked by sync_bracket_size.
    """
    total = 0.0
    found = False
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        if str(t.get("type") or "") not in ("reduce", "close"):
            continue
        ts = parse_executed_at(t.get("executedAt"))
        if ts is not None and ts < opened_ts - 1.0:
            continue
        pnl = _f(t.get("realizedPnl"))
        if pnl is None:
            continue
        total += pnl
        found = True
    return total if found else None


class CohortStats:
    """Rolling (family, side) expectancy from closed brackets."""

    def __init__(self, k: float = K_SHRINKAGE, min_n: int = DEFAULT_MIN_N,
                 window_s: float = DEFAULT_WINDOW_S):
        self.k = float(k)
        self.min_n = int(min_n)
        self.window_s = float(window_s)

    def build(self, closed_brackets: list[dict],
              now: float | None = None) -> dict[str, dict[str, Any]]:
        """closed_brackets: registry rows carrying family/side/
        realized_pnl_final/closed_ts. Only windowed rows with a parseable
        final PnL count."""
        now = time.time() if now is None else now
        out: dict[str, dict[str, Any]] = {}
        for b in closed_brackets or []:
            pnl = _f(b.get("realized_pnl_final"))
            ts = _f(b.get("closed_ts"))
            fam, side = b.get("family"), b.get("side")
            if pnl is None or ts is None or not fam or not side:
                continue
            if now - ts > self.window_s:
                continue
            key = f"{fam}|{side}"
            c = out.setdefault(key, {"n": 0, "wins": 0, "pnl": 0.0,
                                     "last_close_ts": 0.0})
            c["n"] += 1
            c["pnl"] += pnl
            if pnl > 0:
                c["wins"] += 1
            c["last_close_ts"] = max(c["last_close_ts"], ts)
        for c in out.values():
            n = c["n"]
            c["win_rate"] = c["wins"] / n
            c["expectancy"] = c["pnl"] / n
            c["shrunk_expectancy"] = c["pnl"] / (n + self.k)  # prior mean 0
            c["veto"] = n >= self.min_n and c["shrunk_expectancy"] < 0.0
        return out


def veto_for(stats: dict[str, dict[str, Any]], family: str,
             side: str) -> dict[str, Any] | None:
    """The cohort row when it vetoes, else None."""
    row = (stats or {}).get(f"{family}|{side}")
    if row and row.get("veto"):
        return row
    return None


def artifact_fresh(artifact: dict | None,
                   max_age_s: float = DEFAULT_MAX_AGE_S,
                   now: float | None = None) -> bool:
    """Fail-OPEN on stale/missing: a dead probe must never freeze the
    fleet — the veto lifts when the publisher stops refreshing."""
    if not isinstance(artifact, dict):
        return False
    ts = _f(artifact.get("ts"))
    if ts is None:
        return False
    now = time.time() if now is None else now
    return (now - ts) <= max_age_s
