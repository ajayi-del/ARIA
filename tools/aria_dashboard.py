#!/usr/bin/env python3
"""tools/aria_dashboard.py — a window into ARIA.

Runs ENTIRELY on the operator's Mac. One read-only SSH pull per refresh
(tail of aria.log + the JSON/MD report files the bot already writes),
parses locally, renders a self-contained HTML page. Zero code in the
trading process, zero inbound ports on the VM, zero hot-path contact —
the server only ever sees `tail`/`cat`/`wc`.

Usage:
    python3 tools/aria_dashboard.py            # one-shot render + open
    python3 tools/aria_dashboard.py --watch 60 # re-render every 60s
    python3 tools/aria_dashboard.py --no-open  # render without opening
"""

from __future__ import annotations

import argparse
import html
import json
import os
import subprocess
import sys
import time
import webbrowser
from collections import Counter
from datetime import datetime, timezone

VM = "aria-prod-v2"
ZONE = "europe-west3-c"
OUT = os.path.expanduser("~/aria_dashboard.html")
LOG_TAIL_BYTES = 3_000_000

REMOTE_CMD = r"""
echo '===LOG==='; tail -c {n} ~/ARIA/logs/aria.log;
echo '===FILE:compression==='; cat ~/ARIA/logs/compression_watchlist.json 2>/dev/null;
echo '===FILE:venue_comparison==='; cat ~/ARIA/logs/venue_comparison.json 2>/dev/null;
echo '===FILE:gate_report==='; cat ~/ARIA/logs/gate_report.json 2>/dev/null;
echo '===FILE:param_store==='; cat ~/ARIA/logs/param_store.json 2>/dev/null;
echo '===FILE:vault==='; cat ~/ARIA/logs/vault.json 2>/dev/null;
echo '===FILE:snapshots==='; wc -l ~/ARIA/logs/venue_snapshots.jsonl 2>/dev/null;
echo '===FILE:watchdog==='; cat ~/aria_watchdog/report.md 2>/dev/null;
echo '===FILE:proposals==='; cat ~/aria_watchdog/proposals.jsonl 2>/dev/null;
echo '===FILE:positions==='; curl -s --max-time 8 \
  https://mainnet-gw.sodex.dev/api/v1/perps/accounts/0xdb87899C08eA8A5C7cFCe2211a487C889B58A869/positions;
echo '===END==='
""".replace("{n}", str(LOG_TAIL_BYTES))


def pull() -> str:
    r = subprocess.run(
        ["gcloud", "compute", "ssh", VM, f"--zone={ZONE}",
         f"--command={REMOTE_CMD}"],
        capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:400])
    return r.stdout


def split_sections(raw: str) -> dict:
    import re
    # cat output may not end with \n — markers can glue onto JSON tails
    raw = re.sub(r"(?<!^)(?====LOG===|===FILE:|===END===)", "\n", raw,
                 flags=re.M)
    secs, cur, buf = {}, None, []
    for line in raw.splitlines():
        m = re.match(r"^===FILE:([a-z_]+)===$", line)
        if m or line in ("===LOG===", "===END==="):
            if cur:
                secs[cur] = "\n".join(buf)
            cur = m.group(1) if m else ("LOG" if line == "===LOG===" else None)
            buf = []
        else:
            buf.append(line)
    if cur:
        secs[cur] = "\n".join(buf)
    return secs


def parse_log(txt: str) -> list:
    out = []
    for line in txt.splitlines():
        if not line.startswith("{"):
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if "event" in r:
            out.append(r)
    return out


def _f(v, d=0.0):
    try:
        return float(str(v).replace("$", "").replace(",", ""))
    except Exception:
        return d


def _ts(r):
    return r.get("timestamp", "")[11:19]


def esc(s):
    return html.escape(str(s))


HISTORY = os.path.expanduser("~/aria_dashboard_history.jsonl")


def update_history(st: dict) -> list:
    """Mac-side equity memory: one JSONL line per poll. The bot never feels
    this; the terminal accumulates a curve ARIA itself doesn't store."""
    pts = []
    try:
        with open(HISTORY) as f:
            for line in f:
                try:
                    pts.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    pts.append({"ts": time.time(), "balance": st["balance"],
                "upnl": st["upnl"], "realized": st["realized"],
                "open": len(st["positions"])})
    pts = pts[-2000:]
    try:
        with open(HISTORY, "w") as f:
            for p in pts:
                f.write(json.dumps(p) + "\n")
    except Exception:
        pass
    return pts


