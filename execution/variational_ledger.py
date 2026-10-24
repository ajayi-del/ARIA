"""
execution/variational_ledger.py — Manual-position mirror for the Variational venue.

PURPOSE (Governor venue-asymmetry axiom: "Variational = manual ledger"):
the Governor trades MANUALLY on the Variational venue. ARIA holds a mirror
of those manual positions so future telemetry/observatory logic can account
for them. The bot NEVER manages, sizes, routes, or acts on these positions —
this module is a BOOK OF RECORD, not a strategy surface. There is no splice
point in main.py and there must never be one that mutates the trade path
from this data without an explicit Governor order.

STORAGE DOCTRINE (house pattern — journal permanence rule #14, the
2026-07-26 calendar.db "disk image malformed" precedent, and the
intelligence/pair_spread.py ledger idiom):
  - Append-only JSONL journal: logs/variational_manual_ledger.jsonl
    One event per line: {ts, ts_utc, op, symbol, ...fields}
    op ∈ {"open", "update", "close", "note"}. Journals are NEVER mutated or
    deleted — state is derived, never edited in place.
  - Atomic snapshot: logs/variational_manual_positions.json
    tmp+replace idiom (core/state_persistence.atomic_save pattern —
    reimplemented here for an honest bool return). Rewritten after every
    successful mutation.
  - One-bad-line doctrine on journal reads: a corrupt line kills only
    itself, never the file, never the caller.
  - Reads prefer the snapshot; a missing/corrupt snapshot falls back to a
    full journal replay (the journal is the source of truth).

KILL SWITCH: VARIATIONAL_LEDGER_ENABLED (env, call-time read, default true).
False = every write is a no-op returning False and every read returns {} —
indistinguishable from the module not existing (department template §1.3).

POSITION SCHEMA:
  {symbol, side ("long"|"short"), qty, entry_price, leverage (optional),
   venue="variational", source="manual", opened_ts, updated_ts,
   note (optional str)}

Timestamps are epoch floats (machine register); the journal also carries a
UTC Z-suffixed ts_utc string for log joins (Berlin-time law: machine
registers stay UTC).

Validation doctrine: qty > 0, entry_price > 0, side ∈ {long, short},
leverage > 0 when provided, all finite. Invalid writes return False and
NEVER raise — a bookkeeping mirror must never crash the trade path.
"""

from __future__ import annotations

import json
import math
import os
import time
from pathlib import Path
from typing import Dict, Optional

import structlog

log = structlog.get_logger(__name__)

VENUE = "variational"
SOURCE = "manual"
SIDES = ("long", "short")
OPS = ("open", "update", "close", "note")

JOURNAL_NAME = "variational_manual_ledger.jsonl"
SNAPSHOT_NAME = "variational_manual_positions.json"

# Fields update_position() is allowed to mutate. opened_ts is immutable —
# the open event owns it (journal permanence doctrine).
UPDATABLE_FIELDS = ("qty", "entry_price", "leverage", "note")


# ── Kill switch ─────────────────────────────────────────────────────────────

def variational_ledger_enabled() -> bool:
    """Call-time env read (issue-#17 class: knobs must bind at call time,
    not import time). Default true; False = pre-module system bit-for-bit."""
    return os.environ.get("VARIATIONAL_LEDGER_ENABLED", "true").strip().lower() != "false"


# ── Validation (pure, never raises) ─────────────────────────────────────────

