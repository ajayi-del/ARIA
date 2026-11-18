#!/usr/bin/env python3
"""Deterministic prop fix-intent collector (host arithmetic only).

Reads Vultr prop harness state roots + journals → report.md + cycles.jsonl
+ verdict.json. Never edits Kant/Nietzsche/Chancellor. Never deletes journals.
Never auto-restarts open-book seats. Kill switch: MONITOR_ROOT/DISABLED.

Usage (on Vultr):
  /root/ARIA/.venv/bin/python /root/prop_fix_monitor/collect.py
  MONITOR_ROOT=/root/prop_fix_monitor STATE_GLOB=/root/prop_harness_state*

Exit 0 always (fail-closed sections carry their own errors) unless --strict.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow `python collect.py` from MONITOR_ROOT or repo tools/ copy.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from classify import (  # noqa: E402
    DESIGNED_DEAD_SEATS,
    GOLD_KEEP,
    HB_STALE_S,
    NEIGHBOUR_CRYPTO,
    PRIMARY_CRYPTO,
    WINDOW_S,
    classify_loop_error,
    gold_floor_ok,
    metal_veto_verdict,
    opposite_pairs,
    parse_symbol_min_coh_map,
    seat_role,
    slot_geometry,
)

DEFAULT_MONITOR_ROOT = Path(os.environ.get("MONITOR_ROOT", "/root/prop_fix_monitor"))
DEFAULT_STATE_PARENT = Path(os.environ.get("STATE_PARENT", "/root"))
DEFAULT_ARIA_ROOT = Path(os.environ.get("ARIA_ROOT", "/root/ARIA"))
DEPLOYED_SHA_PATH = "DEPLOYED_SHA"
EXPECTED_SHA = os.environ.get("EXPECTED_DEPLOYED_SHA", "520ff6c")


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _parse_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        # never retain secrets in report
        if any(s in k.upper() for s in ("KEY", "SECRET", "TOKEN", "PASS", "PRIVATE")):
            continue
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _live_pids_by_root() -> dict[str, list[int]]:
    """Map PROP_STATE_ROOT → [pids] for prop_harness.main processes."""
    by: dict[str, list[int]] = defaultdict(list)
    proc = Path("/proc")
    if not proc.is_dir():
        return {}
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except Exception:
            continue
        if "prop_harness.main" not in cmdline and "prop_harness/main" not in cmdline:
            continue
        try:
            env_raw = (entry / "environ").read_bytes()
        except Exception:
            continue
        root = None
        for part in env_raw.split(b"\0"):
            if part.startswith(b"PROP_STATE_ROOT="):
                root = part.split(b"=", 1)[1].decode(errors="replace")
                break
        if root:
            by[root].append(int(entry.name))
    return by


def _discover_seats(parent: Path) -> list[Path]:
    seats = sorted(parent.glob("prop_harness_state*"))
    return [p for p in seats if p.is_dir()]


def _journal_window(path: Path, cut_ts: float) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    try:
        with path.open(errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except Exception:
                    continue
                ts = e.get("ts") or 0
                try:
                    ts = float(ts)
                except (TypeError, ValueError):
                    continue
                if ts >= cut_ts:
                    rows.append(e)
    except Exception:
        return rows
    return rows


def _open_positions(root: Path) -> list[dict[str, Any]]:
    data = _read_json(root / "prop_positions.json") or {}
    positions = data.get("positions") if isinstance(data, dict) else data
    if not isinstance(positions, list):
        return []
    opens = []
    for p in positions:
        if not isinstance(p, dict):
            continue
        if p.get("closed_ts"):
            continue
        if p.get("status") in ("closed", "filled_closed"):
            continue
        opens.append(p)
    return opens


def collect_seat(
    root: Path,
    *,
    cut_ts: float,
    pids_by_root: dict[str, list[int]],
) -> dict[str, Any]:
    role = seat_role(root.name)
    env = _parse_env_file(root / "prop.env")
    hb = _read_json(root / "heartbeat.json") or {}
    hb_ts = float(hb.get("ts") or 0)
    hb_age = (time.time() - hb_ts) if hb_ts else None
    pids = pids_by_root.get(str(root), [])
    pidfile = root / "harness.pid"
    pidfile_pid = None
    if pidfile.is_file():
        try:
            pidfile_pid = int(pidfile.read_text().strip())
        except Exception:
            pidfile_pid = None
    opens = _open_positions(root)
    open_n = len(opens)
    max_pos = None
    try:
        max_pos = int(env.get("PROP_MAX_POSITIONS") or "")
    except ValueError:
        max_pos = None
    reserve = None
    try:
        reserve = int(env.get("PROP_SLOT_RESERVE") or "0")
    except ValueError:
        reserve = 0
    geom = slot_geometry(max_pos, open_n, reserve)
    coh_map = parse_symbol_min_coh_map(env.get("PROP_SYMBOL_MIN_COH_FIRE"))
    gold = gold_floor_ok(coh_map, keep=GOLD_KEEP) if role == "ns_metal" or coh_map else None

    events = _journal_window(root / "journal" / "events.jsonl", cut_ts)
    recent_cut = time.time() - 30 * 60  # contemporaneous alert window
    type_ctr: Counter[str] = Counter()
    loop_cls: Counter[str] = Counter()
    reason_ctr: Counter[str] = Counter()
    band_a = 0
    slot_refuse = 0
    slot_refuse_recent = 0
    metal_veto_refuse = 0
    metal_veto_refuse_recent = 0
    gold_coh_refuse = 0
    company_conflict = 0
    for e in events:
        t = str(e.get("type") or "?")
        type_ctr[t] += 1
        ts = float(e.get("ts") or 0)
        if t == "loop_error":
            loop_cls[classify_loop_error(e.get("error"))] += 1
        reason = str(e.get("reason") or "")
        if reason:
            reason_ctr[reason] += 1
        if "probe_standdown_slot_reserve" in reason:
            slot_refuse += 1
            if ts >= recent_cut:
                slot_refuse_recent += 1
        if "loss veto metal|" in reason:
            metal_veto_refuse += 1
            if ts >= recent_cut:
                metal_veto_refuse_recent += 1
        if "symbol_min_coh_fire" in reason and (
            "GOLD" in str(e.get("asset") or "") or "XAUT" in str(e.get("asset") or "")
        ):
            gold_coh_refuse += 1
        if t in ("company_conflict_open", "company_conflict_block"):
            company_conflict += 1
        # Band-A fear-scratch heuristic: band_label on close stamps
        if t in ("close_fill_snapshot", "reconcile_closed_external", "position_closed"):
            bl = str(e.get("band_label") or e.get("band") or "").upper()
            if bl in ("A", "BAND_A", "BAND-A") or "fear" in reason.lower():
                band_a += 1

    designed_dead = role in DESIGNED_DEAD_SEATS
    alive = len(pids) == 1
    stale = (hb_age is None) or (hb_age > HB_STALE_S)
    if designed_dead:
        health = "DESIGNED_DEAD" if not alive else "UNEXPECTED_ALIVE"
    elif len(pids) > 1:
        health = "DUPLICATE"
    elif not alive:
        health = "DEAD"
    elif stale:
        health = "STALE"
    else:
        health = "OK"

    return {
        "seat": role,
        "state_root": str(root),
        "health": health,
        "designed_dead": designed_dead,
        "pids": pids,
        "pidfile_pid": pidfile_pid,
        "pidfile_stale": bool(pidfile_pid and pidfile_pid not in pids),
        "hb_age_s": round(hb_age, 1) if hb_age is not None else None,
        "enabled": hb.get("enabled"),
        "dd": hb.get("dd"),
        "budget": hb.get("budget"),
        "open_brackets_hb": hb.get("open_brackets"),
        "open_n": open_n,
        "opens": [
            {"asset": p.get("asset") or p.get("symbol"), "side": p.get("side")}
            for p in opens
        ],
        "max_positions": max_pos,
        "slot_reserve": reserve,
        "geometry": geom,
        "symbol_min_coh_fire": env.get("PROP_SYMBOL_MIN_COH_FIRE"),
        "gold_floor": gold,
        "crypto_min_coh_fire": env.get("PROP_CRYPTO_MIN_COH_FIRE"),
        "tradfi_min_coh_fire": env.get("PROP_TRADFI_MIN_COH_FIRE"),
        "event_counts": dict(type_ctr.most_common(20)),
        "loop_error_classes": dict(loop_cls),
        "slot_reserve_refuses": slot_refuse,
        "slot_reserve_refuses_recent_30m": slot_refuse_recent,
        "metal_veto_refuses": metal_veto_refuse,
        "metal_veto_refuses_recent_30m": metal_veto_refuse_recent,
        "gold_symbol_min_coh_refuses": gold_coh_refuse,
        "company_conflict_events": company_conflict,
        "band_a_closes": band_a,
        "top_reasons": reason_ctr.most_common(8),
    }


def collect_shared(demo_root: Path) -> dict[str, Any]:
    program = demo_root / "program"
    loss = _read_json(program / "loss_veto.json") or {}
    stats = loss.get("stats") or {}
    metal = {
        "sell": metal_veto_verdict(stats.get("metal|sell")),
        "buy": metal_veto_verdict(stats.get("metal|buy")),
        "semis_buy_veto": bool((stats.get("equity_semis|buy") or {}).get("veto")),
        "semis_sell_veto": bool((stats.get("equity_semis|sell") or {}).get("veto")),
    }
    book = _read_json(program / "book_view.json") or {}
    return {
        "loss_veto_path": str(program / "loss_veto.json"),
        "metal": metal,
        "book_view_schema": book.get("schema"),
        "book_view_sleeve_n": len(book.get("sleeves") or {}),
    }


def propose_maintain(seats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Whitelist proposals only — never auto-apply open-book restarts."""
    props: list[dict[str, Any]] = []
    for s in seats:
        if s.get("designed_dead"):
            if s["health"] == "UNEXPECTED_ALIVE":
                props.append({
                    "action": "ALERT_ONLY",
                    "seat": s["seat"],
                    "detail": "turbo2/designed-dead seat is ALIVE — do NOT heal; Governor disposition",
                })
            continue
        if s.get("pidfile_stale") and s["health"] in ("DEAD", "STALE") and s.get("open_n", 0) == 0:
            props.append({
                "action": "CLEAR_STALE_PIDFILE",
                "seat": s["seat"],
                "path": f"{s['state_root']}/harness.pid",
                "detail": "pidfile points at dead PID; book flat — safe clear candidate",
                "requires_flag": "AUTO_MAINTAIN",
            })
        if s["health"] == "DEAD" and s.get("open_n", 0) == 0:
            props.append({
                "action": "PROPOSE_RESTART",
                "seat": s["seat"],
                "detail": "DEAD + flat book — propose restart via /root/restart_seat.sh; do NOT auto without GO",
                "auto": False,
            })
        if s["health"] == "DEAD" and s.get("open_n", 0) > 0:
            props.append({
                "action": "ALERT_ONLY",
                "seat": s["seat"],
                "detail": "DEAD with open book — never auto-restart; exchange SoT + Dayo GO required",
            })
        if s["health"] == "DUPLICATE":
            props.append({
                "action": "ALERT_ONLY",
                "seat": s["seat"],
                "detail": f"DUPLICATE pids={s.get('pids')} — kill by PROP_STATE_ROOT match, never trust pidfile",
            })
    return props


