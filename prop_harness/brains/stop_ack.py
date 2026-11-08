"""Protective stop acknowledgement FSM (management-layer §6 build #1).

Typed protection truth for the prop sleeve: place → ARMED → ACKED.
Timeout / lost confirmation → MISSED → halt new risk + heal
(``resize_protection``) then reduce naked exposure. Pure brain — no
venue I/O, no new stop writers (trail/BE/resize ownership unchanged).

Fail-closed:
- ACKED + absent/unknown open-orders → MISSED
- any open MISSED → fleet HALT (new place/reload refused)

Kill: ``PROP_STOP_ACK_FSM=0`` → legacy bit-for-bit (caller skips).
Reduce kill: ``PROP_STOP_ACK_REDUCE=0`` → halt+heal only.
"""

from __future__ import annotations

from typing import Any

# Typed states — string literals so registry JSON stays human-readable.
ARMED = "ARMED"
ACKED = "ACKED"
MISSED = "MISSED"

STATES = frozenset({ARMED, ACKED, MISSED})

# Observation plane for open-order scans.
PRESENT = "present"
ABSENT = "absent"
UNKNOWN = "unknown"

DEFAULT_TIMEOUT_S = 30.0


def normalize_state(raw: Any) -> str | None:
    """Return a known state or None (legacy / unset rows)."""
    if raw is None:
        return None
    s = str(raw).strip().upper()
    return s if s in STATES else None


def stamp_armed(bracket: dict, now: float) -> dict:
    """Birth / re-arm after a successful protection place.

    Mutates ``bracket`` in place and returns it. Callers stamp only after
    venue place work succeeds (place_bracket, resize heal, trail/BE).
    """
    bracket["stop_ack_state"] = ARMED
    bracket["stop_ack_ts"] = float(now)
    bracket["stop_ack_reason"] = "placed"
    return bracket


def protection_present(open_orders: list[dict] | None, side: str) -> str:
    """Classify resting protective stop for a bracket side.

    ``open_orders=None`` = fetch failed → UNKNOWN (fail-closed once ACKED).
    Looks for ``stop_market`` + ``reduceOnly`` on the exit side.
    """
    if open_orders is None:
        return UNKNOWN
    if not isinstance(open_orders, list):
        return UNKNOWN
    exit_side = "sell" if side == "buy" else "buy"
    for o in open_orders:
        if not isinstance(o, dict):
            continue
        if o.get("type") != "stop_market":
            continue
        if o.get("reduceOnly") is False:
            continue
        if o.get("side") and o.get("side") != exit_side:
            continue
        status = o.get("status")
        if status is not None and status not in ("open", "pending"):
            continue
        return PRESENT
    return ABSENT


def transition(
    state: str | None,
    *,
    now: float,
    armed_ts: float | None,
    observation: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> tuple[str, str]:
    """Advance FSM. Returns ``(new_state, reason)``.

    Rules:
    - None/unknown prior → treat as ARMED at ``armed_ts or now`` (adopt).
    - ARMED + PRESENT → ACKED
    - ARMED + ABSENT/UNKNOWN past timeout → MISSED
    - ARMED + ABSENT/UNKNOWN inside timeout → stay ARMED
    - ACKED + PRESENT → stay ACKED
    - ACKED + ABSENT/UNKNOWN → MISSED (fail-closed)
    - MISSED → stay MISSED (heal/reduce are actuation, not transitions)
    """
    obs = observation if observation in (PRESENT, ABSENT, UNKNOWN) else UNKNOWN
    cur = normalize_state(state) or ARMED
    ts = float(armed_ts) if armed_ts is not None else float(now)
    age = max(0.0, float(now) - ts)
    to = max(0.0, float(timeout_s))

    if cur == MISSED:
        return MISSED, "latched"

    if cur == ACKED:
        if obs == PRESENT:
            return ACKED, "confirmed"
        # Fail-closed: lost confirmation OR dark order plane.
        reason = "ack_lost" if obs == ABSENT else "ack_unknown"
        return MISSED, reason

    # ARMED (incl. adopt of None)
    if obs == PRESENT:
        return ACKED, "acked"
    if age >= to:
        reason = "ack_timeout" if obs == ABSENT else "ack_timeout_unknown"
        return MISSED, reason
    return ARMED, "awaiting" if obs == ABSENT else "awaiting_unknown"


def fleet_halt(brackets: list[dict] | None) -> bool:
    """True when any *open* bracket is MISSED — halt new risk."""
    if not brackets:
        return False
    for b in brackets:
        if not isinstance(b, dict):
            continue
        if b.get("status") != "open":
            continue
        if normalize_state(b.get("stop_ack_state")) == MISSED:
            return True
    return False


def apply_transition(bracket: dict, new_state: str, reason: str,
                     now: float) -> bool:
    """Stamp transition onto bracket. Returns True if state changed."""
    prev = normalize_state(bracket.get("stop_ack_state"))
    new = normalize_state(new_state) or ARMED
    changed = prev != new
    bracket["stop_ack_state"] = new
    bracket["stop_ack_reason"] = reason
    if changed:
        bracket["stop_ack_ts"] = float(now)
        if new == MISSED:
            bracket["stop_ack_missed"] = True
    return changed


def should_reduce(*, reduce_enabled: bool, state: str | None) -> bool:
    """Reduce (close naked) only when MISSED and reduce kill is armed."""
    return bool(reduce_enabled) and normalize_state(state) == MISSED
