#!/usr/bin/env python3
"""Macro posture precompute for the watchdog's first daily cycle (2026-09-01,
operator directive "ultrathink and wire it", deterministic + cost-effective).

The user's weekly institutional-flow read (ETF flow BREADTH + persistence,
stablecoin net issuance, macro constraint) compared against ARIA's beliefs
and execution — precomputed deterministically so the watchdog spends tokens
on judgment, not arithmetic. Observer-class: zero trade-path wiring, zero
new gates. Any live use of these inputs is a separate proposal requiring
shadow evidence (López de Prado / Aronson: context is not signal until
measured).

Data planes (probe-verified 2026-09-01):
  1. ETF flows — the BOT's SoSoValue caches (BTC/ETH/SOL; zero API spend —
     the one-fetcher/many-readers doctrine, soso_snapshot owns top-up).
     XRP/HYPE are NOT covered on the demo plan (probed: code 400101), so
     breadth is computed over the tracked trio only and the output says so.
  2. Stablecoin liquidity — DefiLlama stablecoins API (free, no key,
     all-chain; carries circulatingPrevDay/Week/Month so 1d/7d/30d deltas
     come from ONE call). ValueChain (rpc.valuechain.xyz) is the SoDEX
     chain RPC — it cannot serve ETF flows or stablecoin supply; it stays
     the Tier-4 liquidation plane. One fetch per UTC day, date-disciplined
     cache; kill switch MACRO_POSTURE_DEFILLAMA_ENABLED=false = cache-only.
  3. Macro calendar — the bot's sosovalue_macro.json cache (zero spend).
  4. ARIA positioning — deterministic aria.log tail parse (positions from
     the latest pnl_attribution, today's entries, today's tide vetoes)
     crossed with the tide state per symbol = the deviation table the
     watchdog reads in one glance.

Output: logs/macro_posture.json (atomic) + one history line to
logs/macro_posture_history.jsonl + the full JSON on stdout.
Best-effort doctrine: every section self-errors, exit code always 0.

Usage: .venv/bin/python tools/macro_posture.py
"""
import json
import os
import re
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

_STABLE_7D_MATERIALITY = 5e8     # ±$500M net issuance/week = material backdrop
_DEFILLAMA_URL = "https://stablecoins.llama.fi/stablecoins"
_WOW_LOOKBACK_S = 6.5 * 86400    # "7d ago" = newest history line older than this
_LOG_TAIL_CHUNK = 4 * 1024 * 1024
_LOG_TAIL_MAX_CHUNKS = 12        # ≤48MB backward scan, bounded


def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _load_jsonl(path):
    rows = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue   # one-bad-line doctrine
    except Exception:
        pass
    return rows


def _atomic_write(path, payload):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, path)


# ── ETF flows + breadth ──────────────────────────────────────────────────────

def breadth_shares(flow_verdicts: dict) -> dict:
    """|sum_3d| weight per symbol over the tracked stack (sign carried in the
    per-symbol fields). The report's "alt share 2.7%→15.5%" pattern computed
    over what the plan covers (BTC/ETH/SOL)."""
    abs3 = {s: abs(float(v.get("sum_3d_usd") or 0.0))
            for s, v in flow_verdicts.items() if v.get("rows")}
    total = sum(abs3.values())
    if total <= 0:
        return {}
    return {s: round(w / total, 4) for s, w in abs3.items()}


