"""Cluster-loser cut brain (prop harness only) — SHADOW by default.

Detects open legs that are simultaneously underwater on the *same*
directional β (e.g. BTC short + xyz:XYZ100 short) so management can
tighten / harvest / reduce the *losers* — never fear-scratch winners.

Why this exists
---------------
``crypto_comove`` is an *entry* gate (refuse opposite-signed crypto adds)
and is sleeve-local + crypto-only (``is_crypto_asset`` / tradfi exempt).
``flip_predicate`` journals ``crypto_comove_would_flip`` when the *crypto*
dominant book is underwater — still entry-plane, still no stocks.

Live specimen (Vultr 2026-11-16): turbo BTC short + FT crypto BTC short
+ FT stocks XYZ100 short all red same side. Zero ``cluster_loser`` /
``adverse_comove`` journal stamps — the cross-sleeve adverse co-move was
*missed* because stocks and crypto do not share a management plane.

Doctrine (conquest-safe)
------------------------
- Cluster = ≥ min_n open legs with aligned side AND adverse PnL/R within
  a TTL underwater window, optionally spanning macro-β families
  (majors ↔ index) so BTC ↔ XYZ100 compares.
- Cut policy (shadow actions only while live=False):
    * losers → would_tighten / would_harvest / would_reduce
    * winners (uPnL>0 or r_moved>0) → never targeted
- Cross-sleeve via kingdom-view / peer legs: compare marks & uPnL only —
  never merge DD boxes or capital across sleeves.
- Kill: PROP_CLUSTER_LOSER_CUT=0 (default) → brain cold, bit-for-bit.
  PROP_CLUSTER_LOSER_CUT=1 enables shadow journals.
  PROP_CLUSTER_LOSER_CUT_LIVE=1 reserved — still refuses actuation in
  this module (caller must not place until a separate LIVE PR).

MUST-NOT: Kant / Nietzsche / Chancellor / tiers 1–6.
"""

from __future__ import annotations

from typing import Any

from ..families import family_of

CLUSTER_LOSER_DETECTED = "cluster_loser_detected"
CLUSTER_LOSER_WOULD_CUT = "cluster_loser_would_cut"

# Macro β plane: crypto majors/large_alt ride risk-on with equity index
# (XYZ100 / SP500). Dayo's BTC ↔ XYZ100 specimen lives here.
MACRO_BETA_FAMILIES = frozenset({"majors", "index", "large_alt"})

DEFAULT_MIN_N = 2
DEFAULT_MIN_ADVERSE_USD = 0.05
DEFAULT_UNDERWATER_S = 300.0
DEFAULT_JOURNAL_COOLDOWN_S = 60.0

_BUY = frozenset({"buy", "long"})
_SELL = frozenset({"sell", "short"})


def _side_sign(side: str | None) -> int:
    s = (side or "").lower()
    if s in _BUY:
        return 1
    if s in _SELL:
        return -1
    return 0


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:  # NaN
        return None
    return v


def _leg_upnl(leg: dict) -> float | None:
    for key in ("last_upnl", "unrealized_pnl", "unrealizedPnl"):
        v = _f(leg.get(key))
        if v is not None:
            return v
    return None


def _leg_r_moved(leg: dict) -> float | None:
    return _f(leg.get("r_moved"))


def is_adverse(leg: dict, *, min_adverse_usd: float = DEFAULT_MIN_ADVERSE_USD
               ) -> bool:
    """True when the open leg is underwater on $ or R plane.

    Prefer last_upnl (Propr-authoritative). Fall back to r_moved < 0 for
    kingdom-view peer snapshots that only publish R telemetry.
    """
    upnl = _leg_upnl(leg)
    if upnl is not None:
        try:
            floor = float(min_adverse_usd or 0.0)
        except (TypeError, ValueError):
            floor = DEFAULT_MIN_ADVERSE_USD
        return upnl < -abs(floor)
    r = _leg_r_moved(leg)
    if r is None:
        return False
    return r < 0.0


def is_winner(leg: dict) -> bool:
    """Green legs are never cut targets (conquest law)."""
    upnl = _leg_upnl(leg)
    if upnl is not None and upnl > 0:
        return True
    r = _leg_r_moved(leg)
    return r is not None and r > 0.0


def macro_beta_key(asset: str | None, family: str | None = None) -> str | None:
    """Return cluster key for macro-β co-move, or None if outside the plane."""
    fam = (family or family_of(asset or "")).strip()
    if fam in MACRO_BETA_FAMILIES:
        return "macro_beta"
    return None