def sparkline(pts, key="balance", w=300, h=56, stroke="#e6b450") -> str:
    if len(pts) < 2:
        return "<div class='dim'>curve builds as the terminal polls</div>"
    xs = [p[key] for p in pts[-300:]]
    lo, hi = min(xs), max(xs)
    rng = (hi - lo) or 1.0
    step = w / (len(xs) - 1)
    d = " ".join(f"{i*step:.1f},{(h-4-(x-lo)/rng*(h-8)):.1f}"
                 for i, x in enumerate(xs))
    last = xs[-1]
    chg = xs[-1] - xs[0]
    cls = "up" if chg >= 0 else "dn"
    return (f"<svg width='{w}' height='{h}' style='background:#0d1117'>"
            f"<polyline points='{d}' fill='none' stroke='{stroke}' "
            f"stroke-width='1.4'/></svg>"
            f"<div class='{cls}'>{chg:+.2f} over window · last {last:,.2f}</div>")


TAPE_EVENTS = (
    "position_closed", "explosive_fired", "explosive_blocked",
    "explosive_time_stop", "explosive_stop_to_breakeven",
    "subsystem_graduated", "subsystem_lapsed", "graduation_eval",
    "graduation_leverage_boost", "router_graduation_routing",
    "cascade_momentum_fired", "cascade_aftermath_primed_orchestrator",
    "chancellor_veto", "sleeve_halt", "venue_snapshot",
    "rally_graduated", "startup_position_synced",
)


def build_tape(ev: list) -> list:
    out = []
    for r in reversed(ev):
        if r["event"] in TAPE_EVENTS:
            detail = (r.get("symbol") or r.get("subsystem")
                      or r.get("reason") or "")
            extra = ""
            if r["event"] == "position_closed":
                extra = str(r.get("pnl", ""))
            elif r["event"] == "graduation_eval":
                extra = f"n={r.get('n')} wr={r.get('shrunk_wr')} grad={r.get('graduated')}"
            out.append({"ts": _ts(r), "event": r["event"],
                        "detail": f"{detail} {extra}".strip()})
        if len(out) >= 22:
            break
    return out


def venue_matrix(ev: list, st: dict) -> list:
    """Per-venue health row, derived purely from event recency. Extends to
    new venues by adding a row — the terminal is venue-count-agnostic."""
    def last(event, **kw):
        for r in reversed(ev):
            if r["event"] == event and all(r.get(k) == v for k, v in kw.items()):
                return r.get("timestamp", "")[11:19]
        return ""
    fails = Counter(r.get("venue") for r in ev
                    if r["event"] == "venue_balance_failed")
    return [
        {"venue": "SoDEX", "role": "majors · cascade · tradfi legacy",
         "health": last("startup_sync_complete") or "?", "alarms": fails.get("sodex", 0)},
        {"venue": "Aster", "role": "36 alts + commodities · explosive",
         "health": last("aster_feed_connected") or "?", "alarms": fails.get("aster", 0)},
        {"venue": "Bybit", "role": "data lens (keys stale — known)",
         "health": "—", "alarms": fails.get("bybit", 0)},
    ]



def build_state(secs: dict) -> dict:
    ev = parse_log(secs.get("LOG", ""))
    st = {"generated": datetime.now(timezone.utc).strftime("%H:%M:%S UTC")}

    bal = next((r for r in reversed(ev) if r["event"] == "account_balance"), {})
    pnl = next((r for r in reversed(ev) if r["event"] == "pnl_attribution"), {})
    st["balance"] = _f(bal.get("balance"))
    st["upnl"] = _f(pnl.get("unrealized_total"))
    st["realized"] = _f(pnl.get("realized_est"))
    st["breakdown"] = pnl.get("breakdown", "")

    try:
        raw = secs.get("positions", "").strip()
        d = json.loads(raw) if raw.startswith("{") else {}
        d = d.get("data", d)
        pos = d.get("positions", d) if isinstance(d, dict) else d
        st["positions"] = [
            {"symbol": p.get("symbol") or p.get("symbol_id"),
             "size": p.get("size") or p.get("quantity"),
             "entry": p.get("entry_price") or p.get("avgPrice") or ""}
            for p in (pos or [])]
    except Exception:
        st["positions"] = []

    st["trades"] = [
        {"ts": _ts(r), "symbol": r.get("symbol"), "pnl": r.get("pnl"),
         "exit": r.get("exit_reason", r.get("side", "")),
         "daily": r.get("daily_pnl")}
        for r in ev if r["event"] == "position_closed"
        and r.get("logger") == "__main__"][-12:][::-1]

    st["gates"] = Counter(
        r["event"] for r in ev
        if "reject" in r["event"] or "blocked" in r["event"]).most_common(10)

    ex_fired = [r for r in ev if r["event"] == "explosive_fired"]
    ex_blk = Counter(r.get("reason", "?") for r in ev
                     if r["event"] == "explosive_blocked")
    st["explosive"] = {"fired": len(ex_fired), "blocked": ex_blk.most_common(6)}

    st["grad_events"] = [
        {"ts": _ts(r), "event": r["event"],
         "subsystem": r.get("subsystem", r.get("symbol", "")),
         "wr": r.get("shrunk_wr", ""), "n": r.get("n", "")}
        for r in ev if r["event"] in
        ("subsystem_graduated", "subsystem_lapsed",
         "router_graduation_routing", "graduation_leverage_boost")][-8:][::-1]

    try:
        ps = json.loads(secs.get("param_store", "") or "{}")
        items = ps.get("params", ps) if isinstance(ps, dict) else {}
        st["grad_keys"] = [k for k in
                           (items.keys() if isinstance(items, dict) else [])
                           if str(k).startswith("grad_")]
    except Exception:
        st["grad_keys"] = []

    for name, key in (("compression", "compression"),
                      ("venue_comparison", "venue_comparison")):
        try:
            st[key] = json.loads(secs.get(key, "") or "{}")
        except Exception:
            st[key] = {}

    hb = next((r for r in reversed(ev)
               if r["event"] == "router_v2_heartbeat"), {})
    st["router"] = hb
    st["snapshots"] = secs.get("snapshots", "").strip().split(" ")[0] or "0"
    st["watchdog"] = secs.get("watchdog", "").strip()[:3000]
    props = []
    for line in secs.get("proposals", "").splitlines():
        try:
            props.append(json.loads(line))
        except Exception:
            continue
    st["proposals"] = props[-8:][::-1]
    st["_events"] = ev
    return st


