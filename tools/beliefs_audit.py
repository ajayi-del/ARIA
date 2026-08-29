"""Beliefs audit — verification-before-activation diff for the 2026-08-29
beliefs-layer repair (phantom filter + direction conditioning + skeptic decay).

Operator directive: run BEFORE the restart that activates the repair and show
the diff. The journal-corruption audit found three poison layers (phantom
SPCX closes, regime-window contamination, direction blindness — ETH 100% WR
longs throttled by 17% WR shorts pooled into one belief). This tool measures
exactly what changes when the repair activates, per symbol (crypto AND
stocks), per direction, per skeptic config, and per agent personality.

    .venv/bin/python tools/beliefs_audit.py

Reads:  logs/trade_journal_*.json (all-time, deduped like perf.restore),
        logs/shadow_scored.jsonl, logs/agent_winrates.json (persisted).
Writes: logs/beliefs_audit.json (atomic). Stdout: the diff tables.
Stdlib + repo imports, best-effort, exit 0 always.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
OUT_PATH = os.path.join(LOG_DIR, "beliefs_audit.json")
BREAKEVEN_WR = 0.5
VETO_MARGIN = 0.6
VETO_MIN_N = 10


# ── Loaders (read-only; journals are permanent, rule #14) ────────────────────

def _load_all_closed() -> list:
    from memory.trade_journal import is_phantom_record
    out, seen = [], set()
    n_dupes = 0
    for fpath in sorted(glob.glob(os.path.join(LOG_DIR, "trade_journal_*.json"))):
        try:
            with open(fpath) as fh:
                raw = json.load(fh)
        except Exception:
            continue
        if not isinstance(raw, list):
            continue
        for e in raw:
            if not isinstance(e, dict) or e.get("outcome") not in ("win", "loss"):
                continue
            key = (e.get("entry_id"), e.get("closed_at_ms"))
            if key in seen:
                n_dupes += 1
                continue
            seen.add(key)
            e["_phantom"] = bool(is_phantom_record(e))
            out.append(e)
    out.sort(key=lambda e: e.get("closed_at_ms") or e.get("timestamp_ms") or 0)
    return out, n_dupes


def _load_shadow_scored() -> list:
    out = []
    try:
        with open(os.path.join(LOG_DIR, "shadow_scored.jsonl")) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        pass
    return out


# ── Section 1: phantom census (stocks included — bimodality scan) ────────────

def phantom_census(closed: list) -> dict:
    per_sym = defaultdict(lambda: {"phantom_n": 0, "phantom_pnl": 0.0,
                                   "real_n": 0, "real_pnl": 0.0, "max_real_abs": 0.0})
    suspects = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for e in closed:
        sym = e.get("symbol") or "?"
        pnl = float(e.get("pnl_net_usd") or e.get("pnl_usd") or 0.0)
        if e["_phantom"]:
            per_sym[sym]["phantom_n"] += 1
            per_sym[sym]["phantom_pnl"] += pnl
        else:
            per_sym[sym]["real_n"] += 1
            per_sym[sym]["real_pnl"] += pnl
            per_sym[sym]["max_real_abs"] = max(per_sym[sym]["max_real_abs"], abs(pnl))
            if abs(pnl) > 100.0 and sym != "SPCX-USD":
                suspects[sym]["n"] += 1
                suspects[sym]["pnl"] += pnl
    rows = [{"symbol": s, **{k: (round(v, 2) if isinstance(v, float) else v)
                             for k, v in d.items()}}
            for s, d in sorted(per_sym.items(),
                               key=lambda kv: -abs(kv[1]["phantom_pnl"]))]
    return {"by_symbol": rows,
            "total_phantom_n": sum(r["phantom_n"] for r in rows),
            "total_phantom_pnl": round(sum(r["phantom_pnl"] for r in rows), 2),
            "non_spcx_large_pnl_suspects": {s: {"n": d["n"], "pnl": round(d["pnl"], 2)}
                                            for s, d in suspects.items()}}


# ── Section 2: symbol-edge old (pooled) vs new (direction-split) ─────────────

def symbol_edge_diff(closed_real: list) -> list:
    from intelligence.symbol_edge import SymbolEdgeThrottler
    th = SymbolEdgeThrottler()
    rows = []
    symbols = sorted({e.get("symbol") for e in closed_real if e.get("symbol")})
    for sym in symbols:
        entries = [e for e in closed_real if e.get("symbol") == sym]
        pooled = th.get_symbol_edge(sym, list(entries))  # legacy pooled read
        long_e = th.get_symbol_edge(sym, list(entries), direction="long")
        short_e = th.get_symbol_edge(sym, list(entries), direction="short")
        changed = (pooled["edge_mult"] != long_e["edge_mult"]
                   or pooled["edge_mult"] != short_e["edge_mult"])
        rows.append({
            "symbol": sym, "n": len(entries), "changed": changed,
            "pooled": {"mult": pooled["edge_mult"], "wr": pooled["win_rate"],
                       "avg_pnl": pooled["avg_pnl"]},
            "long": {"mult": long_e["edge_mult"], "wr": long_e["win_rate"],
                     "reason": long_e["reason"]},
            "short": {"mult": short_e["edge_mult"], "wr": short_e["win_rate"],
                      "reason": short_e["reason"]},
        })
    rows.sort(key=lambda r: (not r["changed"], -r["n"]))
    return rows


# ── Section 3: skeptic base-rate 4-config diff ───────────────────────────────

class _ShadowStub:
    def __init__(self, records):
        self._records = records

    def scored_records(self):
        return list(self._records)


def skeptic_diff(shadow: list) -> list:
    from intelligence.skeptic import Skeptic
    rows = []
    if not shadow:
        return rows
    sk = Skeptic(_ShadowStub(shadow))
    cohorts = sorted({(r.get("symbol") or "", (r.get("direction") or "").lower())
                      for r in shadow if r.get("symbol")})
    now = max(float(r.get("ts") or 0) for r in shadow)
    for sym, direction in cohorts:
        if not direction:
            continue
        recs = [r for r in shadow if r.get("symbol") == sym]
        if len(recs) < 5:
            continue
        os.environ["SKEPTIC_DECAY_HALFLIFE_DAYS"] = "0"
        leg_wr, leg_n = sk.base_rate(symbol=sym, prior_wr=0.5, now=now)
        os.environ["SKEPTIC_DECAY_HALFLIFE_DAYS"] = "14"
        dec_wr, dec_n = sk.base_rate(symbol=sym, prior_wr=0.5, now=now)
        dir_wr, dir_n = sk.base_rate(symbol=sym, prior_wr=0.5,
                                     direction=direction, now=now)
        veto = lambda wr, n: n >= VETO_MIN_N and wr < BREAKEVEN_WR * VETO_MARGIN
        rows.append({
            "symbol": sym, "direction": direction, "shadow_n": len(recs),
            "legacy": {"wr": round(leg_wr, 3), "n": leg_n, "veto": veto(leg_wr, leg_n)},
            "decay_only": {"wr": round(dec_wr, 3), "n": dec_n, "veto": veto(dec_wr, dec_n)},
            "direction_decay": {"wr": round(dir_wr, 3), "n": dir_n, "veto": veto(dir_wr, dir_n)},
            "veto_flips": veto(leg_wr, leg_n) != veto(dir_wr, dir_n),
        })
    os.environ["SKEPTIC_DECAY_HALFLIFE_DAYS"] = "14"
    rows.sort(key=lambda r: (not r["veto_flips"], -r["shadow_n"]))
    return rows


# ── Section 4: agent winrates — stored vs recomputed (phantom-filtered) ──────

def agent_winrates_diff(closed: list) -> dict:
    stored = {}
    try:
        with open(os.path.join(LOG_DIR, "agent_winrates.json")) as f:
            stored = json.load(f)
    except Exception:
        pass
    def _stats(entries):
        n = len(entries)
        wins = sum(1 for e in entries
                   if float(e.get("pnl_net_usd") or e.get("pnl_usd") or 0.0) > 0)
        pnl = sum(float(e.get("pnl_net_usd") or e.get("pnl_usd") or 0.0) for e in entries)
        return {"n": n, "wr": round(wins / n, 3) if n else None, "pnl": round(pnl, 2)}
    by_p_unfiltered = defaultdict(list)
    by_p_filtered = defaultdict(list)
    for e in closed:
        p = (e.get("personality") or "SCOUT").upper()
        by_p_unfiltered[p].append(e)
        if not e["_phantom"]:
            by_p_filtered[p].append(e)
    rows = []
    for p in sorted(set(by_p_unfiltered) | set(stored)):
        st = stored.get(p) or {}
        sw, sl = st.get("wins"), st.get("losses")
        st_n = (sw + sl) if isinstance(sw, int) and isinstance(sl, int) else None
        st_wr = round(sw / st_n, 3) if st_n else None
        rows.append({
            "personality": p,
            "stored": {"n": st_n, "wr": st_wr,
                       "pnl": round(st.get("total_pnl"), 2)
                       if isinstance(st.get("total_pnl"), (int, float)) else None,
                       "streak": st.get("streak")},
            "recomputed_unfiltered": _stats(by_p_unfiltered.get(p, [])),
            "recomputed_filtered": _stats(by_p_filtered.get(p, [])),
        })
    return {"personalities": rows,
            "note": "sizing caps read get_win_rate(personality) — a personality "
                    "whose filtered WR collapses below its band was sized by a "
                    "phantom-inflated (or phantom-deflated) belief"}


# ── Shell ────────────────────────────────────────────────────────────────────

def _atomic_write(path: str, payload: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def main() -> int:
    closed, n_dupes = _load_all_closed()
    real = [e for e in closed if not e["_phantom"]]
    shadow = _load_shadow_scored()

    census = phantom_census(closed)
    edge_rows = symbol_edge_diff(real)
    skeptic_rows = skeptic_diff(shadow)
    agents = agent_winrates_diff(closed)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "journal": {"closed_total": len(closed), "closed_real": len(real),
                    "phantom": len(closed) - len(real), "dupes_skipped": n_dupes},
        "phantom_census": census,
        "symbol_edge_diff": edge_rows,
        "skeptic_diff": skeptic_rows,
        "agent_winrates": agents,
    }
    _atomic_write(OUT_PATH, json.dumps(payload, indent=1))

    print(f"BELIEFS AUDIT — {len(closed)} closed trades "
          f"({len(closed) - len(real)} phantom, {n_dupes} dupes skipped)")
    print(f"Phantom census: {census['total_phantom_n']} records, "
          f"${census['total_phantom_pnl']:+,.2f} fake pnl")
    if census["non_spcx_large_pnl_suspects"]:
        print(f"  NON-SPCX large-pnl suspects: "
              f"{json.dumps(census['non_spcx_large_pnl_suspects'])}")
    print()
    print("Symbol edge — pooled vs direction-split (changed beliefs first):")
    for r in edge_rows[:15]:
        mark = " *** CHANGED" if r["changed"] else ""
        print(f"  {r['symbol']:<14} n={r['n']:<4} pooled {r['pooled']['mult']:.2f}x"
              f" (wr {r['pooled']['wr']:.0%}) | long {r['long']['mult']:.2f}x"
              f" | short {r['short']['mult']:.2f}x{mark}")
    print()
    if skeptic_rows:
        print("Skeptic base rates — legacy vs direction+decay (veto flips first):")
        for r in skeptic_rows[:15]:
            mark = " *** VETO FLIPS" if r["veto_flips"] else ""
            print(f"  {r['symbol']:<14} {r['direction']:<6} n={r['shadow_n']:<5}"
                  f" legacy {r['legacy']['wr']:.2f}(n{r['legacy']['n']})"
                  f" | decay {r['decay_only']['wr']:.2f}(n{r['decay_only']['n']})"
                  f" | dir+decay {r['direction_decay']['wr']:.2f}"
                  f"(n{r['direction_decay']['n']}){mark}")
        print()
    print("Agent winrates — stored vs recomputed (sizing-cap inputs):")
    for r in agents["personalities"]:
        st, ru, rf = r["stored"], r["recomputed_unfiltered"], r["recomputed_filtered"]
        print(f"  {r['personality']:<12} stored wr={st.get('wr')} n={st.get('n')}"
              f" | unfiltered wr={ru['wr']} n={ru['n']} pnl=${ru['pnl']:+}"
              f" | FILTERED wr={rf['wr']} n={rf['n']} pnl=${rf['pnl']:+}")
    print(f"\nWrote {OUT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
