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

    GROSS: venue realizedPnl excludes fees/funding (C4 audit 2026-11-17 —
    ~$2.37/trade invisible). Net variant below.
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


_FEE_FIELDS = ("fee", "fees", "commission", "feeUsd", "fee_usd")
_FUNDING_FIELDS = ("funding", "fundingFee", "funding_fee", "fundingUsd")


def _sum_field(t: dict, names: tuple[str, ...]) -> tuple[float, bool]:
    """First parseable value among candidate field names; (value, found)."""
    for n in names:
        if n in t:
            v = _f(t.get(n))
            if v is not None:
                return v, True
    return 0.0, False


def close_pnl_net_from_trades(
    trades: list[dict], opened_ts: float
) -> tuple[float | None, bool]:
    """C4 net-PnL stamp: gross realizedPnl on reduce/close legs MINUS fees
    on ALL legs of this bracket's life (opening legs pay taker too) MINUS
    funding rows (type funding/fundingFee when present).

    Returns (net, fees_seen). fees_seen=False → the venue row carried no
    fee/funding fields at all, net == gross; the caller notes this in the
    close event so the audit trail knows the stamp is gross.
    """
    total = 0.0
    found = False
    fees_seen = False
    for t in trades or []:
        if not isinstance(t, dict):
            continue
        ts = parse_executed_at(t.get("executedAt"))
        if ts is not None and ts < opened_ts - 1.0:
            continue
        ttype = str(t.get("type") or "")
        fee, f_found = _sum_field(t, _FEE_FIELDS)
        funding, fu_found = _sum_field(t, _FUNDING_FIELDS)
        fees_seen = fees_seen or f_found or fu_found
        if ttype in ("reduce", "close"):
            pnl = _f(t.get("realizedPnl"))
            if pnl is None:
                continue
            total += pnl
            found = True
        # Fees/funding are costs on every leg of the position's life.
        total -= fee + funding
    if not found:
        return None, fees_seen
    return total, fees_seen


def stamp_close_outcome(
    bracket: dict,
    *,
    family: str | None = None,
    realized_pnl_final: float | None = None,
    closed_ts: float | None = None,
    coherence_at_entry: Any = None,
    restamp_window_s: float | None = None,
    realized_pnl_source: str | None = None,
    account_id: str | None = None,
    now: float | None = None,
) -> bool:
    """Mutate a close row so CohortStats.build can ingest it.

    Live wound (2026-11-08): reconcile could set realized_pnl_final while
    legacy/adopted brackets still carried family=None → artifact {stats:{}}
    forever. Caller supplies family via family_of(asset) so this module
    stays free of the rotation map. risk_usd is place-time and left as-is.

    Venue-lag re-stamp (C4, 2026-11-17): when restamp_window_s is given,
    a row whose closed_ts is within the window ACCEPTS a fresher
    realized_pnl_final (the first venue read can race trailing fills).
    Without the window the legacy first-write-wins holds bit-for-bit.
    Every stamp records realized_pnl_source ("trades_net", "trades_gross",
    "booked_fallback") so consumers can grade the stamp's provenance.

    account_id (C10): stamped when missing so the cohort builder can
    partition evidence bases per account.

    Returns True when any load-bearing outcome field was written.
    """
    if not isinstance(bracket, dict):
        return False
    changed = False
    if family and not bracket.get("family"):
        bracket["family"] = family
        changed = True
    if account_id and not bracket.get("account_id"):
        bracket["account_id"] = account_id
        changed = True
    if closed_ts is not None and bracket.get("closed_ts") is None:
        bracket["closed_ts"] = float(closed_ts)
        changed = True
    pnl_in = _f(realized_pnl_final)
    if pnl_in is not None:
        existing = bracket.get("realized_pnl_final")
        if existing is None:
            bracket["realized_pnl_final"] = pnl_in
            if realized_pnl_source:
                bracket["realized_pnl_source"] = realized_pnl_source
            changed = True
        elif restamp_window_s is not None:
            cts = _f(bracket.get("closed_ts"))
            ref = time.time() if now is None else now
            if cts is not None and (ref - cts) <= restamp_window_s:
                if _f(existing) != pnl_in:
                    bracket["realized_pnl_final"] = pnl_in
                    bracket["realized_pnl_restamped"] = True
                    if realized_pnl_source:
                        bracket["realized_pnl_source"] = realized_pnl_source
                    changed = True
    if bracket.get("realized_pnl_final") is None:
        booked = _f(bracket.get("realized_booked"))
        if booked is not None:
            bracket["realized_pnl_final"] = booked
            bracket.setdefault("realized_pnl_source", "booked_fallback")
            changed = True
    if (coherence_at_entry is not None
            and bracket.get("coherence_at_entry") is None):
        coh = _f(coherence_at_entry)
        if coh is not None:
            bracket["coherence_at_entry"] = coh
            changed = True
    return changed


