"""Hyperliquid first-party OBSERVER data plane (task #37, 2026-11-17).

MISSION: an independent mark/uPnL oracle the harness uses to cross-check
the Propr API mirrors, and the warm adapter a future HL-direct account
would reuse. OBSERVER-CLASS: zero trade-path wiring, zero order
capability, public unsigned endpoints only (no EIP-712, no keys).

Endpoints (POST https://api.hyperliquid.xyz/info):
  - allMids (main + xyz dex)          -> mark prices
  - clearinghouseState (user=wallet)  -> per-seat equity / uPnL
  - metaAndAssetCtxs                  -> funding / open interest

CONTRACT:
  - Pure data class with an explicit tick() — NO asyncio loop inside;
    the harness main decides cadence later (wiring is a separate task).
  - Every method is fail-OPEN-None on any network/parse error: a dead
    plane never poisons the harness. last_good snapshots are retained
    with timestamps; the staleness gate retires them.
  - REFERENCE-ONLY: divergence verdicts are telemetry, never risk math
    (same design law as io/hl_marks.py — HL is never execution truth).
  - requests.Session, matching propr_client.py / hl_marks.py.
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from typing import Any

import requests

from .journal import atomic_write_json

DEFAULT_URL = "https://api.hyperliquid.xyz/info"
DEFAULT_POLL_S = 30.0
DEFAULT_DIVERGE_BPS = 50.0
DEFAULT_MARK_HISTORY = 96
DEFAULT_JUMP_FRAC = 0.15   # per-poll jump guard; 0.0 disables (legacy)


def parse_wallets(csv: str) -> list[str]:
    """PROP_HL_PLANE_WALLETS csv -> clean address list."""
    return [w.strip() for w in (csv or "").split(",") if w.strip()]


def divergence_bps(hl_mark: Any, propr_mark: Any) -> float | None:
    """Signed HL-vs-Propr mark divergence in bps.

    Positive = HL marks HIGHER than the Propr mirror. None on invalid
    inputs (missing / non-numeric / non-positive marks abstain).
    """
    try:
        hl = float(hl_mark)
        pr = float(propr_mark)
    except (TypeError, ValueError):
        return None
    if hl != hl or pr != pr:  # NaN
        return None
    if hl <= 0 or pr <= 0:
        return None
    return (hl - pr) / pr * 1e4


def divergence_verdict(hl_mark: Any, propr_mark: Any,
                       threshold_bps: float = DEFAULT_DIVERGE_BPS) -> dict:
    """Telemetry verdict: suspect when |divergence| exceeds the threshold.

    Never raises; abstains (suspect=False, divergence None) on bad input.
    """
    d = divergence_bps(hl_mark, propr_mark)
    if d is None:
        return {"divergence_bps": None, "suspect": False}
    try:
        thr = abs(float(threshold_bps))
    except (TypeError, ValueError):
        thr = DEFAULT_DIVERGE_BPS
    return {"divergence_bps": d, "suspect": abs(d) > thr}


def parse_clearinghouse(data: Any) -> dict | None:
    """clearinghouseState response -> {equity, upnl, positions} or None.

    equity = marginSummary.accountValue; upnl = sum of open-position
    unrealizedPnl. None when the accountValue leg is unparseable.
    """
    if not isinstance(data, dict):
        return None
    ms = data.get("marginSummary") or {}
    try:
        equity = float(ms.get("accountValue"))
    except (TypeError, ValueError):
        return None
    if equity != equity:
        return None
    upnl = 0.0
    n_pos = 0
    for ap in data.get("assetPositions") or []:
        pos = (ap or {}).get("position") or {}
        try:
            upnl += float(pos.get("unrealizedPnl", 0.0) or 0.0)
            n_pos += 1
        except (TypeError, ValueError):
            continue
    return {"equity": equity, "upnl": upnl, "positions": n_pos}


def parse_meta_ctxs(data: Any) -> dict[str, dict]:
    """metaAndAssetCtxs -> {name: {funding, open_interest}} (bad rows dropped)."""
    out: dict[str, dict] = {}
    if not (isinstance(data, list) and len(data) == 2):
        return out
    meta, ctxs = data
    universe = (meta or {}).get("universe") or []
    if not isinstance(ctxs, list):
        return out
    for u, c in zip(universe, ctxs):
        name = (u or {}).get("name")
        if not name:
            continue
        try:
            out[name] = {
                "funding": float(c.get("funding")),
                "open_interest": float(c.get("openInterest")),
            }
        except (TypeError, ValueError, AttributeError):
            continue
    return out


class HLDataPlane:
    """Observer-class HL oracle. tick() = one poll cycle; never raises."""

    def __init__(self, *, url: str = DEFAULT_URL,
                 wallets: list[str] | tuple[str, ...] = (),
                 poll_s: float = DEFAULT_POLL_S,
                 diverge_bps: float = DEFAULT_DIVERGE_BPS,
                 timeout_s: float = 5.0,
                 mark_history: int = DEFAULT_MARK_HISTORY,
                 stale_s: float | None = None,
                 jump_frac: float = DEFAULT_JUMP_FRAC,
                 session: requests.Session | None = None):
        self.url = url
        self.wallets = list(wallets)
        self.poll_s = float(poll_s)
        self.diverge_bps = float(diverge_bps)
        self.timeout_s = float(timeout_s)
        # Staleness gate: ~3 missed polls before the plane goes dark.
        self.stale_s = (float(stale_s) if stale_s
                        else max(3.0 * self.poll_s, 60.0))
        self.jump_frac = max(0.0, float(jump_frac))
        self._jump_quarantined = 0
        self._hist_cap = max(1, int(mark_history))
        self._session = session or requests.Session()
        # last_good state
        self._marks: dict[str, float] = {}
        self._marks_ts: float = 0.0
        self._hist: dict[str, deque] = {}
        self._seats: dict[str, dict] = {}
        self._ctxs: dict[str, dict] = {}
        self._ctxs_ts: float = 0.0
        self.last_error: str | None = None

    # ── HTTP (the only raising layer; callers wrap) ──────────────────────

    def _info(self, body: dict) -> Any:
        r = self._session.post(self.url, json=body, timeout=self.timeout_s)
        r.raise_for_status()
        return r.json()

    def _all_mids(self, dex: str | None) -> dict:
        body: dict[str, Any] = {"type": "allMids"}
        if dex:
            body["dex"] = dex
        data = self._info(body)
        if not isinstance(data, dict):
            raise ValueError(f"allMids shape {type(data).__name__}")
        return data

    # ── poll legs (fail-open-None; last_good retained) ──────────────────

    def fetch_marks(self, now: float | None = None) -> int | None:
        """One allMids poll (main + xyz dex). Merged count, or None on failure.

        Jump guard (A9, 2026-11-17): a mark that jumps > jump_frac vs the
        symbol's last accepted mark is QUARANTINED — the last good print
        survives and the quarantine counter ticks. A raise-only ratchet
        (mfe_peak_r) fed one bad HIGH print is permanently inflated; the
        venue has served cross-scale prints before (SPCX 5.66x class).
        0.0 disables (legacy bit-for-bit)."""
        now = time.time() if now is None else now
        try:
            merged: dict[str, float] = {}
            for dex in (None, "xyz"):
                for name, raw in self._all_mids(dex).items():
                    try:
                        v = float(raw)
                    except (TypeError, ValueError):
                        continue
                    if v > 0 and v == v:
                        merged[name] = v
            if not merged:
                raise ValueError("allMids empty on both dexes")
        except Exception as exc:
            self.last_error = f"marks: {exc}"
            return None
        if self.jump_frac > 0.0:
            clean: dict[str, float] = {}
            for name, v in merged.items():
                prev = self._marks.get(name)
                if prev and prev > 0 and abs(v - prev) / prev > self.jump_frac:
                    self._jump_quarantined += 1
                    clean[name] = prev
                    continue
                clean[name] = v
            merged = clean
        self._marks = merged
        self._marks_ts = now
        for name, v in merged.items():
            dq = self._hist.get(name)
            if dq is None:
                dq = self._hist[name] = deque(maxlen=self._hist_cap)
            dq.append((now, v))
        self.last_error = None
        return len(merged)

    def fetch_seats(self, now: float | None = None) -> int | None:
        """clearinghouseState per configured wallet (both dexes merged).

        Number of wallets refreshed, or None when every seat failed.
        Per-wallet failures skip that seat (its last_good survives).
        """
        if not self.wallets:
            return 0
        now = time.time() if now is None else now
        ok = 0
        first_err: str | None = None
        for wallet in self.wallets:
            legs: list[dict] = []
            for dex in (None, "xyz"):
                body: dict[str, Any] = {"type": "clearinghouseState",
                                        "user": wallet}
                if dex:
                    body["dex"] = dex
                try:
                    parsed = parse_clearinghouse(self._info(body))
                except Exception as exc:
                    first_err = first_err or f"seat {wallet}: {exc}"
                    continue
                if parsed is not None:
                    legs.append(parsed)
            if not legs:
                first_err = first_err or f"seat {wallet}: unparseable"
                continue
            self._seats[wallet] = {
                "equity": sum(l["equity"] for l in legs),
                "upnl": sum(l["upnl"] for l in legs),
                "positions": sum(l["positions"] for l in legs),
                "ts": now,
            }
            ok += 1
        if ok == 0:
            self.last_error = first_err or "seats: no wallets refreshed"
            return None
        self.last_error = None
        return ok

    def fetch_ctxs(self, now: float | None = None) -> int | None:
        """metaAndAssetCtxs funding/OI (both dexes). Count, or None on failure."""
        now = time.time() if now is None else now
        try:
            merged: dict[str, dict] = {}
            for dex in (None, "xyz"):
                body: dict[str, Any] = {"type": "metaAndAssetCtxs"}
                if dex:
                    body["dex"] = dex
                merged.update(parse_meta_ctxs(self._info(body)))
            if not merged:
                raise ValueError("metaAndAssetCtxs empty on both dexes")
        except Exception as exc:
            self.last_error = f"ctxs: {exc}"
            return None
        self._ctxs = merged
        self._ctxs_ts = now
        self.last_error = None
        return len(merged)

    def tick(self, now: float | None = None) -> dict:
        """One full poll cycle: marks + ctxs + seats. NEVER raises."""
        now = time.time() if now is None else now
        marks = self.fetch_marks(now)
        ctxs = self.fetch_ctxs(now)
        seats = self.fetch_seats(now)
        return {"ts": now, "marks": marks, "ctxs": ctxs, "seats": seats,
                "ok": marks is not None, "last_error": self.last_error}

    # ── reads (staleness-gated, fail-open-None) ─────────────────────────

    def _fresh(self, ts: float, now: float) -> bool:
        return ts > 0 and now - ts <= self.stale_s

    def mark(self, symbol: str, now: float | None = None) -> float | None:
        """Fresh HL mark for a symbol (e.g. BTC, xyz:GOLD), else None."""
        now = time.time() if now is None else now
        if not self._fresh(self._marks_ts, now):
            return None
        return self._marks.get(symbol)

    def equity(self, wallet: str, now: float | None = None) -> dict | None:
        """Fresh per-seat {equity, upnl, positions, ts}, else None."""
        now = time.time() if now is None else now
        seat = self._seats.get(wallet)
        if not seat or not self._fresh(float(seat.get("ts", 0.0)), now):
            return None
        return dict(seat)

    def funding(self, symbol: str, now: float | None = None) -> dict | None:
        now = time.time() if now is None else now
        if not self._fresh(self._ctxs_ts, now):
            return None
        row = self._ctxs.get(symbol)
        return dict(row) if row else None

    def history(self, symbol: str) -> list[tuple[float, float]]:
        """Bounded (ts, mark) series — newest last."""
        dq = self._hist.get(symbol)
        return list(dq) if dq else []

    def staleness(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        return {
            "marks_age_s": (round(now - self._marks_ts, 1)
                            if self._marks_ts else None),
            "marks_stale": not self._fresh(self._marks_ts, now),
            "ctxs_age_s": (round(now - self._ctxs_ts, 1)
                           if self._ctxs_ts else None),
            "seats": {w: round(now - float(s.get("ts", 0.0)), 1)
                      for w, s in self._seats.items()},
            "last_error": self.last_error,
        }

    def check_divergence(self, symbol: str, propr_mark: Any,
                         now: float | None = None) -> dict:
        """Telemetry-only oracle verdict vs a Propr-mirrored mark."""
        return divergence_verdict(self.mark(symbol, now), propr_mark,
                                  self.diverge_bps)

    # ── snapshot (state-root pattern; atomic tmp+replace) ───────────────

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        return {
            "ts": now,
            "marks_ts": self._marks_ts,
            "n_marks": len(self._marks),
            "marks": dict(self._marks),
            "seats": {w: dict(s) for w, s in self._seats.items()},
            "ctxs_ts": self._ctxs_ts,
            "ctxs": {k: dict(v) for k, v in self._ctxs.items()},
            "diverge_bps": self.diverge_bps,
            "staleness": self.staleness(now),
            "jump_quarantined": self._jump_quarantined,
        }

    def dump_snapshot(self, path: Path, now: float | None = None) -> bool:
        """Atomic JSON dump. Fail-open: False on any write error."""
        try:
            atomic_write_json(Path(path), self.snapshot(now))
        except (OSError, TypeError, ValueError):
            return False
        return True