CSS = """
body{background:#05070b;color:#c8d3e0;font:12px/1.4 ui-monospace,Menlo,monospace;margin:0;padding:18px}
h1{font-size:16px;color:#e6b450;margin:0;letter-spacing:.06em}
h2{font-size:11px;color:#e6b450;text-transform:uppercase;letter-spacing:.14em;
   border-bottom:1px solid #1d2635;padding-bottom:3px;margin:0 0 8px}
.sub{color:#55647a;margin:2px 0 14px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(340px,1fr));gap:10px}
.card{background:#0a0e15;border:1px solid #1a2230;border-radius:4px;padding:10px 12px}
.wide{grid-column:1/-1}
.big{font-size:24px;color:#fff}.up{color:#7ee0a3}.dn{color:#e06c75}.amber{color:#e6b450}
table{width:100%;border-collapse:collapse}td,th{padding:2px 6px;text-align:left;
border-bottom:1px solid #111825;white-space:nowrap}th{color:#55647a;font-weight:normal}
.dim{color:#55647a}.pill{display:inline-block;padding:1px 7px;border-radius:8px;
background:#111a28;color:#8fb8e8;margin:1px 2px;font-size:10px}
pre{white-space:pre-wrap;word-break:break-word;color:#93a4bb;margin:0;max-height:240px;overflow:auto}
.tape td{border-bottom:1px solid #0e1420;color:#9fb2c8}
.tape td:first-child{color:#4a5a70}
"""