def same_direction_cluster_key(leg: dict, *, use_macro_beta: bool = True
                               ) -> str | None:
    """Bucket key: direction × (macro_beta | family).

    Same-side BTC short + XYZ100 short → ``sell|macro_beta``.
    Same-side HYPE short alone stays ``sell|exchange`` (needs a peer).
    """
    sign = _side_sign(leg.get("side"))
    if sign == 0:
        return None
    direction = "buy" if sign > 0 else "sell"
    fam = (leg.get("family") or family_of(leg.get("asset") or "")).strip()
    if use_macro_beta:
        mk = macro_beta_key(leg.get("asset"), fam)
        if mk:
            return f"{direction}|{mk}"
    return f"{direction}|{fam or 'solo'}"


def _underwater_age_s(leg: dict, now: float) -> float | None:
    since = _f(leg.get("upnl_negative_since"))
    if since is None or since <= 0:
        # Conservative: opened_ts as lower bound only when already adverse.
        since = _f(leg.get("opened_ts"))
    if since is None or since <= 0:
        return None
    return max(0.0, now - since)


def proposed_action(leg: dict) -> str:
    """Shadow action for one loser — tighten/harvest/reduce, never flatten-all.

    Priority:
      - BE already armed → would_tighten (pull residual stop further /
        refuse reloads — conquest floor, not birth wipe)
      - Some risk already released → would_harvest (scale loser residual)
      - Else → would_reduce (trim size / risk on the loser only)
    """
    if bool(leg.get("be_armed")):
        return "would_tighten"
    released = _f(leg.get("risk_released")) or 0.0
    if released > 0:
        return "would_harvest"
    return "would_reduce"


def normalize_leg(raw: dict, *, army_id: str = "", sleeve: str = ""
                  ) -> dict | None:
    """Accept registry brackets OR kingdom-view evidence legs."""
    if not isinstance(raw, dict):
        return None
    asset = raw.get("asset")
    side = raw.get("side")
    if not asset or _side_sign(side) == 0:
        return None
    status = (raw.get("status") or "open").lower()
    if status not in ("open", ""):
        return None
    fam = raw.get("family") or family_of(str(asset))
    return {
        "asset": asset,
        "side": side,
        "family": fam,
        "status": "open",
        "army_id": army_id or raw.get("army_id") or raw.get("account_id") or "",
        "sleeve": sleeve or raw.get("sleeve") or "",
        "semantic_id": raw.get("semantic_id"),
        "risk_usd": _f(raw.get("risk_usd")),
        "last_upnl": _leg_upnl(raw),
        "r_moved": _leg_r_moved(raw),
        "be_armed": bool(raw.get("be_armed")),
        "risk_released": _f(raw.get("risk_released")),
        "opened_ts": _f(raw.get("opened_ts")),
        "upnl_negative_since": _f(raw.get("upnl_negative_since")),
        "mfe_peak_r": _f(raw.get("mfe_peak_r")),
    }


def collect_legs(
        local_brackets: list | None,
        peer_legs: list | None = None,
        *,
        own_account_id: str = "",
        own_sleeve: str = "",
) -> list[dict]:
    """Merge local open brackets + peer/kingdom evidence legs (dedup asset|side|army)."""
    out: list[dict] = []
    seen: set[tuple] = set()
    for raw in local_brackets or []:
        leg = normalize_leg(raw, army_id=own_account_id, sleeve=own_sleeve)
        if leg is None:
            continue
        key = (leg["army_id"], leg["asset"], leg["side"])
        if key in seen:
            continue
        seen.add(key)
        out.append(leg)
    for raw in peer_legs or []:
        if not isinstance(raw, dict):
            continue
        aid = str(raw.get("army_id") or raw.get("account_id") or "")
        # Skip own army duplicates already taken from richer registry.
        if own_account_id and aid and aid == own_account_id:
            continue
        leg = normalize_leg(raw, army_id=aid, sleeve=str(raw.get("sleeve") or ""))
        if leg is None:
            continue
        key = (leg["army_id"], leg["asset"], leg["side"])
        if key in seen:
            continue
        seen.add(key)
        out.append(leg)
    return out