def score_intent(seats: list[dict[str, Any]], shared: dict[str, Any]) -> dict[str, Any]:
    """Fix-vs-intent scorecard (D1→D3 + standing oracles)."""
    alerts: list[str] = []
    notes: list[str] = []

    # D1 loop_error
    d1_403 = 0
    d1_bv = 0
    stale_ok_seats = 0
    for s in seats:
        if s.get("designed_dead"):
            continue
        lc = s.get("loop_error_classes") or {}
        d1_403 += int(lc.get("html_403") or 0)
        d1_bv += int(lc.get("book_view_tmp") or 0)
        if s["health"] in ("STALE", "DEAD"):
            stale_ok_seats += 1
            alerts.append(f"D1/HB: {s['seat']} health={s['health']} hb_age={s.get('hb_age_s')}")
    if d1_403 >= 20:
        alerts.append(f"D1: html_403 burst n={d1_403} across seats (shared egress/API plane)")
    elif d1_403 > 0:
        notes.append(f"D1: html_403 n={d1_403} (sub-burst)")
    if d1_bv > 0:
        alerts.append(f"D1: book_view.tmp Errno-2 n={d1_bv} (multi-writer race still live)")
    else:
        notes.append("D1: book_view.tmp race silent this window")

    # D2 metal veto
    metal = shared.get("metal") or {}
    for side in ("sell", "buy"):
        v = metal.get(side) or {}
        if v.get("alert"):
            alerts.append(
                f"D2: metal|{side} veto re-armed without material PnL "
                f"(abs_pnl={v.get('abs_pnl')})"
            )
        elif v.get("present") and not v.get("veto"):
            notes.append(
                f"D2: metal|{side} released veto={v.get('veto')} "
                f"immaterial={v.get('veto_suppressed_immaterial')}"
            )
    metal_released = all(
        (metal.get(side) or {}).get("present") and not (metal.get(side) or {}).get("veto")
        for side in ("sell", "buy")
    )
    for s in seats:
        if s.get("seat") == "ns_metal":
            recent_v = s.get("metal_veto_refuses_recent_30m") or 0
            window_v = s.get("metal_veto_refuses") or 0
            if metal_released and recent_v > 0:
                alerts.append(
                    f"D2: ns_metal still refusing loss veto metal|* in last 30m "
                    f"n={recent_v} while artifact released"
                )
            elif window_v > 0:
                notes.append(
                    f"D2: ns_metal loss-veto refuses in 4h window n={window_v} "
                    f"(recent_30m={recent_v}; pre-release noise OK if artifact released)"
                )
            if s.get("gold_symbol_min_coh_refuses", 0) > 0:
                notes.append(
                    f"D2 residual: GOLD/XAUT symbol_min_coh_fire n="
                    f"{s['gold_symbol_min_coh_refuses']} (KEEP 4.0 OK)"
                )
            gf = s.get("gold_floor") or {}
            if gf and not gf.get("ok"):
                alerts.append(f"GOLD floor BREACH: {gf.get('notes')} — restore ≥{GOLD_KEEP}")
            elif gf:
                notes.append(f"GOLD floor KEEP: {gf.get('notes')}")

    # D3 slot_reserve — until reject payloads carry open/free, only alert on
    # contemporaneous refuses while CURRENT geometry says free>reserve.
    for s in seats:
        if s.get("seat") != PRIMARY_CRYPTO:
            continue
        geom = s.get("geometry") or {}
        refuses = s.get("slot_reserve_refuses") or 0
        recent = s.get("slot_reserve_refuses_recent_30m") or 0
        if recent and geom.get("ok") and not geom.get("probe_must_standdown"):
            alerts.append(
                f"D3: turbo slot_reserve refuses_30m={recent} but free={geom.get('free')} "
                f"> reserve={geom.get('reserve')} — false standdown vs live geometry"
            )
        notes.append(
            f"D3 geometry truth: max={geom.get('max')} open={geom.get('open')} "
            f"free={geom.get('free')} reserve={geom.get('reserve')} "
            f"refuses_4h={refuses} refuses_30m={recent} "
            f"(enrichment pending — historical refuses may predate MAX bump)"
        )

    # Company opposite turbo ↔ ns_crypto
    exposures: list[tuple[str, str, str]] = []
    for s in seats:
        for o in s.get("opens") or []:
            exposures.append((s["seat"], str(o.get("asset") or ""), str(o.get("side") or "")))
    opps = opposite_pairs(exposures)
    # focus primary↔neighbour
    company = [
        o for o in opps
        if PRIMARY_CRYPTO in (o["buy_seats"] + "," + o["sell_seats"])
        and NEIGHBOUR_CRYPTO in (o["buy_seats"] + "," + o["sell_seats"])
    ]
    if company:
        for o in company:
            alerts.append(
                f"COMPANY: opposite {o['asset']} buys={o['buy_seats']} sells={o['sell_seats']} "
                f"(ops soft-refuse neighbour; do not flatten primary)"
            )
    else:
        notes.append("COMPANY: no turbo↔ns_crypto opposite opens this snapshot")

    # turbo2
    for s in seats:
        if s.get("seat") == "turbo2":
            notes.append(f"turbo2: health={s['health']} (LOCKED/dead by design — do not heal)")

    severity = "OK"
    if any(s["health"] in ("DEAD", "DUPLICATE", "STALE") and not s.get("designed_dead") for s in seats):
        severity = "DEGRADED"
    if alerts:
        severity = "DEGRADED" if severity == "OK" else severity
    if any(s["health"] == "DEAD" and not s.get("designed_dead") and s.get("open_n", 0) > 0 for s in seats):
        severity = "CRITICAL"

    return {
        "severity": severity,
        "alerts": alerts,
        "notes": notes,
        "d1": {"html_403": d1_403, "book_view_tmp": d1_bv, "stale_or_dead": stale_ok_seats},
        "company_opposites": company,
        "all_opposites": opps,
    }


