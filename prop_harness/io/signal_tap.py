"""Signal tap — the harness's one-way read of ARIA's prop_outbox.jsonl.

CEO prop-readonly-bridge-1106 contract: pull-only, effectively-once
(durable idempotency via deterministic source ids), TTL quarantine
(stale signals are dead on arrival), gap quarantine (file shrink or
rotation resets the cursor; dedup absorbs the replay). Symbol filter:
SYMBOL_MAP — crypto + commodities + indices + stocks (Governor
2026-11-07; the funded-program sleeve partition lives in the account
layer, not the tap).

Byte-offset doctrine: the cursor only ever advances past COMPLETE
lines. A trailing partial line is left unconsumed and re-read on the
next poll, so a mid-write read can never corrupt a record.

Transports: local file (same-host future, tests, demos) and ssh
tail-pull against the ARIA VM (current cross-machine reality).
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

from .journal import atomic_write_json, read_json

# Symbol universe (Governor 2026-11-07: all three classes on the demo
# account — sleeve-B-only filter superseded; the 1:1 account↔sleeve
# partition still governs the FUNDED multi-account program). Every asset
# needs a SPECS row in bracket.py (xyz: assets fall back to
# XYZ_DEFAULT_SPEC) and a venue margin config set at provisioning.
SYMBOL_MAP = {
    # crypto
    "BTC-USD": "BTC", "ETH-USD": "ETH", "SOL-USD": "SOL",
    "DOGE-USD": "DOGE", "XRP-USD": "XRP", "AVAX-USD": "AVAX",
    "LINK-USD": "LINK", "AAVE-USD": "AAVE",
    "ZEC-USD": "ZEC", "BCH-USD": "BCH",
    # commodities (XAUT is ARIA's gold vehicle)
    "XAUT-USD": "xyz:GOLD", "SILVER-USD": "xyz:SILVER",
    "COPPER-USD": "xyz:COPPER", "CL-USD": "xyz:CL",
    # indices
    "SPCX-USD": "xyz:SP500", "USTECH100-USD": "xyz:XYZ100",
    # single-name stocks (venue-probed listed 2026-11-07)
    "NVDA-USD": "xyz:NVDA", "TSLA-USD": "xyz:TSLA", "AMD-USD": "xyz:AMD",
    "MSFT-USD": "xyz:MSFT", "META-USD": "xyz:META", "AAPL-USD": "xyz:AAPL",
    "AMZN-USD": "xyz:AMZN", "GOOGL-USD": "xyz:GOOGL", "COIN-USD": "xyz:COIN",
    "ORCL-USD": "xyz:ORCL", "TSM-USD": "xyz:TSM", "HOOD-USD": "xyz:HOOD",
    "MSTR-USD": "xyz:MSTR",
}
_SIDE_MAP = {"long": "buy", "buy": "buy", "short": "sell", "sell": "sell"}
REQUIRED_FIELDS = ("id", "symbol", "side", "entry_ref", "stop_price", "ts_ms")
DEFAULT_REMOTE_OUTBOX = "/home/dayodapper/ARIA/signals/prop_outbox.jsonl"


class SignalTap:
    def __init__(self, cursor_path: Path, ttl_s: float = 300.0,
                 dedup_cap: int = 2000):
        self.cursor_path = Path(cursor_path)
        self.ttl_ms = ttl_s * 1000.0
        self.dedup_cap = dedup_cap
        state = read_json(self.cursor_path, {})
        self.offset = int(state.get("offset", 0))
        self.seen: dict[str, float] = dict(state.get("seen", {}))

    def _save(self) -> None:
        if len(self.seen) > self.dedup_cap:
            keep = list(self.seen.items())[-self.dedup_cap // 2:]
            self.seen = dict(keep)
        atomic_write_json(self.cursor_path,
                          {"offset": self.offset, "seen": self.seen})

    def consume(self, size: int, chunk: str,
                now_ms: int | None = None) -> tuple[list[dict], list[dict]]:
        """Consume bytes read from self.offset. Returns (signals, events)."""
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        events: list[dict] = []
        signals: list[dict] = []

        if size < self.offset:
            events.append({"type": "tap_gap_reset",
                           "offset": self.offset, "size": size})
            self.offset = 0
            self._save()
            return signals, events  # caller refetches from 0 next poll

        last_nl = chunk.rfind("\n")
        complete = chunk[:last_nl + 1] if last_nl >= 0 else ""
        self.offset += len(complete.encode("utf-8"))

        for line in complete.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                events.append({"type": "tap_malformed", "line": line[:120]})
                continue
            if not isinstance(rec, dict) or any(k not in rec for k in REQUIRED_FIELDS):
                events.append({"type": "tap_malformed", "line": line[:120]})
                continue
            side = _SIDE_MAP.get(str(rec["side"]).lower())
            if side is None:
                events.append({"type": "tap_malformed", "line": line[:120]})
                continue
            if rec["symbol"] not in SYMBOL_MAP:
                events.append({"type": "tap_filtered",
                               "symbol": rec["symbol"], "id": rec["id"]})
                continue
            if now_ms - int(rec["ts_ms"]) > self.ttl_ms:
                events.append({"type": "tap_expired", "id": rec["id"]})
                continue
            if rec["id"] in self.seen:
                events.append({"type": "tap_duplicate", "id": rec["id"]})
                continue
            self.seen[rec["id"]] = now_ms
            signals.append({
                "id": rec["id"],
                "symbol": rec["symbol"],
                "asset": SYMBOL_MAP[rec["symbol"]],
                "side": side,
                "entry_ref": float(rec["entry_ref"]),
                "stop_price": float(rec["stop_price"]),
                "tp1_price": (float(rec["tp1_price"])
                              if rec.get("tp1_price") else None),
                "coherence": rec.get("coherence"),
                "ts_ms": int(rec["ts_ms"]),
            })

        if complete or events:
            self._save()
        return signals, events


def fetch_local(path: Path, offset: int) -> tuple[int, str] | None:
    """Read from offset. Returns (file_size, chunk) or None if missing."""
    p = Path(path)
    if not p.is_file():
        return None
    size = p.stat().st_size
    if size < offset:
        return (size, "")
    with open(p, "rb") as f:
        f.seek(offset)
        return (size, f.read().decode("utf-8", "replace"))


def fetch_ssh(host: str, zone: str, remote_path: str, offset: int,
              timeout: int = 20) -> tuple[int, str]:
    """Pull new bytes from the ARIA VM. One ssh round-trip per poll."""
    cmd = (f'f="{remote_path}"; s=$(stat -c %s "$f" 2>/dev/null || echo 0); '
           f'echo "PROP_SIZE:$s"; '
           f'if [ "$s" -gt {offset} ]; then tail -c +{offset + 1} "$f"; fi')
    out = subprocess.run(
        ["gcloud", "compute", "ssh", host, f"--zone={zone}", f"--command={cmd}"],
        capture_output=True, text=True, timeout=timeout)
    if out.returncode != 0:
        raise RuntimeError(f"ssh pull failed: {out.stderr.strip()[:200]}")
    first, _, rest = out.stdout.partition("\n")
    if not first.startswith("PROP_SIZE:"):
        raise RuntimeError(f"bad transport header: {first[:80]}")
    return (int(first.split(":", 1)[1]), rest)
