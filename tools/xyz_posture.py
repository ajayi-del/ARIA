#!/usr/bin/env python3
"""xyz tradfi-complex posture tool (observer-class, 2026-11-08).

Standalone measurement plane for the Hyperliquid xyz dex (the prop
program's execution plane) + the Variational public stats plane. NEVER
touches the trade path, gates, or config — same doctrine as
tools/macro_posture.py: deterministic precompute, best-effort sections,
exit code always 0.

Data planes:
  1. HL xyz dex snapshot — POST https://api.hyperliquid.xyz/info with
     {"type":"metaAndAssetCtxs","dex":"xyz"} (the "dex" field is
     load-bearing; the default response omits xyz: assets — proven idiom
     in data/hyperliquid_feed.py). Per asset: markPx, funding,
     openInterest, dayNtlVlm, prevDayPx. ~143 assets.
  2. Variational public stats — GET
     https://api.variational.io/v1/metadata/v2/stats (public, probed
     working 2026-11-08): per-listing open-interest long/short splits.
     Rate limit 10 req/10s per IP; on HTTP 429 read retry_after_seconds,
     back off ONCE, retry once, then skip the plane for the run
     (fail-soft). Any non-200 skips the plane.
  3. Derived complexes — pure math on the HL snapshot: copper/gold
     ratio, DXY anchor + 24h change, complex breadth (% assets with
     markPx > prevDayPx), top-5 funding extremes by |rate|, metals
     complex table.

Verdicts (deterministic; thresholds are heuristic anchors, NOT gospel):
  - positioning_crowding: OI long share per asset from Variational;
    long_share > 0.65 = "crowded_long", < 0.35 = "crowded_short".
  - metals_risk_tone: copper/gold ratio vs its own rolling history kept
    in the state file (last 30 runs). <3 prior points =
    "insufficient_history"; ratio > median(hist) x 1.005 =
    "copper_leading" (growth/risk-on tone); < median / 1.005 =
    "gold_leading" (defensive tone); else "balanced".
  - dollar_tide: DXY 24h change > +0.3% = "strengthening" (headwind for
    metals/ags), < -0.3% = "weakening", else "flat".
  - breadth_regime: advancing share > 0.60 = "broad_risk_on",
    < 0.40 = "broad_risk_off", else "mixed".

Output: logs/xyz_posture.json (atomic tmp+replace) + one compact history
line to logs/xyz_posture_history.jsonl per run (density law: one-line
reproducible records, no prose). Total failure writes {"error": ...} and
still exits 0.

Usage: .venv/bin/python tools/xyz_posture.py
"""
import json
import os
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

HL_REST = "https://api.hyperliquid.xyz/info"
_VARIATIONAL_STATS_URL = "https://api.variational.io/v1/metadata/v2/stats"

# Verdict thresholds (documented heuristic anchors — see module docstring).
_CROWDED_LONG = 0.65
_CROWDED_SHORT = 0.35
_DXY_TIDE_PCT = 0.3          # 24h change % band for strengthening/weakening
_BREADTH_RISK_ON = 0.60
_BREADTH_RISK_OFF = 0.40
_RATIO_TONE_BAND = 1.005     # ±0.5% vs rolling median = directional tone
_RATIO_HISTORY_KEEP = 30
_RATIO_HISTORY_MIN = 3
_RETRY_BACKOFF_CAP_S = 12.0  # one bounded backoff on Variational 429

_METALS = ("GOLD", "SILVER", "PLATINUM", "PALLADIUM", "COPPER", "ALUMINIUM")


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


def _f(x):
    """Float coercion that never raises; None on junk."""
    try:
        v = float(x)
        return v if v == v else None   # NaN guard
    except (TypeError, ValueError):
        return None


# ── Pure verdicts (test-pinned) ──────────────────────────────────────────────

def crowding_verdict(long_share):
    """OI long share → crowding flag. None/no-data abstains."""
    if long_share is None:
        return None
    if long_share > _CROWDED_LONG:
        return "crowded_long"
    if long_share < _CROWDED_SHORT:
        return "crowded_short"
    return "neutral"


def dollar_tide(dxy_change_pct):
    """DXY 24h change % → dollar tide verdict."""
    if dxy_change_pct is None:
        return "no_data"
    if dxy_change_pct > _DXY_TIDE_PCT:
        return "strengthening"
    if dxy_change_pct < -_DXY_TIDE_PCT:
        return "weakening"
    return "flat"