def flow_section(log_dir: str, now: float) -> dict:
    from data.sosovalue_feed import (flow_verdict, tide_aligned,
                                     etf_calendar_adjusted_age)
    rows = _load(os.path.join(log_dir, "sosovalue_flows.json"), {})
    verdicts, ages = {}, []
    for sym, r in rows.items():
        v = flow_verdict(sym, r or [])
        if not v.get("rows"):
            continue
        age = 999.0
        try:
            import calendar as _cal
            d = str(v["last_date"])
            y, m, dd = int(d[:4]), int(d[5:7]), int(d[8:10])
            raw = max(0.0, (now - _cal.timegm((y, m, dd, 0, 0, 0, 0, 0, 0))) / 3600.0)
            age = etf_calendar_adjusted_age(d, raw, now)
        except Exception:
            pass
        ages.append(age)
        verdicts[sym] = {**v, "age_hours": round(age, 1),
                         "tide_long": tide_aligned(v, "long", age_hours=age),
                         "tide_short": tide_aligned(v, "short", age_hours=age)}
    shares_now = breadth_shares(verdicts)
    alt_now = shares_now.get("SOL")

    # Week-over-week breadth shift from the append-only history (zero spend).
    shares_then, alt_then = None, None
    hist = _load_jsonl(os.path.join(log_dir, "sosovalue_flows.jsonl"))
    prior = [h for h in hist
             if isinstance(h, dict) and float(h.get("ts") or 0) <= now - _WOW_LOOKBACK_S]
    if prior:
        shares_then = breadth_shares(prior[-1].get("flows") or {})
        alt_then = (shares_then or {}).get("SOL")

    return {"symbols": verdicts,
            "cache_age_hours": round(min(ages), 1) if ages else 999.0,
            "breadth_abs3d_now": shares_now,
            "sol_share_abs3d_now": alt_now,
            "sol_share_abs3d_7d_ago": alt_then,
            "coverage_note": ("BTC/ETH/SOL only — XRP/HYPE not covered on the "
                              "SoSoValue demo plan (probed code 400101, "
                              "2026-09-01); breadth is the tracked trio.")}


# ── Stablecoin liquidity (DefiLlama, one fetch/UTC-day) ─────────────────────

def _fetch_stablecoins():
    import httpx
    r = httpx.get(_DEFILLAMA_URL, params={"includePrices": "true"}, timeout=15.0)
    d = r.json()
    out = {}
    for a in d.get("peggedAssets", []):
        sym = a.get("symbol")
        if sym in ("USDT", "USDC"):
            def _peg(k):
                return float((a.get(k) or {}).get("peggedUSD") or 0.0)
            out[sym] = {"circulating": _peg("circulating"),
                        "prev_day": _peg("circulatingPrevDay"),
                        "prev_week": _peg("circulatingPrevWeek"),
                        "prev_month": _peg("circulatingPrevMonth")}
    return out


def stablecoin_verdict(delta_7d: float,
                       materiality: float = _STABLE_7D_MATERIALITY) -> str:
    if delta_7d >= materiality:
        return "expanding"
    if delta_7d <= -materiality:
        return "contracting"
    return "flat"


def stablecoin_section(log_dir: str, now: float) -> dict:
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    cache_path = os.path.join(log_dir, "stablecoin_liquidity.json")
    cache = _load(cache_path, {})
    data, origin, stale = None, "none", False
    if cache.get("date") == today and cache.get("data"):
        data, origin = cache["data"], "cache_hit"
    elif os.environ.get("MACRO_POSTURE_DEFILLAMA_ENABLED", "true").lower() != "false":
        try:
            data = _fetch_stablecoins()
            _atomic_write(cache_path, {"date": today, "fetched_ts": now, "data": data})
            origin = "fetched"
        except Exception as e:
            if cache.get("data"):
                data, origin, stale = cache["data"], "stale_cache", True
            else:
                return {"error": f"defillama_unavailable:{str(e)[:100]}"}
    elif cache.get("data"):
        data, origin, stale = cache["data"], "stale_cache_kill_switch", True
    if not data:
        return {"error": "no_data"}
    usdt = data.get("USDT") or {}
    usdc = data.get("USDC") or {}
    tot = usdt.get("circulating", 0.0) + usdc.get("circulating", 0.0)
    d1 = (usdt.get("circulating", 0.0) - usdt.get("prev_day", 0.0)
          + usdc.get("circulating", 0.0) - usdc.get("prev_day", 0.0))
    d7 = (usdt.get("circulating", 0.0) - usdt.get("prev_week", 0.0)
          + usdc.get("circulating", 0.0) - usdc.get("prev_week", 0.0))
    d30 = (usdt.get("circulating", 0.0) - usdt.get("prev_month", 0.0)
           + usdc.get("circulating", 0.0) - usdc.get("prev_month", 0.0))
    return {"asof_date": today, "source": "defillama", "fetch": origin,
            "stale": stale, "usdt_circ": round(usdt.get("circulating", 0.0), 0),
            "usdc_circ": round(usdc.get("circulating", 0.0), 0),
            "total_usdt_usdc": round(tot, 0),
            "delta_1d": round(d1, 0), "delta_7d": round(d7, 0),
            "delta_30d": round(d30, 0),
            "materiality_7d": _STABLE_7D_MATERIALITY,
            "verdict": stablecoin_verdict(d7)}