def cluster_loser_verdict(
        enabled: bool,
        legs: list | None,
        *,
        min_n: int = DEFAULT_MIN_N,
        min_adverse_usd: float = DEFAULT_MIN_ADVERSE_USD,
        underwater_s: float = DEFAULT_UNDERWATER_S,
        use_macro_beta: bool = True,
        live: bool = False,
        now: float | None = None,
) -> dict[str, Any] | None:
    """Pure shadow/live-reserved verdict. Never mutates legs.

    Returns None when kill-switch off / no cluster. Else detail dict with
    ``event``, ``cluster_key``, ``members`` (losers only), ``actions``,
    ``n_winners_spared``, ``shadow`` / ``live``.

    ``live=True`` still only *labels* the payload — this module never
    returns an actuation order. Callers MUST NOT place until a LIVE PR.
    """
    if not enabled:
        return None
    import time as _time
    ts = float(now if now is not None else _time.time())
    try:
        need = max(2, int(min_n or DEFAULT_MIN_N))
    except (TypeError, ValueError):
        need = DEFAULT_MIN_N
    try:
        need_s = float(underwater_s or 0.0)
    except (TypeError, ValueError):
        need_s = DEFAULT_UNDERWATER_S

    buckets: dict[str, list[dict]] = {}
    winners_spared = 0
    for raw in legs or []:
        leg = raw if "family" in raw and "army_id" in raw else normalize_leg(raw)
        if leg is None:
            continue
        if is_winner(leg):
            winners_spared += 1
            continue
        if not is_adverse(leg, min_adverse_usd=min_adverse_usd):
            continue
        age = _underwater_age_s(leg, ts)
        # Missing age → treat as just-adverse (age 0); still counts for
        # detection but TTL gate requires age>=need_s when stamp exists
        # OR opened long enough. Fail-open into cluster if age unknown
        # and need_s<=0; else require opened_ts age.
        if need_s > 0:
            if age is None:
                continue
            if age < need_s:
                continue
        key = same_direction_cluster_key(leg, use_macro_beta=use_macro_beta)
        if key is None:
            continue
        buckets.setdefault(key, []).append(leg)

    # Pick the largest adverse cluster (stable: first key on tie).
    best_key = None
    best: list[dict] = []
    for key, members in sorted(buckets.items()):
        if len(members) > len(best):
            best_key, best = key, members
    if best_key is None or len(best) < need:
        return None

    actions = []
    for leg in best:
        actions.append({
            "asset": leg["asset"],
            "side": leg["side"],
            "army_id": leg.get("army_id") or "",
            "sleeve": leg.get("sleeve") or "",
            "semantic_id": leg.get("semantic_id"),
            "last_upnl": leg.get("last_upnl"),
            "r_moved": leg.get("r_moved"),
            "action": proposed_action(leg),
        })
    sleeves = sorted({a["sleeve"] for a in actions if a.get("sleeve")})
    armies = sorted({a["army_id"] for a in actions if a.get("army_id")})
    return {
        "event": CLUSTER_LOSER_WOULD_CUT,
        "cluster_key": best_key,
        "n_adverse": len(best),
        "n_winners_spared": winners_spared,
        "members": [
            {
                "asset": m["asset"], "side": m["side"],
                "army_id": m.get("army_id") or "",
                "sleeve": m.get("sleeve") or "",
                "last_upnl": m.get("last_upnl"),
                "r_moved": m.get("r_moved"),
                "be_armed": m.get("be_armed"),
                "semantic_id": m.get("semantic_id"),
            }
            for m in best
        ],
        "actions": actions,
        "cross_sleeve": len(armies) >= 2 or len(sleeves) >= 2,
        "n_armies": len(armies),
        "sleeves": sleeves,
        "shadow": not bool(live),
        "live": bool(live),
        # Actuation hard-block in this module — LIVE PR must flip this.
        "actuation": "forbidden",
        "min_n": need,
        "underwater_s": need_s,
        "use_macro_beta": bool(use_macro_beta),
    }


def should_journal(last_ts: float | None, now: float,
                   *, cooldown_s: float = DEFAULT_JOURNAL_COOLDOWN_S) -> bool:
    """Throttle shadow spam on the heartbeat cadence."""
    try:
        cool = float(cooldown_s or 0.0)
    except (TypeError, ValueError):
        cool = DEFAULT_JOURNAL_COOLDOWN_S
    if cool <= 0:
        return True
    if last_ts is None:
        return True
    try:
        prev = float(last_ts)
    except (TypeError, ValueError):
        return True
    return (now - prev) >= cool