def breadth_regime(advancing_share):
    """Share of xyz assets with markPx > prevDayPx → breadth verdict."""
    if advancing_share is None:
        return "no_data"
    if advancing_share > _BREADTH_RISK_ON:
        return "broad_risk_on"
    if advancing_share < _BREADTH_RISK_OFF:
        return "broad_risk_off"
    return "mixed"


def metals_risk_tone(ratio, history):
    """Copper/gold ratio vs its own rolling history (median anchor).
    <_RATIO_HISTORY_MIN prior points = insufficient_history."""
    if ratio is None:
        return "no_data"
    hist = [h for h in (history or []) if isinstance(h, (int, float))]
    if len(hist) < _RATIO_HISTORY_MIN:
        return "insufficient_history"
    s = sorted(hist)
    med = s[len(s) // 2] if len(s) % 2 else 0.5 * (s[len(s) // 2 - 1] + s[len(s) // 2])
    if med <= 0:
        return "insufficient_history"
    if ratio > med * _RATIO_TONE_BAND:
        return "copper_leading"
    if ratio < med / _RATIO_TONE_BAND:
        return "gold_leading"
    return "balanced"


def pct_change(now, prev):
    if now is None or prev is None or prev == 0:
        return None
    return (now - prev) / prev * 100.0


# ── Plane 1: HL xyz dex snapshot ─────────────────────────────────────────────

def parse_xyz_snapshot(payload):
    """metaAndAssetCtxs(dex=xyz) payload → {name: {mark, funding, oi,
    day_ntl_vlm, prev_day_px}}. Fail-closed: bad shape → {} (never raises)."""
    out = {}
    if not (isinstance(payload, list) and len(payload) == 2
            and isinstance(payload[0], dict) and isinstance(payload[1], list)):
        return out
    universe = payload[0].get("universe") or []
    ctxs = payload[1]
    for meta, ctx in zip(universe, ctxs):
        if not (isinstance(meta, dict) and isinstance(ctx, dict)):
            continue
        name = meta.get("name")
        if not name:
            continue
        out[name] = {"mark": _f(ctx.get("markPx")),
                     "funding": _f(ctx.get("funding")),
                     "oi": _f(ctx.get("openInterest")),
                     "day_ntl_vlm": _f(ctx.get("dayNtlVlm")),
                     "prev_day_px": _f(ctx.get("prevDayPx"))}
    return out


def fetch_xyz_snapshot(client=None):
    """POST the proven idiom (data/hyperliquid_feed.py): the "dex" field is
    load-bearing — xyz: assets do NOT appear in the default response."""
    import httpx
    if client is None:
        with httpx.Client(timeout=15.0) as c:
            return _fetch_xyz_snapshot(c)
    return _fetch_xyz_snapshot(client)


def _fetch_xyz_snapshot(client):
    resp = client.post(HL_REST, json={"type": "metaAndAssetCtxs", "dex": "xyz"})
    if resp.status_code != 200:
        raise RuntimeError(f"http_{resp.status_code}")
    snap = parse_xyz_snapshot(resp.json())
    if not snap:
        raise RuntimeError("empty_or_bad_shape")
    return snap


def derived_complexes(snap):
    """Pure math on the HL snapshot: ratio, DXY anchor, breadth, funding
    extremes, metals table. Missing legs abstain (None), never raise."""
    def _get(sym):
        return snap.get(f"xyz:{sym}") or {}

    copper, gold = _get("COPPER").get("mark"), _get("GOLD").get("mark")
    ratio = (copper / gold) if (copper and gold and gold > 0) else None

    dxy = _get("DXY")
    dxy_mark, dxy_prev = dxy.get("mark"), dxy.get("prev_day_px")
    dxy_chg = pct_change(dxy_mark, dxy_prev)

    adv, total = 0, 0
    for a in snap.values():
        m, p = a.get("mark"), a.get("prev_day_px")
        if m is None or p is None or p <= 0:
            continue
        total += 1
        if m > p:
            adv += 1
    adv_share = (adv / total) if total else None

    extremes = sorted(
        ((name, a["funding"]) for name, a in snap.items()
         if a.get("funding") is not None),
        key=lambda kv: abs(kv[1]), reverse=True)[:5]

    metals = {}
    for sym in _METALS:
        a = _get(sym)
        if not a or a.get("mark") is None:
            continue
        metals[sym] = {"mark": a.get("mark"),
                       "chg_24h_pct": _r2(pct_change(a.get("mark"),
                                                     a.get("prev_day_px"))),
                       "funding": a.get("funding"),
                       "oi": a.get("oi"),
                       "day_ntl_vlm": a.get("day_ntl_vlm")}

    return {"asset_count": len(snap),
            "copper_gold_ratio": _r6(ratio),
            "dxy": {"mark": dxy_mark, "chg_24h_pct": _r4(dxy_chg)},
            "breadth": {"advancing": adv, "total": total,
                        "advancing_share": _r4(adv_share)},
            "funding_extremes": [{"asset": n, "funding": f} for n, f in extremes],
            "metals": metals}


def _r2(x):
    return round(x, 2) if isinstance(x, float) else x


def _r4(x):
    return round(x, 4) if isinstance(x, float) else x


def _r6(x):
    return round(x, 6) if isinstance(x, float) else x


# ── Plane 2: Variational public stats (fail-soft) ────────────────────────────

def _normalize_variational_symbol(raw):
    if not isinstance(raw, str) or not raw:
        return None
    s = raw.upper()
    if s.startswith("XYZ:"):
        s = s[4:]
    for suf in ("-USD", "-USDC", "USD", "/USD"):
        if s.endswith(suf) and len(s) > len(suf):
            s = s[:-len(suf)]
            break
    return s or None


def parse_variational_stats(payload):
    """Defensive parser — the exact wire shape is not pinned in-repo, so
    accept list-of-dicts or a dict wrapping the list under a common key, and
    several plausible field spellings for the OI long/short split.
    Returns {BASE_SYMBOL: {"oi_long": f, "oi_short": f, "long_share": f|None}}.
    Never raises; unusable rows are skipped."""
    rows = payload
    if isinstance(payload, dict):
        rows = None
        for k in ("data", "stats", "results", "listings", "items"):
            if isinstance(payload.get(k), list):
                rows = payload[k]
                break
        if rows is None and all(isinstance(v, dict) for v in payload.values()):
            # dict keyed by symbol
            rows = [{**v, "symbol": k} for k, v in payload.items()]
    if not isinstance(rows, list):
        return {}
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        sym = _normalize_variational_symbol(
            row.get("symbol") or row.get("ticker") or row.get("asset")
            or row.get("name") or row.get("token"))
        if not sym:
            continue
        oi_long = oi_short = None
        nested = row.get("open_interest")
        if isinstance(nested, dict):
            oi_long = _f(nested.get("long"))
            oi_short = _f(nested.get("short"))
        if oi_long is None:
            for k in ("open_interest_long", "long_open_interest", "oi_long",
                      "long_oi", "openInterestLong"):
                if _f(row.get(k)) is not None:
                    oi_long = _f(row.get(k))
                    break
        if oi_short is None:
            for k in ("open_interest_short", "short_open_interest", "oi_short",
                      "short_oi", "openInterestShort"):
                if _f(row.get(k)) is not None:
                    oi_short = _f(row.get(k))
                    break
        share = None
        if oi_long is not None and oi_short is not None and (oi_long + oi_short) > 0:
            share = oi_long / (oi_long + oi_short)
        else:
            for k in ("long_share", "long_ratio", "pct_long", "long_pct"):
                v = _f(row.get(k))
                if v is not None:
                    share = v / 100.0 if v > 1.0 else v
                    break
        out[sym] = {"oi_long": oi_long, "oi_short": oi_short,
                    "long_share": _r4(share)}
    return out


def fetch_variational_stats(client=None, sleep_fn=time.sleep):
    """One bounded retry on 429 (read retry_after_seconds, back off once),
    then skip the plane for the run (fail-soft). Non-200 → skip. Raises
    RuntimeError with the reason on skip so the caller can self-error the
    section."""
    import httpx
    if client is None:
        with httpx.Client(timeout=15.0) as c:
            return _fetch_variational_stats(c, sleep_fn)
    return _fetch_variational_stats(client, sleep_fn)


def _fetch_variational_stats(client, sleep_fn):
    for attempt in (0, 1):
        resp = client.get(_VARIATIONAL_STATS_URL)
        if resp.status_code == 200:
            stats = parse_variational_stats(resp.json())
            if not stats:
                raise RuntimeError("empty_or_bad_shape")
            return stats
        if resp.status_code == 429 and attempt == 0:
            retry = None
            try:
                body = resp.json()
                if isinstance(body, dict):
                    retry = _f(body.get("retry_after_seconds"))
            except Exception:
                pass
            if retry is None:
                retry = _f(getattr(resp, "headers", {}).get("Retry-After"))
            sleep_fn(min(max(retry or 1.0, 0.0), _RETRY_BACKOFF_CAP_S))
            continue
        raise RuntimeError(f"http_{resp.status_code}")
    raise RuntimeError("http_429_after_retry")


def crowding_section(stats, snap):
    """Positioning crowding over assets present in BOTH planes (Variational
    keyed by base symbol; HL names are xyz:-prefixed)."""
    crowded_long, crowded_short, neutral_n = [], [], 0
    coverage = 0
    for name in snap:
        base = name[4:] if name.startswith("xyz:") else name
        v = stats.get(base)
        if not v:
            continue
        share = v.get("long_share")
        verdict = crowding_verdict(share)
        if verdict is None:
            continue
        coverage += 1
        row = {"symbol": base, "long_share": share}
        if verdict == "crowded_long":
            crowded_long.append(row)
        elif verdict == "crowded_short":
            crowded_short.append(row)
        else:
            neutral_n += 1
    crowded_long.sort(key=lambda r: r["long_share"], reverse=True)
    crowded_short.sort(key=lambda r: r["long_share"])
    return {"data_plane": "variational", "coverage": coverage,
            "crowded_long": crowded_long, "crowded_short": crowded_short,
            "neutral": neutral_n,
            "thresholds": {"crowded_long": _CROWDED_LONG,
                           "crowded_short": _CROWDED_SHORT},
            "note": "heuristic anchors, not gospel"}


# ── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    log_dir = os.path.join(_ROOT, "logs")
    os.makedirs(log_dir, exist_ok=True)
    now = time.time()
    out = {"ts": now, "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}
    try:
        try:
            snap = fetch_xyz_snapshot()
            out["hl_xyz"] = derived_complexes(snap)
        except Exception as e:
            snap = {}
            out["hl_xyz"] = {"error": str(e)[:120]}

        try:
            stats = fetch_variational_stats()
            out["positioning_crowding"] = crowding_section(stats, snap)
        except Exception as e:
            out["positioning_crowding"] = {"error": str(e)[:120],
                                           "data_plane": "variational",
                                           "degraded": "hl_only"}

        derived = out.get("hl_xyz") or {}
        verdicts = {}
        if "error" not in derived:
            ratio = None
            try:
                ratio = float(derived.get("copper_gold_ratio"))
            except (TypeError, ValueError):
                ratio = None
            # Rolling ratio history lives in the state file (last 30 runs).
            prev_state = _load(os.path.join(log_dir, "xyz_posture.json"), {})
            hist = (((prev_state.get("verdicts") or {}).get("metals_risk_tone")
                     or {}).get("ratio_history") or [])[-_RATIO_HISTORY_KEEP:]
            verdicts["metals_risk_tone"] = {
                "verdict": metals_risk_tone(ratio, hist),
                "ratio": ratio, "history_n": len(hist),
                "ratio_history": ([*hist, ratio] if ratio is not None else hist)}
            verdicts["dollar_tide"] = dollar_tide(
                (derived.get("dxy") or {}).get("chg_24h_pct"))
            verdicts["breadth_regime"] = breadth_regime(
                (derived.get("breadth") or {}).get("advancing_share"))
        crowd = out.get("positioning_crowding") or {}
        verdicts["positioning_crowding"] = (
            {"crowded_long_n": len(crowd.get("crowded_long") or []),
             "crowded_short_n": len(crowd.get("crowded_short") or []),
             "coverage": crowd.get("coverage")}
            if "error" not in crowd else {"error": crowd["error"]})
        out["verdicts"] = verdicts
    except Exception as e:
        out = {"ts": now, "utc": out["utc"], "error": str(e)[:160]}

    try:
        _atomic_write(os.path.join(log_dir, "xyz_posture.json"), out)
        v = out.get("verdicts") or {}
        d = out.get("hl_xyz") or {}
        hist_line = {"ts": now,
                     "assets": d.get("asset_count"),
                     "cugold": d.get("copper_gold_ratio"),
                     "dxy_chg": (d.get("dxy") or {}).get("chg_24h_pct"),
                     "adv_share": (d.get("breadth") or {}).get("advancing_share"),
                     "dollar_tide": v.get("dollar_tide"),
                     "breadth_regime": v.get("breadth_regime"),
                     "metals_tone": (v.get("metals_risk_tone") or {}).get("verdict"),
                     "crowd_cov": (v.get("positioning_crowding") or {}).get("coverage")}
        with open(os.path.join(log_dir, "xyz_posture_history.jsonl"), "a") as f:
            f.write(json.dumps(hist_line) + "\n")
    except Exception:
        pass
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
