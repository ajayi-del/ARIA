#!/usr/bin/env python3
"""extended_collector.py — observer-class data collector for the Extended
perp DEX (extended.exchange), Phase 1 of the multi-venue harvest
architecture. A SENSOR, not a trader: public unauthenticated market data
only, zero execution surface, zero repo imports, safe under cron.

Each invocation fetches Extended's full market list (mark/index prices,
funding rate, open interest, 24h stats in ONE call), normalizes symbols to
ARIA's SYMBOL-USD convention, joins against SoDEX public marks for the
basis view, and appends ONE JSON LINE PER SYMBOL to
logs/extended_snapshots.jsonl (append-only, venue_snapshots.jsonl
convention). Stdout: one dense JSON summary line + a compact basis table.

ENDPOINTS (public, no auth — probe-verified schema pinned in
tests/test_extended_collector.py):
  GET https://api.starknet.extended.exchange/api/v1/info/markets
    -> {"status":"OK","data":[{name, assetName, active, marketStats:{
         markPrice, indexPrice, fundingRate, nextFundingRate, openInterest,
         lastPrice, askPrice, bidPrice, dailyVolume, dailyVolumeBase,
         dailyVolumeQuote, dailyLow, dailyHigh,
         dailyPriceChangePercentage}, ...}]}
  GET https://mainnet-gw.sodex.dev/api/v1/perps/markets/mark-prices
    -> {"data":[{"symbol":"BTC-USD","markPrice":"...","fundingRate":"..."}]}
    (the same public endpoint tools/funding_dispersion_collector.py uses)

FAIL-CLOSED doctrine (per Governor spec, stricter than the best-effort
collectors): a bad Extended payload, HTTP error, or timeout = NO WRITE, one
error line to stdout, exit 1. The SoDEX basis leg degrades independently —
a SoDEX failure leaves sodex_mark/basis_bps null but never blocks the
Extended snapshot (Extended is the primary plane; basis is a join).

Symbol naming: Extended uses BTC-USD-style names already; ALIASES maps its
TradFi tickers onto ARIA's (XAU->XAUT gold, SPX500->US500, BRENT->CL).
raw_symbol is always preserved on the record.

Flags: --once (default), --no-sodex (skip the basis join).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

EXTENDED_BASE = "https://api.starknet.extended.exchange/api/v1"
EXTENDED_MARKETS_URL = f"{EXTENDED_BASE}/info/markets"
SODEX_MARKS_URL = "https://mainnet-gw.sodex.dev/api/v1/perps/markets/mark-prices"

REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = REPO_ROOT / "logs" / "extended_snapshots.jsonl"

TIMEOUT_S = 20.0
VENUE = "extended"

# Extended base-asset -> ARIA base-asset aliases, FACT-BASED against the
# live 395-market list (probe 2026-10-04): oil = WTI-USD + XBR-USD (Brent —
# SoDEX CL is NYMEX WTI, so XBR deliberately does NOT join), gold = XAU-USD
# (physical) + XAUT-USD (token) + PAXG-USD (XAU is NOT aliased to XAUT —
# the alias collided with the native XAUT-USD market on first live run),
# silver XAG, copper XCU, S&P = SPX-USD + SPX500m-USD (two legs, mapping to
# SoDEX US500/SPCX ambiguous — recorded raw, consumer decides), Nasdaq =
# TECH100m-USD (1:1 with SoDEX USTECH100, inferred).
ALIASES = {
    "WTI": "CL",            # NYMEX CL = WTI crude (XBR Brent stays raw)
    "XAG": "SILVER",
    "XCU": "COPPER",
    "TECH100M": "USTECH100",  # base upper-cased before alias lookup
}

# Extended 24/5-session equity perps carry a "_24_5" suffix (AMD_24_5-USD);
# SoDEX lists the same underlyings plain (AMD-USD). The suffix is a session
# variant, not a different asset — strip it so the cross-venue join works.
SESSION_SUFFIX = "_24_5"


# ── pure helpers (no network — the test surface) ────────────────────────────


def normalize_symbol(raw: str) -> tuple[str, str]:
    """Extended market name -> (aria_base, aria_symbol SYMBOL-USD).
    'BTC-USD' -> ('BTC', 'BTC-USD'); 'AMD_24_5-USD' -> ('AMD', 'AMD-USD');
    'WTI-USD' -> ('CL', 'CL-USD')."""
    base = raw.split("-", 1)[0].strip().upper()
    if base.endswith(SESSION_SUFFIX.upper()):
        base = base[: -len(SESSION_SUFFIX)]
    aria_base = ALIASES.get(base, base)
    return aria_base, f"{aria_base}-USD"


def _f(row: dict, key: str):
    """float-or-None field read; never raises."""
    try:
        v = row.get(key)
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def parse_market(entry: dict, ts: float) -> dict | None:
    """One Extended market entry -> one snapshot record, or None to skip.
    Skips inactive markets and entries missing the critical trio
    (name, markPrice, fundingRate) — a partial record is a lie with fields
    missing; skip the symbol, never the run."""
    try:
        if not isinstance(entry, dict):
            return None
        # Fail-closed on tradability: the `active` flag is UNRELIABLE
        # (live probe 2026-10-04: 54 DELISTED + 15 PRELISTED markets carry
        # active=true, and DELISTED markets serve frozen marks — TON-USD
        # showed a -283bps phantom basis against SoDEX). Only status
        # == "ACTIVE" is a live market; REDUCE_ONLY is not collectable
        # evidence for a new-venue sensor either.
        if entry.get("active") is False:
            return None
        if entry.get("status", "ACTIVE") != "ACTIVE":
            return None
        raw = entry.get("name")
        stats = entry.get("marketStats")
        if not raw or not isinstance(stats, dict):
            return None
        mark = _f(stats, "markPrice")
        funding = _f(stats, "fundingRate")
        if mark is None or funding is None or mark <= 0:
            return None
        aria_base, aria_symbol = normalize_symbol(raw)
        next_fr = stats.get("nextFundingRate")
        return {
            "ts": ts,
            "venue": VENUE,
            "symbol": aria_symbol,
            "base": aria_base,
            "raw_symbol": raw,
            "mark": mark,
            "index": _f(stats, "indexPrice"),
            "funding_rate": funding,
            "next_funding_ms": (int(next_fr) if isinstance(next_fr, (int, float))
                                else None),
            "open_interest": _f(stats, "openInterest"),
            "last": _f(stats, "lastPrice"),
            "bid": _f(stats, "bidPrice"),
            "ask": _f(stats, "askPrice"),
            "vol_24h_quote": _f(stats, "dailyVolumeQuote") or _f(stats, "dailyVolume"),
            "high_24h": _f(stats, "dailyHigh"),
            "low_24h": _f(stats, "dailyLow"),
            "chg_24h_pct": _f(stats, "dailyPriceChangePercentage"),
            "sodex_mark": None,
            "basis_bps": None,
        }
    except Exception:
        return None


def parse_markets_payload(payload, ts: float) -> tuple[list[dict], int]:
    """Full Extended payload -> (records, skipped_count).
    Raises ValueError on a structurally bad payload (fail-closed: the caller
    writes NOTHING)."""
    if not isinstance(payload, dict):
        raise ValueError(f"payload is {type(payload).__name__}, not dict")
    if payload.get("status") not in (None, "OK"):
        raise ValueError(f"status={payload.get('status')!r}")
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ValueError("data missing/empty")
    records, skipped = [], 0
    for entry in data:
        rec = parse_market(entry, ts)
        if rec is None:
            skipped += 1
        else:
            records.append(rec)
    if not records:
        raise ValueError(f"0 of {len(data)} markets parseable")
    # Dedup normalized symbols (e.g. a plain market + its _24_5 session
    # variant both normalizing to AMD-USD): identity-named market wins,
    # else the deeper book (open_interest). Losers count as skipped —
    # a silent last-writer-wins overwrite is a data-quality lie.
    by_symbol: dict[str, dict] = {}
    for rec in records:
        prev = by_symbol.get(rec["symbol"])
        if prev is None:
            by_symbol[rec["symbol"]] = rec
            continue
        skipped += 1
        prev_identity = prev["raw_symbol"].upper() == prev["symbol"]
        rec_identity = rec["raw_symbol"].upper() == rec["symbol"]
        if rec_identity and not prev_identity:
            by_symbol[rec["symbol"]] = rec
        elif rec_identity == prev_identity and (
                (rec.get("open_interest") or 0)
                > (prev.get("open_interest") or 0)):
            by_symbol[rec["symbol"]] = rec
    return list(by_symbol.values()), skipped


def parse_sodex_marks(payload) -> dict:
    """SoDEX mark-prices payload -> {aria_base: mark}. Tolerant per row."""
    out = {}
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    for row in rows:
        try:
            sym = row["symbol"]
            base = sym[:-4] if sym.endswith("-USD") else sym
            out[base] = float(row["markPrice"])
        except (KeyError, TypeError, ValueError):
            continue
    return out


def join_basis(records: list[dict], sodex_marks: dict) -> int:
    """In-place join: sodex_mark + basis_bps (Extended mark vs SoDEX mark).
    basis_bps = (ext - sodex) / sodex * 1e4; positive = Extended rich.
    Returns the overlap count."""
    n = 0
    for rec in records:
        sm = sodex_marks.get(rec["base"])
        if sm and sm > 0:
            rec["sodex_mark"] = sm
            rec["basis_bps"] = round((rec["mark"] - sm) / sm * 1e4, 2)
            n += 1
    return n


def append_records(path: Path, records: list[dict]) -> int:
    """Append-only JSONL, venue_snapshots convention. Dense separators."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return len(records)