def _finite_positive(value) -> Optional[float]:
    """Coerce to a finite float > 0, else None."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v <= 0.0:
        return None
    return v


def _valid_side(side) -> Optional[str]:
    s = str(side or "").strip().lower()
    return s if s in SIDES else None


def _valid_note(note) -> bool:
    return note is None or isinstance(note, str)


def _iso_z(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


# ── The ledger ──────────────────────────────────────────────────────────────

class VariationalLedger:
    """Mirror of the Governor's manual Variational positions.

    Owns its file I/O (this is a persistence module, not a zero-I/O brain).
    Stateless in memory: every operation re-derives state from disk
    (snapshot or journal replay) so a crashed writer can never strand
    in-memory state ahead of the journal.
    """

    def __init__(self, log_dir=None):
        """log_dir: str/Path pins the directory; None = resolve at call time
        from env VARIATIONAL_LEDGER_LOG_DIR, default 'logs' (testability —
        tests pin tmp_path via the constructor)."""
        self._log_dir_arg = log_dir

    # ── paths ──

    def _log_dir(self) -> Path:
        if self._log_dir_arg is not None:
            return Path(self._log_dir_arg)
        return Path(os.environ.get("VARIATIONAL_LEDGER_LOG_DIR", "logs"))

    @property
    def journal_path(self) -> Path:
        return self._log_dir() / JOURNAL_NAME

    @property
    def snapshot_path(self) -> Path:
        return self._log_dir() / SNAPSHOT_NAME

    # ── writes ──

    def _append_event(self, event: dict) -> bool:
        """Append one JSON line to the journal. Never raises — a write
        failure costs this event and returns False (one-bad-line doctrine
        applied to writes)."""
        try:
            line = json.dumps(event, default=str)
        except Exception as _se:
            log.warning("variational_ledger_serialize_failed", error=str(_se)[:120])
            return False
        try:
            path = self.journal_path
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception as _we:
            log.warning("variational_ledger_write_failed", error=str(_we)[:120])
            return False

    def _atomic_write_snapshot(self, positions: Dict[str, dict]) -> bool:
        """tmp+replace atomic write (core/state_persistence.atomic_save
        idiom) with an honest bool return. Never raises."""
        path = self.snapshot_path
        tmp = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"saved_at": time.time(), "positions": positions}, f)
            os.replace(tmp, path)
            return True
        except Exception as _e:
            log.warning("variational_ledger_snapshot_failed", error=str(_e)[:120])
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
            return False

    # ── reads ──

    def _read_snapshot(self) -> Optional[Dict[str, dict]]:
        """Snapshot positions dict; None when missing or corrupt (caller
        falls back to journal replay). Never raises."""
        path = self.snapshot_path
        if not path.exists():
            return None
        try:
            with open(path, encoding="utf-8") as f:
                payload = json.load(f)
            positions = payload.get("positions")
            if not isinstance(positions, dict):
                return None
            return positions
        except Exception:
            return None

    def _replay_journal(self) -> Dict[str, dict]:
        """Rebuild state from the append-only journal. Last event wins per
        symbol; one bad line kills only itself."""
        positions: Dict[str, dict] = {}
        path = self.journal_path
        if not path.exists():
            return positions
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue  # one-bad-line doctrine
                    if not isinstance(ev, dict):
                        continue
                    op, symbol = ev.get("op"), ev.get("symbol")
                    if op not in OPS or not symbol or not isinstance(symbol, str):
                        continue
                    ts = ev.get("ts")
                    try:
                        ts = float(ts)
                    except (TypeError, ValueError):
                        continue
                    if op == "open":
                        side = _valid_side(ev.get("side"))
                        qty = _finite_positive(ev.get("qty"))
                        entry = _finite_positive(ev.get("entry_price"))
                        if side is None or qty is None or entry is None:
                            continue  # corrupt open row kills itself
                        lev = ev.get("leverage")
                        lev = _finite_positive(lev) if lev is not None else None
                        note = ev.get("note")
                        positions[symbol] = {
                            "symbol": symbol,
                            "side": side,
                            "qty": qty,
                            "entry_price": entry,
                            "leverage": lev,
                            "venue": VENUE,
                            "source": SOURCE,
                            "opened_ts": ts,
                            "updated_ts": ts,
                            "note": note if isinstance(note, str) else None,
                        }
                    elif op in ("update", "note"):
                        pos = positions.get(symbol)
                        if pos is None:
                            continue  # update to a non-open symbol = no-op
                        for field in UPDATABLE_FIELDS:
                            if field in ev:
                                pos[field] = ev[field]
                        pos["updated_ts"] = ts
                    elif op == "close":
                        positions.pop(symbol, None)
        except OSError:
            return {}
        return positions

    def _load_state(self) -> Dict[str, dict]:
        """Snapshot first; journal replay when the snapshot is missing or
        corrupt (the journal is the source of truth)."""
        snap = self._read_snapshot()
        if snap is not None:
            return snap
        return self._replay_journal()

    # ── public API ──

    def open_position(self, symbol, side, qty, entry_price,
                      leverage=None, note=None, ts=None) -> bool:
        """Open a mirrored manual position. Refuses (False) when the symbol
        is already mirrored — close first (an implicit re-open would silently
        reset opened_ts; explicit is doctrine). Never raises."""
        if not variational_ledger_enabled():
            return False
        if not isinstance(symbol, str) or not symbol.strip():
            return False
        symbol = symbol.strip()
        side = _valid_side(side)
        qty = _finite_positive(qty)
        entry = _finite_positive(entry_price)
        if side is None or qty is None or entry is None:
            return False
        if leverage is not None:
            leverage = _finite_positive(leverage)
            if leverage is None:
                return False
        if not _valid_note(note):
            return False
        ts = time.time() if ts is None else float(ts)

        state = self._load_state()
        if symbol in state:
            return False
        position = {
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "entry_price": entry,
            "leverage": leverage,
            "venue": VENUE,
            "source": SOURCE,
            "opened_ts": ts,
            "updated_ts": ts,
            "note": note,
        }
        event = {"ts": ts, "ts_utc": _iso_z(ts), "op": "open", "symbol": symbol,
                 "side": side, "qty": qty, "entry_price": entry,
                 "leverage": leverage, "note": note}
        if not self._append_event(event):
            return False
        state[symbol] = position
        return self._atomic_write_snapshot(state)

    def update_position(self, symbol, ts=None, **fields) -> bool:
        """Mutate qty/entry_price/leverage/note on a mirrored position.
        Unknown fields, invalid values, or a missing symbol return False.
        Never raises."""
        if not variational_ledger_enabled():
            return False
        if not isinstance(symbol, str) or not symbol.strip():
            return False
        symbol = symbol.strip()
        unknown = set(fields) - set(UPDATABLE_FIELDS)
        if unknown or not fields:
            return False
        if "qty" in fields:
            fields["qty"] = _finite_positive(fields["qty"])
            if fields["qty"] is None:
                return False
        if "entry_price" in fields:
            fields["entry_price"] = _finite_positive(fields["entry_price"])
            if fields["entry_price"] is None:
                return False
        if "leverage" in fields and fields["leverage"] is not None:
            fields["leverage"] = _finite_positive(fields["leverage"])
            if fields["leverage"] is None:
                return False
        if "note" in fields and not _valid_note(fields["note"]):
            return False
        ts = time.time() if ts is None else float(ts)

        state = self._load_state()
        pos = state.get(symbol)
        if pos is None:
            return False
        for field, value in fields.items():
            pos[field] = value
        pos["updated_ts"] = ts
        # op "note" when only the note changed — the journal vocabulary
        # distinguishes commentary from position mutation.
        op = "note" if set(fields) == {"note"} else "update"
        event = {"ts": ts, "ts_utc": _iso_z(ts), "op": op, "symbol": symbol}
        event.update(fields)
        if not self._append_event(event):
            return False
        return self._atomic_write_snapshot(state)

    def close_position(self, symbol, ts=None) -> bool:
        """Remove a mirrored position (the Governor closed it manually).
        False when the symbol is not mirrored. Never raises."""
        if not variational_ledger_enabled():
            return False
        if not isinstance(symbol, str) or not symbol.strip():
            return False
        symbol = symbol.strip()
        ts = time.time() if ts is None else float(ts)

        state = self._load_state()
        if symbol not in state:
            return False
        event = {"ts": ts, "ts_utc": _iso_z(ts), "op": "close", "symbol": symbol}
        if not self._append_event(event):
            return False
        state.pop(symbol, None)
        return self._atomic_write_snapshot(state)

    def get_positions(self) -> Dict[str, dict]:
        """symbol -> position dict for all mirrored manual positions.
        Reads the snapshot; falls back to journal replay when the snapshot
        is missing/corrupt. Empty dict when nothing is mirrored or the
        kill switch is off. Never raises."""
        if not variational_ledger_enabled():
            return {}
        return self._load_state()

    def snapshot_now(self) -> bool:
        """Rewrite the atomic snapshot from current state. Called internally
        after every mutation; exposed for operators/tools. False on write
        failure or when disabled. Never raises."""
        if not variational_ledger_enabled():
            return False
        return self._atomic_write_snapshot(self._load_state())


# ── Module-level default instance (house singleton idiom) ──────────────────

_default_ledger = VariationalLedger()


def open_position(symbol, side, qty, entry_price, leverage=None, note=None, ts=None) -> bool:
    return _default_ledger.open_position(symbol, side, qty, entry_price,
                                         leverage=leverage, note=note, ts=ts)


def update_position(symbol, ts=None, **fields) -> bool:
    return _default_ledger.update_position(symbol, ts=ts, **fields)


def close_position(symbol, ts=None) -> bool:
    return _default_ledger.close_position(symbol, ts=ts)


def get_positions() -> Dict[str, dict]:
    return _default_ledger.get_positions()


def snapshot_now() -> bool:
    return _default_ledger.snapshot_now()