# ── Macro calendar (bot cache) ───────────────────────────────────────────────

def macro_section(log_dir: str, now: float) -> dict:
    from data.sosovalue_feed import macro_due_today
    macro = _load(os.path.join(log_dir, "sosovalue_macro.json"), [])
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    return {"events_today": macro_due_today(macro, today),
            "calendar_head": macro[:8]}


# ── ARIA positioning (deterministic log tail parse) ─────────────────────────

_BREAKDOWN_RE = re.compile(r"([A-Z0-9]+-USD):([LS])@")


def _tail_lines_today(path: str, today: str) -> list:
    """Lines from today (UTC), scanning backwards in bounded chunks."""
    out = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            pos, buf = size, b""
            for _ in range(_LOG_TAIL_MAX_CHUNKS):
                if pos <= 0:
                    break
                start = max(0, pos - _LOG_TAIL_CHUNK)
                f.seek(start)
                buf = f.read(pos - start) + buf
                pos = start
                lines = buf.split(b"\n")
                if len(lines) > 1:
                    # Stop when the oldest full line predates today.
                    head = lines[1].decode("utf-8", "replace")
                    if f'"{today}T' not in head and '"timestamp"' in head \
                            and f'T"{today[5:]}"' not in head:
                        try:
                            ts = head.rsplit('"timestamp": "', 1)[1][:10]
                            if ts < today:
                                break
                        except Exception:
                            pass
                buf = buf
        out = buf.decode("utf-8", "replace").splitlines()
        first_today = next((i for i, l in enumerate(out) if f'"{today}T' in l), 0)
        return out[first_today:]
    except Exception:
        return out


def positioning_section(log_dir: str, now: float, flows: dict) -> dict:
    today = time.strftime("%Y-%m-%d", time.gmtime(now))
    lines = _tail_lines_today(os.path.join(log_dir, "aria.log"), today)
    positions, entries, vetoes = [], [], 0
    last_attr = None
    for line in lines:
        if '"event"' not in line:
            continue
        if '"pnl_attribution"' in line:
            last_attr = line
        elif '"signal_rejected_etf_tide"' in line:
            vetoes += 1
        elif '"execution_decision"' in line or '"fastpath_entry_journaled"' in line:
            try:
                d = json.loads(line[line.index("{"):])
                sym, ev = d.get("symbol"), d.get("event")
                side = d.get("direction") or d.get("side")
                approved = d.get("decision") in (None, "approved", "APPROVED")
                if sym and side and approved:
                    entries.append({"event": ev, "symbol": sym,
                                    "direction": str(side).lower()})
            except Exception:
                continue
    if last_attr:
        try:
            d = json.loads(last_attr[last_attr.index("{"):])
            for m in _BREAKDOWN_RE.finditer(d.get("breakdown") or ""):
                positions.append({"symbol": m.group(1),
                                  "direction": "long" if m.group(2) == "L" else "short"})
        except Exception:
            pass
    tide = flows.get("symbols") or {}

    def _tide_for(sym, direction):
        base = sym.split("-")[0]
        v = tide.get(base)
        if not v:
            return "no_data"
        return v.get("tide_long" if direction == "long" else "tide_short") or "neutral"

    deviations = [{"symbol": p["symbol"], "direction": p["direction"],
                   "tide": _tide_for(p["symbol"], p["direction"]), "kind": "open_position"}
                  for p in positions]
    deviations += [{"symbol": e["symbol"], "direction": e["direction"],
                    "tide": _tide_for(e["symbol"], e["direction"]),
                    "kind": e["event"]} for e in entries[-20:]]
    return {"asof": today, "open_positions": positions,
            "entries_today": len(entries), "tide_vetoes_today": vetoes,
            "deviation_table": deviations,
            "opposed_count": sum(1 for r in deviations if r["tide"] == "opposed")}


