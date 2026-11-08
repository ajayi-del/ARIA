"""Hyperliquid public-info mark plane (external-review spec §3, GO 2026-11-08).

DESIGN LAW — HL is a REFERENCE plane, never execution truth:
- equity / uPnL / headroom / band ladder / breach math stay propr-authoritative
- HL marks drive TRIGGERS and TELEMETRY only, NEVER risk math
- propr B-books fills and smooths HIP-3 marks off-hours; divergence is expected

Fail-OPEN everywhere: no HL data (down, rate-limited, stale) = the harness
behaves exactly as before this module existed. Supervised loop, staleness
abstain. Zero keys, zero cost. Trust precedent: ARIA's whale-mirror leg
(clearinghouseState, unsigned).

Two dexes: the main perp dex (crypto) and the HIP-3 "xyz" dex (equities,
commodities, fx, pre-IPO) — separate allMids calls, one merged store.
"""

from __future__ import annotations

import time

import requests

DEFAULT_URL = "https://api.hyperliquid.xyz/info"

# Harness asset -> HL name. Identity for everything on both dexes today
# (crypto mains are bare names, xyz assets carry the dex prefix already);
# the map exists so a rename is a one-line change, not a code hunt.
HL_NAME: dict[str, str] = {}


def hl_name(asset: str) -> str:
    return HL_NAME.get(asset, asset)


class HLMarkPlane:
    """Merged allMids snapshot (main + xyz dex) with a staleness gate."""

    def __init__(self, url: str = DEFAULT_URL, stale_s: float = 60.0,
                 timeout_s: float = 5.0, session: requests.Session | None = None):
        self.url = url
        self.stale_s = stale_s
        self.timeout_s = timeout_s
        self._session = session or requests.Session()
        self._marks: dict[str, float] = {}
        self.ts: float = 0.0
        self.last_error: str | None = None

    def _all_mids(self, dex: str | None) -> dict:
        body: dict = {"type": "allMids"}
        if dex:
            body["dex"] = dex
        r = self._session.post(self.url, json=body, timeout=self.timeout_s)
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict):
            raise ValueError(f"allMids shape {type(data).__name__}")
        return data

    def poll(self, now: float | None = None) -> int:
        """Refresh both dexes. Returns merged mark count. On failure: marks
        keep their old ts (staleness gate retires them), last_error is set,
        the exception re-raises for the caller's supervised loop."""
        try:
            merged: dict[str, float] = {}
            for dex in (None, "xyz"):
                for name, raw in self._all_mids(dex).items():
                    try:
                        v = float(raw)
                    except (TypeError, ValueError):
                        continue
                    if v > 0:
                        merged[name] = v
            if not merged:
                raise ValueError("allMids empty on both dexes")
        except Exception as exc:
            self.last_error = str(exc)
            raise
        self._marks = merged
        self.ts = time.time() if now is None else now
        self.last_error = None
        return len(merged)

    def mark(self, asset: str, now: float | None = None) -> float | None:
        """Fresh mark or None. None = abstain (caller runs legacy path)."""
        now = time.time() if now is None else now
        if self.ts <= 0 or now - self.ts > self.stale_s:
            return None
        return self._marks.get(hl_name(asset))

    def snapshot(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        return {"ts": self.ts, "n": len(self._marks),
                "age_s": round(now - self.ts, 1) if self.ts else None,
                "stale": (self.ts <= 0 or now - self.ts > self.stale_s),
                "last_error": self.last_error}