def render(st: dict) -> str:
    def rows(rs, cols):
        body = "".join(
            "<tr>" + "".join(f"<td>{esc(fn(r))}</td>" for _, fn in cols)
            + "</tr>"
            for r in rs) or f"<tr><td class='dim' colspan={len(cols)}>none yet</td></tr>"
        return f"<table><tr>{''.join(f'<th>{h}</th>' for h, _ in cols)}</tr>{body}</table>"

    bal_cls = "up" if st["balance"] >= 150 else "dn"
    upnl_cls = "up" if st["upnl"] >= 0 else "dn"
    comp = st.get("compression") or {}
    comp_rows = comp.get("watchlist") or comp.get("rows") or []
    vc = (st.get("venue_comparison") or {}).get("symbols") or {}
    hist = update_history(st)
    tape = build_tape(st.get("_events", []))
    venues = venue_matrix(st.get("_events", []), st)

    parts = [f"""<html><head><meta http-equiv="refresh" content="60">
<title>ARIA TERMINAL</title><style>{CSS}</style></head><body>
<h1>▮ ARIA TERMINAL</h1>
<div class="sub">{st['generated']} · read-only scrape of aria-prod-v2 · 60s auto-refresh · zero hot-path contact</div>
<div class="grid">

<div class="card wide"><h2>Kingdom equity — Mac-side curve</h2>
<table><tr>
<td style="width:1%"><div class="big {bal_cls}">${st['balance']:,.2f}</div>
<div>uPnL <span class="{upnl_cls}">{st['upnl']:+.2f}</span> · realized {st['realized']:+.2f}</div>
<div class="dim">{esc(st['breakdown'])}</div></td>
<td>{sparkline(hist)}</td>
</tr></table></div>

<div class="card"><h2>Venue matrix</h2>
{rows(venues, [("venue", lambda r: r["venue"]), ("role", lambda r: r["role"]),
               ("last healthy", lambda r: r["health"]),
               ("alarms", lambda r: r["alarms"] or "")])}
<div class="dim" style="margin-top:6px">router: dual_listed={st["router"].get("dual_listed", "?")} · divergences={st["router"].get("divergences", "?")} · graduated={st["router"].get("graduated", "?")} · snapshots={st["snapshots"]}</div>
</div>

<div class="card"><h2>Open positions (SoDEX live)</h2>
{rows(st["positions"], [("sym", lambda r: r["symbol"]), ("size", lambda r: r["size"]), ("entry", lambda r: r["entry"])])}
<h2 style="margin-top:10px">Recent closes</h2>
{rows(st["trades"][:8], [("time", lambda r: r["ts"]), ("sym", lambda r: r["symbol"]),
                     ("pnl", lambda r: r["pnl"]), ("exit", lambda r: str(r["exit"])[:26])])}
</div>

<div class="card wide"><h2>Event tape</h2>
<table class="tape">{"".join(f"<tr><td>{esc(t['ts'])}</td><td class='amber'>{esc(t['event'])}</td><td>{esc(t['detail'])}</td></tr>" for t in tape) or "<tr><td class='dim'>no notable events in window</td></tr>"}</table>
</div>

<div class="card"><h2>Gates working · explosive pilot</h2>
{"".join(f'<span class="pill">{esc(k)} ×{v}</span>' for k, v in st["gates"]) or "<span class='dim'>no rejections</span>"}
<div style="margin-top:6px">explosive fired: <b>{st["explosive"]["fired"]}</b>
{"".join(f'<span class="pill">{esc(k)} ×{v}</span>' for k, v in st["explosive"]["blocked"]) or ""}</div>
</div>

<div class="card"><h2>Dreamer — compression watch</h2>
<div class="dim">scanned {comp.get("scanned", "?")} · max {comp.get("max_score", "?")} · best {esc(comp.get("best_symbol", "?"))} {esc(comp.get("best_precursors", ""))}</div>
{rows(comp_rows[:8], [("sym", lambda r: r.get("symbol", r.get("sym", "?"))),
                      ("score", lambda r: r.get("score", "")),
                      ("days", lambda r: r.get("days_compressed", "")),
                      ("status", lambda r: r.get("status", ""))]) if comp_rows else "<div class='dim'>watchlist empty — market quiet</div>"}
</div>

<div class="card"><h2>Graduation (autonomous)</h2>
<div>live keys: {"".join(f'<span class="pill">{esc(k)}</span>' for k in st["grad_keys"]) or "<span class='dim'>none earned yet</span>"}</div>
{rows(st["grad_events"], [("time", lambda r: r["ts"]), ("event", lambda r: r["event"]),
                          ("what", lambda r: r["subsystem"]),
                          ("wr", lambda r: r["wr"]), ("n", lambda r: r["n"])])}
</div>

<div class="card"><h2>Venue comparison (7d)</h2>
{rows([{"s": k, **v} for k, v in vc.items()],
      [("sym", lambda r: r["s"]), ("n", lambda r: r.get("n", "")),
       ("sd bps", lambda r: r.get("sodex_spread_bps_avg", "")),
       ("as bps", lambda r: r.get("aster_spread_bps_avg", "")),
       ("verdict", lambda r: r.get("verdict", ""))]) if vc else "<div class='dim'>report pending snapshots</div>"}
</div>

<div class="card"><h2>Proposals (inter-node)</h2>
{rows(st["proposals"], [("id", lambda r: r.get("id", r.get("title", "?"))[:24]),
                        ("status", lambda r: r.get("status", "?")),
                        ("risk", lambda r: r.get("risk", "")),
                        ("node", lambda r: r.get("node", ""))])}
</div>

<div class="card"><h2>Watchdog</h2><pre>{esc(st["watchdog"] or "no report yet")}</pre></div>

</div></body></html>"""
    ]
    return "".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=0,
                    help="re-render every N seconds")
    ap.add_argument("--no-open", action="store_true")
    a = ap.parse_args()
    while True:
        try:
            st = build_state(split_sections(pull()))
            with open(OUT, "w") as f:
                f.write(render(st))
            print(f"[{st['generated']}] wrote {OUT} "
                  f"(balance ${st['balance']:,.2f}, "
                  f"{len(st['positions'])} positions, "
                  f"{len(st['trades'])} closes)")
        except Exception as e:
            print(f"pull failed: {e}", file=sys.stderr)
        if not a.watch:
            break
        time.sleep(a.watch)
    if not a.no_open and os.path.exists(OUT):
        webbrowser.open(f"file://{OUT}")


if __name__ == "__main__":
    main()
