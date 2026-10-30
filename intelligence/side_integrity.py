"""Side-integrity brain (CEO P0 side-integrity schema, 2026-10-24).

Zero-I/O department module (docs/DEPARTMENT_TEMPLATE.md): pure verdict
functions consumed by ONE splice family in main.py. Kill switch
`side_integrity_guard_enabled` (read via getattr(config, ...) at each
splice site) — False = pre-module behavior bit-for-bit on every row.

ROOT CAUSE (CEO proof): SoDEX one-way-mode reconciliation matched
"same symbol + nonzero size" but NEVER same side. Opposite-side exchange
positions were adopted onto stale-side tracks and manufactured phantom
PnL (BTC +$2.87 / +$5.01 phantom wins on the wrong side; ETH 9,592
structural-rejection rows over two days).

BINDING INVARIANTS
  SI-1 signed truth: identity = (venue, symbol, generation, signed_qty).
       ONE parser normalizes explicit side fields; unsigned/directionless
       input is "unknown", NEVER long. Side is never inferred from an
       order side.
  SI-2 no management on mismatch: no size sync, grow, shrink, partial
       booking, TP inference, stop assignment, purge, or close accounting
       unless fresh exchange side == tracked side. Unknown = quarantine.
  SI-3 generation binding: async verifications recheck opened_at_ms +
       side + object identity before any mutation.
  SI-4 detach != close: confirmed foreign side flip removes tracking
       with ZERO accounting — no journal close/outcome/cooldowns/PnL.
  SI-5 fresh intent: re-adoption requires fresh side-matching ARIA
       intent; stale intent can never resurrect a detached side.
  SI-6 one structural verifier: all structural rejection strings route
       through verify_action(); error text alone never books a close.
  SI-7 accounting units: existing PnL formulas untouched.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

# ── Side vocabulary (SI-1) ──────────────────────────────────────────────────
SIDE_LONG = "long"
SIDE_SHORT = "short"
SIDE_UNKNOWN = "unknown"

# ── Row verdicts (SI-A) ─────────────────────────────────────────────────────
VERDICT_SAME_SIDE = "same_side"
VERDICT_SIDE_CONFLICT = "side_conflict"
VERDICT_SIDE_UNKNOWN = "side_unknown"
VERDICT_VENUE_AMBIGUOUS = "venue_ambiguous"

# ── Structural error classes (SI-6) ─────────────────────────────────────────
ERR_QTY_INVALID = "qty_invalid"
ERR_RO_WOULD_OPEN = "ro_would_open"
ERR_POSITION_NOT_FOUND = "position_not_found"
ERR_OTHER = "other"

# ── Verify outcomes (SI-D) ──────────────────────────────────────────────────
V_REFRESH_QTY = "refresh_qty"
V_DETACH = "detach"
V_FLAT = "flat"
V_QUARANTINE = "quarantine"


def parse_position_side(row: Any) -> str:
    """ONE parser for exchange position-row side (SI-1).

    Normalizes explicit side fields first (side / direction / positionSide;
    tokens long|buy|1 => long, short|sell|2 => short), then falls back to
    signed quantity (size / qty / quantity / positionAmt sign). Unsigned,
    directionless, zero, or unparseable input => SIDE_UNKNOWN — NEVER long.
    """
    if not isinstance(row, dict):
        return SIDE_UNKNOWN
    for key in ("side", "direction", "positionSide", "position_side"):
        raw = row.get(key)
        if raw is None:
            continue
        tok = str(raw).strip().lower()
        if tok in ("long", "buy", "1"):
            return SIDE_LONG
        if tok in ("short", "sell", "-1", "2"):
            return SIDE_SHORT
    for key in ("size", "qty", "quantity", "positionAmt", "position_amt"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            continue
        if v > 0:
            return SIDE_LONG
        if v < 0:
            return SIDE_SHORT
        return SIDE_UNKNOWN  # explicit zero = no position, side unknown
    return SIDE_UNKNOWN


def row_qty_abs(row: Any) -> Optional[float]:
    """Absolute quantity from an exchange row; None when unparseable."""
    if not isinstance(row, dict):
        return None
    for key in ("size", "qty", "quantity", "positionAmt", "position_amt"):
        raw = row.get(key)
        if raw is None:
            continue
        try:
            return abs(float(raw))
        except (TypeError, ValueError):
            return None
    return None


def row_venue(row: Any) -> str:
    """Venue stamp on an exchange row (bybit/aster stamped; SoDEX unstamped)."""
    if not isinstance(row, dict):
        return ""
    return str(row.get("venue", "") or "").lower()


def side_venue_verdict(tracked_side: str, row: Any, routed_venue: str,
                       duplicate: bool = False) -> str:
    """SI-A verdict for one tracked-position vs exchange-row pairing.

    duplicate rows (same symbol twice in one poll) and wrong-venue rows are
    venue_ambiguous — the row cannot be trusted as THIS venue's truth.
    Unknown exchange side, or a tracked side outside {long, short}, is
    side_unknown (management quarantine). Otherwise same_side/side_conflict.
    """
    if duplicate:
        return VERDICT_VENUE_AMBIGUOUS
    rv = row_venue(row)
    routed = str(routed_venue or "").lower()
    if rv and routed and rv != routed:
        return VERDICT_VENUE_AMBIGUOUS
    ex_side = parse_position_side(row)
    if ex_side == SIDE_UNKNOWN:
        return VERDICT_SIDE_UNKNOWN
    if tracked_side not in (SIDE_LONG, SIDE_SHORT):
        return VERDICT_SIDE_UNKNOWN
    return (VERDICT_SAME_SIDE if ex_side == tracked_side
            else VERDICT_SIDE_CONFLICT)


def normalize_verify_error(text: str) -> str:
    """Map a venue error string to ONE structural class (SI-6).

    Order is load-bearing: qty-invalid checked first (its text can contain
    'position' on some venues), ro-would-open before not-found.
    """
    t = str(text or "").lower()
    if "quantity is invalid" in t or "qty invalid" in t:
        return ERR_QTY_INVALID
    if "reduce only" in t and "open" in t:
        return ERR_RO_WOULD_OPEN
    if "reduce-only" in t and "open" in t:
        return ERR_RO_WOULD_OPEN
    if "not found" in t or "no position" in t:
        return ERR_POSITION_NOT_FOUND
    return ERR_OTHER


def verify_action(fetch_ok: bool, rows: Optional[List[Any]],
                  routed_venue: str, expected_side: str
                  ) -> Tuple[str, str, Optional[float], str]:
    """THE single structural verifier (SI-D / SI-6).

    Inputs: whether the fresh venue.all_positions poll succeeded, the
    symbol-filtered nonzero-size rows, the routed venue, and the tracked
    (expected) side captured at arm time.

    Returns (outcome, live_side, live_size, quarantine_reason):
      fetch failure                 -> quarantine / fetch_failed
      no rows (flat confirmed)      -> flat
      >1 row                        -> quarantine / duplicate_rows
      malformed row                 -> quarantine / malformed_row
      wrong-venue stamp             -> quarantine / wrong_venue
      unknown exchange side         -> quarantine / unknown_side
      unparseable qty               -> quarantine / malformed_row
      opposite side (any size)      -> detach (live side/size reported)
      same side                     -> refresh_qty (fresh size reported)
    """
    if not fetch_ok:
        return (V_QUARANTINE, SIDE_UNKNOWN, None, "fetch_failed")
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    if len(rows) > 1:
        return (V_QUARANTINE, SIDE_UNKNOWN, None, "duplicate_rows")
    if not rows:
        return (V_FLAT, SIDE_UNKNOWN, None, "")
    row = rows[0]
    rv = row_venue(row)
    routed = str(routed_venue or "").lower()
    if rv and routed and rv != routed:
        return (V_QUARANTINE, SIDE_UNKNOWN, None, "wrong_venue")
    live_side = parse_position_side(row)
    if live_side == SIDE_UNKNOWN:
        return (V_QUARANTINE, SIDE_UNKNOWN, None, "unknown_side")
    live_size = row_qty_abs(row)
    if live_size is None:
        return (V_QUARANTINE, SIDE_UNKNOWN, None, "malformed_row")
    if live_size <= 0:
        return (V_FLAT, SIDE_UNKNOWN, None, "")
    if expected_side in (SIDE_LONG, SIDE_SHORT) and live_side != expected_side:
        return (V_DETACH, live_side, live_size, "")
    return (V_REFRESH_QTY, live_side, live_size, "")


def capture_generation(pos: Any) -> Dict[str, Any]:
    """SI-3 generation snapshot: object identity + opened_at_ms + side +
    size + venue + symbol, captured at arm time and rechecked before any
    async verification mutates state."""
    return {
        "id": id(pos),
        "opened_at_ms": getattr(pos, "opened_at_ms", None),
        "side": getattr(pos, "side", None),
        "size": getattr(pos, "size", None),
        "venue": getattr(pos, "venue", ""),
        "symbol": getattr(pos, "symbol", ""),
    }


def generation_matches(expected: Optional[Dict[str, Any]], pos: Any) -> bool:
    """SI-3 recheck: the live position object must be the SAME generation
    the verifier was armed for. A replacement generation (close + re-entry
    between arm and fire) can never be mutated by the old verdict."""
    if not expected or pos is None:
        return False
    if id(pos) != expected.get("id"):
        return False
    if getattr(pos, "opened_at_ms", None) != expected.get("opened_at_ms"):
        return False
    if getattr(pos, "side", None) != expected.get("side"):
        return False
    return True


def bracket_merge_side_ok(existing_side: str, incoming_side: str,
                          enabled: bool = True) -> bool:
    """SI-G: never merge bracket geometry across a side conflict.

    Kill switch off => legacy (merge). Unknown side on either track fails
    closed (no merge). Equal sides merge; long vs short never merges.
    """
    if not enabled:
        return True
    if existing_side not in (SIDE_LONG, SIDE_SHORT):
        return False
    if incoming_side not in (SIDE_LONG, SIDE_SHORT):
        return False
    return existing_side == incoming_side


def grow_adoption_allowed(has_fresh_side_matched_intent: bool) -> bool:
    """SI-C: a foreign same-side grow may only be ADOPTED when fresh
    side-matching ARIA add evidence exists (late-fill / self-heal path).
    Otherwise the grow is DEFERRED — tracked size and anchors untouched."""
    return bool(has_fresh_side_matched_intent)


def journal_entry_side(entry: Optional[Dict[str, Any]]) -> str:
    """Side recorded on a journal entry: additive 'side' field preferred,
    legacy 'direction' (candidate.side) as fallback. Missing => unknown."""
    if not isinstance(entry, dict):
        return SIDE_UNKNOWN
    for key in ("side", "direction"):
        raw = entry.get(key)
        if raw is None:
            continue
        tok = str(raw).strip().lower()
        if tok in ("long", "buy"):
            return SIDE_LONG
        if tok in ("short", "sell"):
            return SIDE_SHORT
    return SIDE_UNKNOWN


def intent_side_matches(entry: Optional[Dict[str, Any]], side: str) -> bool:
    """SI-5/SI-F intent matching: a journal intent row only matches when its
    side EQUALS the requested side. Side-less legacy rows (unknown) fail
    closed — a stale opposite-side intent can never resurrect adoption."""
    if side not in (SIDE_LONG, SIDE_SHORT):
        return False
    return journal_entry_side(entry) == side