def render_report(
    *,
    meta: dict[str, Any],
    seats: list[dict[str, Any]],
    shared: dict[str, Any],
    intent: dict[str, Any],
    proposals: list[dict[str, Any]],
) -> str:
    lines: list[str] = []
    lines.append(f"# Prop fix-intent monitor — {meta['ts_utc']}")
    lines.append("")
    lines.append(f"**Severity:** `{intent['severity']}`  ")
    lines.append(f"**Host:** `{meta.get('host')}`  ")
    lines.append(f"**DEPLOYED_SHA:** `{meta.get('deployed_sha')}` (expected `{EXPECTED_SHA}`)  ")
    lines.append(f"**Window:** last {WINDOW_S // 3600}h  ")
    lines.append(f"**Doctrine:** policies-before-code · ARIA=signal · harness=fascia · eval≈$5k/$150  ")
    lines.append("")
    lines.append("## Alerts")
    lines.append("")
    if intent["alerts"]:
        for a in intent["alerts"]:
            lines.append(f"- ⚠ {a}")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    for n in intent["notes"]:
        lines.append(f"- {n}")
    lines.append("")
    lines.append("## Seats")
    lines.append("")
    lines.append("| Seat | Health | PID(s) | HB age | Open | Max | Band | Equity | Headroom | Realized |")
    lines.append("|------|--------|--------|--------|------|-----|------|--------|----------|----------|")
    for s in seats:
        dd = s.get("dd") or {}
        bud = s.get("budget") or {}
        lines.append(
            f"| {s['seat']} | {s['health']} | {','.join(map(str, s.get('pids') or [])) or '—'} | "
            f"{s.get('hb_age_s')} | {s.get('open_n')} | {s.get('max_positions')} | "
            f"{dd.get('band')} | {dd.get('equity')} | {dd.get('headroom')} | {bud.get('realized')} |"
        )
    lines.append("")
    lines.append("## D1 loop_error classes (window)")
    lines.append("")
    for s in seats:
        if s.get("designed_dead") and not s.get("loop_error_classes"):
            continue
        lines.append(f"- **{s['seat']}**: `{s.get('loop_error_classes')}`")
    lines.append("")
    lines.append("## D2 metal loss_veto (shared)")
    lines.append("")
    lines.append(f"```json\n{json.dumps(shared.get('metal'), indent=2)}\n```")
    lines.append("")
    lines.append("## D3 turbo slot geometry")
    lines.append("")
    for s in seats:
        if s["seat"] == PRIMARY_CRYPTO:
            lines.append(f"```json\n{json.dumps(s.get('geometry'), indent=2)}\n```")
            lines.append(f"slot_reserve_refuses={s.get('slot_reserve_refuses')}")
    lines.append("")
    lines.append("## Opens / company book")
    lines.append("")
    for s in seats:
        if s.get("opens"):
            lines.append(f"- **{s['seat']}**: {s['opens']}")
    lines.append(f"- opposites: `{intent.get('all_opposites')}`")
    lines.append("")
    lines.append("## Maintain proposals (whitelist)")
    lines.append("")
    if not proposals:
        lines.append("- (none)")
    else:
        for p in proposals:
            lines.append(f"- `{p.get('action')}` seat={p.get('seat')} — {p.get('detail')}")
    lines.append("")
    lines.append("## MUST-NOT")
    lines.append("")
    lines.append("- No Kant / Nietzsche / Chancellor / tiers 1–6 edits")
    lines.append("- No open-book auto-restart; turbo2 LOCKED = do not heal")
    lines.append("- No GOLD floor < 4.0; no M5 / fear-scratch; journals permanent")
    lines.append("- Telegram optional — cycle must succeed without keys")
    lines.append("")
    lines.append("---")
    lines.append("*Deterministic collector only. Cursor subscribe_timer agent judges this report.*")
    return "\n".join(lines) + "\n"


