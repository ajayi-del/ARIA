#!/usr/bin/env python3
"""
signals/anthropic_trader.py — Open Interpreter + Anthropic signal executor.

Runs SEPARATELY from ARIA. Watches aria_outbox.json, sends each signal
to Anthropic Claude for trade-decision review, and prints the approved
action for manual or GUI execution.

Usage:
    export ANTHROPIC_API_KEY="sk-ant-..."
    python signals/anthropic_trader.py
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

# ── Config ───────────────────────────────────────────────────────────────────
OUTBOX = Path(__file__).parent / "aria_outbox.json"
PROCESSED_LOG = Path(__file__).parent / ".processed_ids"
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
POLL_INTERVAL_S = 2.0

# ── Anthropic client ─────────────────────────────────────────────────────────
try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None


def _load_processed() -> set[str]:
    if PROCESSED_LOG.exists():
        return set(PROCESSED_LOG.read_text().strip().splitlines())
    return set()


def _save_processed(ids: set[str]) -> None:
    PROCESSED_LOG.write_text("\n".join(sorted(ids)) + "\n")


def _build_prompt(signal: dict) -> str:
    """Craft a trading prompt that Claude can reason about."""
    return f"""You are a disciplined quantitative trader reviewing a signal from ARIA, an autonomous trading system.

SIGNAL:
- Symbol: {signal.get('symbol', 'UNKNOWN')}
- Direction: {signal.get('direction', 'UNKNOWN')}
- Size: {signal.get('size', 0)}
- Leverage: {signal.get('leverage', 'default')}
- Notional USD: {signal.get('notional_usd', 'unknown')}
- Stop Price: {signal.get('stop_price', 'none')}
- TP Price: {signal.get('tp_price', 'none')}
- Source: {signal.get('source', 'aria')}
- Timestamp: {signal.get('timestamp', 0)}

ACCOUNT CONTEXT:
- Balance: ~$138 (small account — fees and slippage matter)
- Venue: Tria (no API — manual/GUI execution only)
- Strategy: 30-min time stop, 5x leverage cap

YOUR TASK:
Decide whether to execute this signal. Be ruthlessly selective.

RULES:
1. Reject if notional < $80 (fees eat edge)
2. Reject if direction conflicts with obvious trend (don't short into a rip)
3. Reject if symbol is illiquid or has wide spreads
4. Approve ONLY if edge clearly exceeds fee + slippage + noise
5. If approving, state exact size, direction, and a 1-sentence rationale

OUTPUT FORMAT (strict):
DECISION: APPROVE | REJECT
SIZE: <exact size if approve, else 0>
SYMBOL: <symbol>
DIRECTION: <LONG | SHORT>
RATIONALE: <1 sentence>
"""


def _call_claude(prompt: str) -> str:
    if Anthropic is None:
        print("[ERROR] anthropic SDK not installed: pip install anthropic")
        sys.exit(1)
    if not ANTHROPIC_API_KEY:
        print("[ERROR] ANTHROPIC_API_KEY not set")
        sys.exit(1)

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        temperature=0.0,
        system="You are a disciplined quant trader. Be concise. Every word costs latency.",
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text if resp.content else ""


def _parse_decision(text: str) -> dict:
    out = {"decision": "REJECT", "size": 0, "symbol": "", "direction": "", "rationale": ""}
    for line in text.strip().splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            key = key.strip().lower()
            val = val.strip()
            if key in out:
                out[key] = val
    return out


def main() -> None:
    print("[anthropic_trader] Starting signal watcher...")
    print(f"[anthropic_trader] Watching: {OUTBOX}")
    print(f"[anthropic_trader] Anthropic key: {'set' if ANTHROPIC_API_KEY else 'MISSING'}")

    processed = _load_processed()

    while True:
        if not OUTBOX.exists():
            time.sleep(POLL_INTERVAL_S)
            continue

        try:
            data = json.loads(OUTBOX.read_text())
        except (json.JSONDecodeError, OSError):
            time.sleep(POLL_INTERVAL_S)
            continue

        signals = data if isinstance(data, list) else [data]
        for sig in signals:
            sig_id = sig.get("id") or f"{sig.get('symbol')}_{sig.get('timestamp')}"
            if sig_id in processed:
                continue

            print(f"\n[NEW SIGNAL] {sig.get('symbol')} {sig.get('direction')} size={sig.get('size')}")
            prompt = _build_prompt(sig)
            try:
                raw = _call_claude(prompt)
            except Exception as exc:
                print(f"[ERROR] Claude call failed: {exc}")
                continue

            decision = _parse_decision(raw)
            print(f"[DECISION] {decision['decision']}")
            print(f"[RATIONALE] {decision['rationale']}")

            if decision["decision"].upper() == "APPROVE":
                print(f"\n>>> EXECUTE ON TRIA: {decision['direction']} {decision['size']} {decision['symbol']}")
                # TODO: wire to tria_bridge executor here

            processed.add(sig_id)
            _save_processed(processed)

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
