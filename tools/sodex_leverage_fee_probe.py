#!/usr/bin/env python3
"""sodex_leverage_fee_probe.py — READ-ONLY reconciliation probe (rule 9:
the exchange API is the source of truth).

Two open reconciliations block the campaign launch:

  1. FEE RATES — a doctrine paste quotes maker 0.02% / taker 0.05%, but the
     verified engine defaults (intelligence/fast_cycle_engine.py,
     fast_cycle_maker_fee_rate / fast_cycle_taker_fee_rate) are maker
     0.0114% / taker 0.038% (measured + SOSO_STAKED 5% discount). This probe
     reads the account's ACTUAL fee schedule from SoDEX.

  2. PER-SYMBOL MAX LEVERAGE — the campaign engine's LEVERAGE_CAPS matrix
     assumes per-symbol caps (BTC 38, ETH/XAUT/USTECH100 25, SOL/SILVER/
     US500/CL/COPPER 20, mid-cap tier 10). This probe reads the exchange's
     ACTUAL per-symbol leverage limits.

Auth doctrine (SoDEX): GET endpoints take the wallet address in the URL
path, NO X-API-Key. POST/DELETE are forbidden here — this probe issues
GETs only. Base host: mainnet-gw.sodex.dev (testnet-gw.sodex.dev with
--testnet); response envelope is {"code": 0, "data": ...} — data may nest
({"data": {"positions": [...]}}).

Best-effort doctrine: exit 0 ALWAYS; per-endpoint failures print and
continue ("endpoint not found" per item, never crash). Stdlib + httpx only.

CLI:
  .venv/bin/python tools/sodex_leverage_fee_probe.py --wallet 0x...
  (wallet from argv, else SODEX_WALLET env)

Coordinator note: build-only locally; run on the server against the live
account.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

# ── Repo universe imports (stdlib-only modules; degrade to literals) ─────
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

try:
    from intelligence.fast_cycle_engine import LEVERAGE_CAPS as _CAPS
except Exception:  # import context differs on server — literal fallback
    _CAPS = {
        "BTC": 38, "ETH": 25, "XAUT": 25, "USTECH100": 25,
        "SOL": 20, "SILVER": 20, "US500": 20, "CL": 20, "COPPER": 20,
        "XRP": 10, "DOGE": 10, "LINK": 10, "ARB": 10, "BCH": 10,
        "NEAR": 10, "ZEC": 10,
    }

try:
    from intelligence.market_families import FAMILIES_V2 as _FAMS
except Exception:
    _FAMS = {}

# ── Reconciliation constants ─────────────────────────────────────────────
PASTE_MAKER, PASTE_TAKER = 0.0002, 0.0005      # doctrine paste: 0.02% / 0.05%
ENGINE_MAKER, ENGINE_TAKER = 0.000114, 0.00038  # engine defaults: 0.0114% / 0.038%

MAINNET_BASE = "https://mainnet-gw.sodex.dev/api/v1/perps"
TESTNET_BASE = "https://testnet-gw.sodex.dev/api/v1/perps"

# Field names the wire spec (go-sdk) or sibling venues use for leverage
# limits inside symbol-spec dicts — searched case-insensitively.
_LEVERAGE_KEY_HINTS = (
    "maxleverage", "max_leverage", "maxinitialleverage",
    "max_initial_leverage", "leveragemax", "maxleverageallowed",
)


def _base_symbol(sym: str) -> str:
    """BTC-USD / btc / BTC-USD.P → BTC (mirrors fast_cycle_engine)."""
    return str(sym).split("-")[0].split(".")[0].upper()


def campaign_universe() -> list:
    """LEVERAGE_CAPS keys plus every FAMILIES_V2 member/proxy member."""
    uni = set(_CAPS)
    for spec in (_FAMS or {}).values():
        for m in spec.get("members") or []:
            uni.add(_base_symbol(m))
        for m in (spec.get("proxy_members") or {}):
            uni.add(_base_symbol(m))
    return sorted(uni)


def _get(client: httpx.Client, url: str, params=None):
    """One GET, never raises. Returns (ok, payload, note)."""
    try:
        resp = client.get(url, params=params, timeout=10.0)
    except Exception as exc:
        return False, None, f"request failed: {type(exc).__name__}: {exc}"
    if resp.status_code == 404:
        return False, None, "endpoint not found (404)"
    if resp.status_code != 200:
        return False, None, f"HTTP {resp.status_code}: {resp.text[:160]}"
    try:
        payload = resp.json()
    except Exception:
        return False, None, "non-JSON response body"
    if isinstance(payload, dict) and payload.get("code") not in (None, 0):
        return False, payload, f"API code={payload.get('code')} msg={payload.get('msg')}"
    return True, payload, ""


def _unwrap(payload):
    """SoDEX envelope: {"code":0,"data":...} — data may nest one level."""
    if not isinstance(payload, dict):
        return payload
    data = payload.get("data", payload)
    return data


def _extract_max_leverage(spec: dict):
    """Pull a max-leverage value out of a symbol-spec dict, if present."""
    best = None
    for key, val in spec.items():
        kl = str(key).lower()
        if kl in _LEVERAGE_KEY_HINTS or ("leverage" in kl and "min" not in kl):
            try:
                v = float(val)
                if v > 1 and (best is None or v > best):
                    best = v
            except (TypeError, ValueError):
                continue
        if kl in ("leveragebrackets", "leverage_brackets", "brackets") and isinstance(val, list):
            for b in val:
                if isinstance(b, dict):
                    v = _extract_max_leverage(b)
                    if v and (best is None or v > best):
                        best = v
    return best


def probe_fee_rate(client, base, wallet, symbols):
    print("=" * 72)
    print("FEE SCHEDULE  (GET /api/v1/perps/accounts/{wallet}/fee-rate)")
    print("=" * 72)
    url = f"{base}/accounts/{wallet}/fee-rate"
    ok, payload, note = _get(client, url)
    if not ok:
        print(f"  account fee-rate: endpoint not found / failed — {note}")
        return
    d = _unwrap(payload)
    if not isinstance(d, dict):
        print(f"  account fee-rate: unexpected payload shape: {str(d)[:160]}")
        return
    maker = d.get("makerFeeRate")
    taker = d.get("takerFeeRate")
    tier = d.get("tier", d.get("feeTier"))
    staking = d.get("stakingTier")
    print(f"  makerFeeRate : {maker}")
    print(f"  takerFeeRate : {taker}")
    print(f"  tier         : {tier}")
    print(f"  stakingTier  : {staking}   (SOSO_STAKED 5% discount field)")
    try:
        m, t = float(maker), float(taker)
    except (TypeError, ValueError):
        print("  VERDICT: could not parse live rates — reconciliation UNRESOLVED")
        return
    print(f"  live         : maker {m*100:.4f}% / taker {t*100:.4f}%")
    print(f"  paste        : maker {PASTE_MAKER*100:.4f}% / taker {PASTE_TAKER*100:.4f}%")
    print(f"  engine       : maker {ENGINE_MAKER*100:.4f}% / taker {ENGINE_TAKER*100:.4f}%")
    eng_hit = abs(m - ENGINE_MAKER) < 1e-6 and abs(t - ENGINE_TAKER) < 1e-6
    paste_hit = abs(m - PASTE_MAKER) < 1e-6 and abs(t - PASTE_TAKER) < 1e-6
    if eng_hit:
        print("  VERDICT: live == ENGINE (0.0114%/0.038%) — paste 0.02/0.05 REFUTED")
    elif paste_hit:
        print("  VERDICT: live == PASTE (0.02%/0.05%) — engine defaults WRONG, update"
              " fast_cycle_maker_fee_rate / fast_cycle_taker_fee_rate")
    else:
        print("  VERDICT: live matches NEITHER — engine defaults stale; use the"
              " live numbers above as the reconciliation truth")
    # Per-symbol fee-rate (symbol-level discounts) — campaign universe only.
    print()
    print("  per-symbol fee-rate spot check (universe):")
    for base_sym in symbols:
        sym = f"{base_sym}-USD"
        ok2, p2, n2 = _get(client, url, params={"symbol": sym})
        if not ok2:
            print(f"    {sym:<14} not found / failed — {n2}")
            continue
        d2 = _unwrap(p2)
        if not isinstance(d2, dict):
            continue
        m2, t2 = d2.get("makerFeeRate"), d2.get("takerFeeRate")
        flag = ""
        try:
            if abs(float(m2) - m) > 1e-9 or abs(float(t2) - t) > 1e-9:
                flag = "  <-- DIVERGES from account-wide rate"
        except (TypeError, ValueError):
            pass
        print(f"    {sym:<14} maker {m2} / taker {t2}{flag}")


def probe_leverage(client, base, wallet, universe):
    print()
    print("=" * 72)
    print("PER-SYMBOL MAX LEVERAGE")
    print("=" * 72)

    # (b1) symbol-spec endpoint: GET /markets/symbols — inspect for
    # leverage fields on each market entry.
    url = f"{base}/markets/symbols"
    ok, payload, note = _get(client, url)
    exchange_max = {}
    if not ok:
        print(f"  /markets/symbols: endpoint not found / failed — {note}")
    else:
        markets = payload if isinstance(payload, list) else _unwrap(payload)
        if isinstance(markets, dict):
            markets = markets.get("symbols") or markets.get("markets") or []
        lev_fields_seen = set()
        for m in markets if isinstance(markets, list) else []:
            if not isinstance(m, dict):
                continue
            sym = m.get("symbol") or m.get("name") or ""
            b = _base_symbol(sym)
            v = _extract_max_leverage(m)
            if v is not None:
                exchange_max[b] = v
                lev_fields_seen.update(
                    k for k in m if "leverage" in str(k).lower())
        if not exchange_max:
            print("  /markets/symbols: responded, but NO leverage fields on any"
                  " symbol spec (max leverage not served here)")
        else:
            print(f"  /markets/symbols: leverage fields present: "
                  f"{sorted(lev_fields_seen)}")

    # (b2) candidate leverage-bracket endpoints — try, report not found.
    for path in ("/markets/leverage-brackets", "/markets/leverage-bracket",
                 "/leverage-brackets", "/markets/exchange-info",
                 "/markets/exchangeInfo"):
        ok2, p2, n2 = _get(client, f"{base}{path}")
        if not ok2:
            print(f"  {path}: endpoint not found / failed — {n2}")
            continue
        data = _unwrap(p2)
        items = data if isinstance(data, list) else (
            data.get("brackets") if isinstance(data, dict) else None) or []
        found = 0
        for it in items if isinstance(items, list) else []:
            if isinstance(it, dict):
                sym = it.get("symbol") or it.get("name") or ""
                v = _extract_max_leverage(it)
                if v is not None:
                    exchange_max.setdefault(_base_symbol(sym), v)
                    found += 1
        print(f"  {path}: OK — leverage data for {found} symbols")

    # (c) current leverage setting per symbol — account state + positions.
    print()
    print("  current leverage settings:")
    state_lev = {}
    ok3, p3, n3 = _get(client, f"{base}/accounts/{wallet}/state")
    if not ok3:
        print(f"    /accounts/{{wallet}}/state: not found / failed — {n3}")
    else:
        d = _unwrap(p3)
        for container in (d, d.get("leverages") if isinstance(d, dict) else None,
                          d.get("positions") if isinstance(d, dict) else None):
            if isinstance(container, dict):
                for k, v in container.items():
                    if "leverage" in str(k).lower():
                        state_lev[str(k)] = v
            elif isinstance(container, list):
                for it in container:
                    if isinstance(it, dict):
                        sym = it.get("symbol") or ""
                        for k, v in it.items():
                            if "leverage" in str(k).lower():
                                state_lev[_base_symbol(sym)] = v
        print(f"    /state leverage fields: "
              f"{json.dumps(state_lev) if state_lev else 'none present'}")

    ok4, p4, n4 = _get(client, f"{base}/accounts/{wallet}/positions")
    pos_lev = {}
    if not ok4:
        print(f"    /accounts/{{wallet}}/positions: not found / failed — {n4}")
    else:
        d = _unwrap(p4)
        raw = d.get("positions", d) if isinstance(d, dict) else d
        for it in raw if isinstance(raw, list) else []:
            if isinstance(it, dict):
                sym = it.get("symbol") or ""
                for k, v in it.items():
                    if "leverage" in str(k).lower():
                        pos_lev[_base_symbol(sym)] = v
        print(f"    open-position leverage fields: "
              f"{json.dumps(pos_lev) if pos_lev else 'none (flat book or no field)'}")

    # ── Reconciliation table ─────────────────────────────────────────────
    print()
    print("  {:<12} {:>22} {:>11}  {}".format(
        "symbol", "exchange_max_leverage", "engine_cap", "verdict"))
    print("  " + "-" * 60)
    mismatch = 0
    for sym in universe:
        cap = _CAPS.get(sym, "-")
        ex = exchange_max.get(sym)
        if ex is None:
            verdict = "UNKNOWN (not served)"
        elif cap == "-":
            verdict = "N/A (no engine cap)"
        elif int(ex) == int(cap):
            verdict = "MATCH"
        else:
            verdict = "MISMATCH"
            mismatch += 1
        ex_s = f"{ex:g}x" if isinstance(ex, float) else (str(ex) if ex else "-")
        print("  {:<12} {:>22} {:>11}  {}".format(sym, ex_s, cap, verdict))
    print()
    if mismatch:
        print(f"  LEVERAGE VERDICT: {mismatch} MISMATCH(es) — LEVERAGE_CAPS in"
              " intelligence/fast_cycle_engine.py must be reconciled to the"
              " exchange values above before launch")
    elif exchange_max:
        print("  LEVERAGE VERDICT: all engine caps MATCH the exchange limits")
    else:
        print("  LEVERAGE VERDICT: UNRESOLVED — exchange serves no max-leverage"
              " field on any probed GET; reconcile manually via the go-sdk"
              " wire spec or a leverage-bracket endpoint not yet discovered")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Read-only SoDEX fee-rate + per-symbol leverage"
                    " reconciliation probe (GETs only, wallet in URL,"
                    " no X-API-Key). Exit 0 always.")
    ap.add_argument("--wallet", default=os.environ.get("SODEX_WALLET", ""),
                    help="SoDEX wallet address (default: SODEX_WALLET env)")
    ap.add_argument("--testnet", action="store_true",
                    help="probe testnet-gw.sodex.dev instead of mainnet")
    args = ap.parse_args()

    base = TESTNET_BASE if args.testnet else MAINNET_BASE
    universe = campaign_universe()

    print("SoDEX leverage/fee probe — READ-ONLY (GETs only)")
    print(f"host    : {base}")
    print(f"wallet  : {args.wallet or '(none — leverage/market endpoints only)'}")
    print(f"universe: {len(universe)} symbols "
          f"(LEVERAGE_CAPS {len(_CAPS)} + FAMILIES_V2 members)")
    print()

    client = httpx.Client(http2=False)
    try:
        if not args.wallet:
            print("NOTE: no --wallet / SODEX_WALLET — account-scoped sections"
                  " (fee schedule, current leverage) will be skipped.")
        else:
            probe_fee_rate(client, base, args.wallet, universe)
        probe_leverage(client, base, args.wallet or "0x0", universe)
    except Exception as exc:  # best-effort doctrine: never crash
        print(f"\nPROBE ERROR (non-fatal): {type(exc).__name__}: {exc}")
    finally:
        client.close()
    print("\nprobe complete (best-effort; exit 0)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