# ── Structure verdicts (deterministic facts, watchdog judges) ────────────────

def structure_verdicts(flows: dict, stable: dict) -> dict:
    syms = flows.get("symbols") or {}
    signs = {s: (1 if float(v.get("last_inflow_usd") or 0) > 0 else
                 (-1 if float(v.get("last_inflow_usd") or 0) < 0 else 0))
             for s, v in syms.items()}
    divergence = None
    if signs.get("BTC", 0) < 0 and signs.get("ETH", 0) > 0:
        divergence = ("btc_outflow_eth_inflow" +
                      ("_alts_too" if signs.get("SOL", 0) > 0 else ""))
    persistence = max((abs(int(v.get("streak_days") or 0)) for v in syms.values()),
                      default=0)
    flow_3d_total = sum(float(v.get("sum_3d_usd") or 0.0) for v in syms.values())
    sv = stable.get("verdict")
    mat = 150e6
    if sv == "expanding" and flow_3d_total > mat:
        cross = "supportive_expansion"
    elif sv == "expanding" and flow_3d_total < -mat:
        cross = "liquidity_vs_flow_divergence"
    elif sv == "contracting" and flow_3d_total > mat:
        cross = "flow_rally_on_shrinking_liquidity"
    else:
        cross = "neutral"
    return {"flow_signs_last_day": signs, "flow_divergence": divergence,
            "persistence_max_streak_days": persistence,
            "flow_3d_total_usd": round(flow_3d_total, 0),
            "liquidity_x_flows": cross}


def main() -> int:
    log_dir = os.path.join(_ROOT, "logs")
    now = time.time()
    out = {"ts": now, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
    try:
        out["etf_flows"] = flow_section(log_dir, now)
    except Exception as e:
        out["etf_flows"] = {"error": str(e)[:120]}
    try:
        out["stablecoin_liquidity"] = stablecoin_section(log_dir, now)
    except Exception as e:
        out["stablecoin_liquidity"] = {"error": str(e)[:120]}
    try:
        out["macro_calendar"] = macro_section(log_dir, now)
    except Exception as e:
        out["macro_calendar"] = {"error": str(e)[:120]}
    try:
        out["aria_positioning"] = positioning_section(
            log_dir, now, out.get("etf_flows") or {})
    except Exception as e:
        out["aria_positioning"] = {"error": str(e)[:120]}
    try:
        out["structure"] = structure_verdicts(
            out.get("etf_flows") or {}, out.get("stablecoin_liquidity") or {})
    except Exception as e:
        out["structure"] = {"error": str(e)[:120]}
    try:
        _atomic_write(os.path.join(log_dir, "macro_posture.json"), out)
        hist = {"ts": now, "structure": out.get("structure"),
                "stable_verdict": (out.get("stablecoin_liquidity") or {}).get("verdict"),
                "sol_share": (out.get("etf_flows") or {}).get("sol_share_abs3d_now"),
                "opposed_count": (out.get("aria_positioning") or {}).get("opposed_count")}
        with open(os.path.join(log_dir, "macro_posture_history.jsonl"), "a") as f:
            f.write(json.dumps(hist) + "\n")
    except Exception:
        pass
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