class CohortStats:
    """Rolling (family, side) expectancy from closed brackets."""

    def __init__(self, k: float = K_SHRINKAGE, min_n: int = DEFAULT_MIN_N,
                 window_s: float = DEFAULT_WINDOW_S):
        self.k = float(k)
        self.min_n = int(min_n)
        self.window_s = float(window_s)

    def build(self, closed_brackets: list[dict],
              now: float | None = None,
              grain: str = "family",
              account_id: str | None = None,
              epoch_ts: float = 0.0) -> dict[str, dict[str, Any]]:
        """closed_brackets: registry rows carrying family/side/
        realized_pnl_final/closed_ts. Only windowed rows with a parseable
        final PnL count.

        grain="asset" (2026-11-17 Build 5): cohort key is (asset, side) so
        one toxic name vetoes itself, not its whole family; rows missing
        asset are skipped at that grain. Default "family" = legacy
        bit-for-bit.

        account_id (C10): when set, only rows stamped with the same
        account_id count — one account = one evidence base (the demo's
        11-16 account switch mixed two accounts' closes into one veto).
        Rows missing account_id are EXCLUDED under an active filter (a
        row we cannot attribute must not vote in this account's artifact).
        None = legacy unpartitioned bit-for-bit.

        epoch_ts (C10 warm-start): rows closed BEFORE the epoch never
        count — the Governor declares a regime boundary (account switch,
        doctrine flip) and the artifact rebases without deleting history.
        0.0 = legacy bit-for-bit.

        R-form rows (C12): when a row carries a parseable risk_usd > 0 the
        cohort also accumulates pnl_r (realized_pnl_final / risk_usd) so
        consumers can gate on shrunk_expectancy_r — size-invariant across
        knobs. $-fields unchanged (legacy consumers bit-for-bit)."""
        asset_grain = grain == "asset"
        now = time.time() if now is None else now
        out: dict[str, dict[str, Any]] = {}
        for b in closed_brackets or []:
            if account_id is not None:
                if b.get("account_id") != account_id:
                    continue
            pnl = _f(b.get("realized_pnl_final"))
            ts = _f(b.get("closed_ts"))
            fam, side = b.get("family"), b.get("side")
            if pnl is None or ts is None or not fam or not side:
                continue
            if ts < epoch_ts:
                continue
            if now - ts > self.window_s:
                continue
            if asset_grain:
                asset = b.get("asset")
                if not asset:
                    continue
                key = f"{asset}|{side}"
            else:
                key = f"{fam}|{side}"
            c = out.setdefault(key, {"n": 0, "wins": 0, "pnl": 0.0,
                                     "last_close_ts": 0.0,
                                     "n_r": 0, "pnl_r": 0.0,
                                     "risk_sum": 0.0})
            c["n"] += 1
            c["pnl"] += pnl
            if pnl > 0:
                c["wins"] += 1
            c["last_close_ts"] = max(c["last_close_ts"], ts)
            risk = _f(b.get("risk_usd"))
            if risk is not None and risk > 0:
                c["n_r"] += 1
                c["pnl_r"] += pnl / risk
                c["risk_sum"] += risk
        for c in out.values():
            n = c["n"]
            c["win_rate"] = c["wins"] / n
            c["expectancy"] = c["pnl"] / n
            c["shrunk_expectancy"] = c["pnl"] / (n + self.k)  # prior mean 0
            c["veto"] = n >= self.min_n and c["shrunk_expectancy"] < 0.0
            if c["n_r"] > 0:
                c["expectancy_r"] = c["pnl_r"] / c["n_r"]
                c["shrunk_expectancy_r"] = c["pnl_r"] / (c["n_r"] + self.k)
                c["mean_risk"] = c["risk_sum"] / c["n_r"]
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
                   now: float | None = None,
                   evidence_max_age_s: float | None = None) -> bool:
    """Fail-OPEN on stale/missing: a dead probe must never freeze the
    fleet — the veto lifts when the publisher stops refreshing.

    evidence_max_age_s (audit item 2026-11-17): when set, the artifact's
    EVIDENCE must also be fresh — the max last_close_ts across stats rows
    within that age. The legacy check gates the publisher heartbeat only,
    which can stay warm while the evidence underneath rots. None = legacy
    bit-for-bit."""
    if not isinstance(artifact, dict):
        return False
    ts = _f(artifact.get("ts"))
    if ts is None:
        return False
    now = time.time() if now is None else now
    if (now - ts) > max_age_s:
        return False
    if evidence_max_age_s is None:
        return True
    stats = artifact.get("stats") or {}
    latest = 0.0
    for row in stats.values():
        if isinstance(row, dict):
            lc = _f(row.get("last_close_ts"))
            if lc is not None:
                latest = max(latest, lc)
    if latest <= 0.0:
        return False
    return (now - latest) <= evidence_max_age_s