def maybe_telegram(report_path: Path, severity: str) -> dict[str, Any]:
    """Optional adapter: no-op unless telegram.env present + TELEGRAM_ENABLED=1."""
    root = report_path.parent
    env_path = root / "telegram.env"
    if not env_path.is_file():
        return {"sent": False, "reason": "no_telegram_env"}
    env = _parse_env_file(env_path)
    # re-read allowing token only for send — still never log it
    raw = {}
    for line in env_path.read_text(errors="replace").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            raw[k.strip()] = v.strip()
    if raw.get("TELEGRAM_ENABLED", "0") != "1":
        return {"sent": False, "reason": "TELEGRAM_ENABLED!=1"}
    token = raw.get("TELEGRAM_BOT_TOKEN") or raw.get("BOT_TOKEN")
    chat = raw.get("TELEGRAM_CHAT_ID") or raw.get("CHAT_ID")
    if not token or not chat:
        return {"sent": False, "reason": "missing_token_or_chat"}
    # Prefer alerting only on non-OK
    if severity == "OK" and raw.get("TELEGRAM_ON_OK", "0") != "1":
        return {"sent": False, "reason": "ok_suppressed"}
    try:
        import urllib.parse
        import urllib.request
        text = f"prop_fix_monitor {severity}\n{report_path}\n(see report.md)"
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(url, data=data, timeout=10)  # noqa: S310
        return {"sent": True}
    except Exception as exc:
        return {"sent": False, "reason": f"send_failed:{type(exc).__name__}"}