def basis_table(records: list[dict]) -> str:
    """Compact basis table for stdout, biggest |basis| first."""
    joined = [r for r in records if r.get("basis_bps") is not None]
    joined.sort(key=lambda r: -abs(r["basis_bps"]))
    lines = ["BASIS extended-vs-sodex (bps, + = extended rich):"]
    for r in joined:
        lines.append(f"  {r['symbol']:<14} ext={r['mark']:<14.6g} "
                     f"sodex={r['sodex_mark']:<14.6g} {r['basis_bps']:+.2f}")
    if not joined:
        lines.append("  (no overlap)")
    return "\n".join(lines)


# ── network (only touched in live runs, never in tests) ─────────────────────


async def _fetch_json(client, url: str) -> dict:
    resp = await client.get(url, timeout=TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()


async def run_once(out_path: Path = OUT_PATH, with_sodex: bool = True,
                   fixture_path: Path | None = None) -> dict:
    """One collection tick. Fail-closed on the Extended leg: any fetch or
    parse failure raises before a single byte is written. SoDEX leg is
    independent — its failure degrades basis to null, never blocks.
    fixture_path: dump the verbatim Extended payload there (recorded
    fixtures for tests — the payload is never mutated on this path)."""
    import httpx  # local import: tool is importable without httpx installed

    ts = time.time()
    summary = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
               "event": "extended_snapshot", "venue": VENUE}
    async with httpx.AsyncClient() as client:
        try:
            payload = await _fetch_json(client, EXTENDED_MARKETS_URL)
            records, skipped = parse_markets_payload(payload, ts)
        except Exception as exc:
            summary.update({"ok": False, "written": 0,
                            "error": str(exc)[:160]})
            print(json.dumps(summary, separators=(",", ":")), flush=True)
            raise SystemExit(1)
        if fixture_path is not None:
            try:
                fixture_path.parent.mkdir(parents=True, exist_ok=True)
                with open(fixture_path, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
            except Exception as exc:
                summary["fixture_error"] = str(exc)[:120]
        sodex_marks = {}
        if with_sodex:
            try:
                sodex_marks = parse_sodex_marks(
                    await _fetch_json(client, SODEX_MARKS_URL))
            except Exception as exc:
                summary["sodex_error"] = str(exc)[:120]
    overlap = join_basis(records, sodex_marks)
    written = append_records(out_path, records)
    summary.update({"ok": True, "markets": len(records) + skipped,
                    "written": written, "skipped": skipped,
                    "basis_overlap": overlap,
                    "sodex_n": len(sodex_marks)})
    top = sorted((r for r in records if r.get("basis_bps") is not None),
                 key=lambda r: -abs(r["basis_bps"]))[:3]
    summary["top_basis"] = [{"symbol": r["symbol"], "bps": r["basis_bps"]}
                            for r in top]
    print(json.dumps(summary, separators=(",", ":")), flush=True)
    print(basis_table(records), flush=True)
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Extended venue market-data collector (observer class)")
    parser.add_argument("--once", action="store_true", default=True,
                        help="one cron tick (default)")
    parser.add_argument("--no-sodex", action="store_true",
                        help="skip the SoDEX basis join")
    parser.add_argument("--save-fixture", metavar="PATH", default=None,
                        help="dump the verbatim Extended payload to PATH "
                             "(recorded test fixtures)")
    args = parser.parse_args(argv)
    try:
        summary = asyncio.run(run_once(
            with_sodex=not args.no_sodex,
            fixture_path=(Path(args.save_fixture)
                          if args.save_fixture else None)))
    except SystemExit as se:
        return int(se.code or 0)
    except Exception as exc:  # belt-and-braces: cron must always get a line
        print(json.dumps({"event": "extended_snapshot", "ok": False,
                          "error": str(exc)[:160]}, separators=(",", ":")),
              flush=True)
        return 1
    return 0 if summary.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