def apply_maintain(
    proposals: list[dict[str, Any]],
    monitor_root: Path,
) -> list[dict[str, Any]]:
    """At most one safe action if AUTO_MAINTAIN flag exists."""
    flag = monitor_root / "AUTO_MAINTAIN"
    if not flag.is_file():
        return [{"applied": False, "reason": "no_AUTO_MAINTAIN_flag"}]
    actions = []
    for p in proposals:
        if p.get("action") != "CLEAR_STALE_PIDFILE":
            continue
        path = Path(p["path"])
        try:
            if path.is_file():
                path.unlink()
                actions.append({"applied": True, "action": p["action"], "path": str(path)})
                break  # max one per cycle
        except Exception as exc:
            actions.append({"applied": False, "action": p["action"], "error": str(exc)})
            break
    if not actions:
        actions.append({"applied": False, "reason": "no_safe_clear_candidate"})
    return actions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--monitor-root", type=Path, default=DEFAULT_MONITOR_ROOT)
    ap.add_argument("--state-parent", type=Path, default=DEFAULT_STATE_PARENT)
    ap.add_argument("--aria-root", type=Path, default=DEFAULT_ARIA_ROOT)
    ap.add_argument("--window-s", type=int, default=WINDOW_S)
    ap.add_argument("--strict", action="store_true")
    args = ap.parse_args()

    monitor_root: Path = args.monitor_root
    monitor_root.mkdir(parents=True, exist_ok=True)

    if (monitor_root / "DISABLED").is_file():
        msg = f"{_utc_now()} DISABLED — no-op\n"
        (monitor_root / "report.md").write_text(
            f"# Prop fix-intent monitor DISABLED\n\nKill switch present: `{monitor_root}/DISABLED`\n"
        )
        with (monitor_root / "cycles.jsonl").open("a") as fh:
            fh.write(json.dumps({"ts": time.time(), "status": "DISABLED"}) + "\n")
        print(msg.strip())
        return 0

    cut = time.time() - args.window_s
    pids = _live_pids_by_root()
    seats_paths = _discover_seats(args.state_parent)
    seats = [collect_seat(p, cut_ts=cut, pids_by_root=pids) for p in seats_paths]

    demo = args.state_parent / "prop_harness_state"
    shared = collect_shared(demo if demo.is_dir() else seats_paths[0])
    intent = score_intent(seats, shared)
    proposals = propose_maintain(seats)
    applied = apply_maintain(proposals, monitor_root)
    tg = {"sent": False, "reason": "deferred"}

    sha_path = args.aria_root / DEPLOYED_SHA_PATH
    deployed = sha_path.read_text().strip() if sha_path.is_file() else None
    meta = {
        "ts_utc": _utc_now(),
        "host": os.uname().nodename if hasattr(os, "uname") else "unknown",
        "deployed_sha": deployed,
        "expected_sha": EXPECTED_SHA,
        "sha_match": deployed == EXPECTED_SHA if deployed else None,
        "window_s": args.window_s,
        "monitor_root": str(monitor_root),
    }

    report = render_report(
        meta=meta, seats=seats, shared=shared, intent=intent, proposals=proposals
    )
    report_path = monitor_root / "report.md"
    report_path.write_text(report)

    verdict = {
        "meta": meta,
        "severity": intent["severity"],
        "alerts": intent["alerts"],
        "notes": intent["notes"],
        "d1": intent["d1"],
        "company_opposites": intent["company_opposites"],
        "proposals": proposals,
        "applied": applied,
        "seats": [
            {
                "seat": s["seat"],
                "health": s["health"],
                "pids": s["pids"],
                "hb_age_s": s["hb_age_s"],
                "open_n": s["open_n"],
                "opens": s["opens"],
                "geometry": s["geometry"],
                "loop_error_classes": s["loop_error_classes"],
                "gold_floor": s.get("gold_floor"),
            }
            for s in seats
        ],
        "shared_metal": shared.get("metal"),
    }
    (monitor_root / "verdict.json").write_text(json.dumps(verdict, indent=2) + "\n")
    with (monitor_root / "cycles.jsonl").open("a") as fh:
        fh.write(json.dumps({
            "ts": time.time(),
            "ts_utc": meta["ts_utc"],
            "severity": intent["severity"],
            "alerts_n": len(intent["alerts"]),
            "sha": deployed,
            "applied": applied,
        }) + "\n")

    tg = maybe_telegram(report_path, intent["severity"])
    (monitor_root / "last_telegram.json").write_text(json.dumps(tg, indent=2) + "\n")

    print(f"{meta['ts_utc']} severity={intent['severity']} alerts={len(intent['alerts'])} "
          f"report={report_path} telegram={tg.get('reason') or tg.get('sent')}")
    if args.strict and intent["severity"] == "CRITICAL":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
