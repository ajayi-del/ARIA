#!/usr/bin/env python3
"""reward_actuals — weekly harvest-venue actuals tracker (observer-class).

Answers: "what did each routed dollar actually earn?" across points-program
venues. The HARVEST ledger (fees paid vs rewards banked) is kept strictly
separate from the alpha book's WR/EV stats — net_usd here is
rewards_cash − fees, never a trading-PnL number (realized pnl rides along as
an informational field only, never enters net_usd).

Epoch convention: Tuesday 00:00 UTC → Monday 23:59:59.999 UTC (the Ondo /
SoDEX points-week convention). Rows are keyed (epoch_start, venue).

Data sources (all fail-closed, one-bad-line doctrine):
  - logs/volume_ledger.jsonl — the fee-governor pool ledger (per-fill rows:
    ts, notional_usd, fee_usd, realized_pnl_usd, pool). Reused as the SoDEX
    volume/fee counter — never recomputed from the journal.
  - logs/reward_manual_entries.jsonl — Governor-manual reward events:
    {"ts", "venue", "kind": "box_opened|usdc_payout|points_snapshot",
     "amount_usd"?, "points"?, "program"?, "notes"?}
    ts = epoch seconds (int/float) or ISO-8601. SoDEX treasure boxes are
    opened manually, SoPoints have no confirmed API — both land here.

SoDEX treasure boxes: 1 box per $10,000 cumulative effective volume.
boxes.earned_cumulative = floor(lifetime_ledger_volume / 10_000);
boxes.delta = boxes crossed inside this epoch. Box $ value enters only via
manual box_opened entries (the Governor opens them by hand).

Phase 1 scope: SoDEX actuals computed from the ledger; ondo/extended/vooi
(and any unknown venue) emit skeleton rows from manual entries only until
their lanes are live. The schema admits their rows without migration.

Idempotent: re-running the same epoch upserts — existing (epoch_start,
venue) lines are replaced, never duplicated. Output rewrite is atomic
(tmp + os.replace). Exit code always 0 (best-effort observer doctrine).

Usage:
  python3 tools/reward_actuals.py                    # current epoch, write
  python3 tools/reward_actuals.py --epoch 2026-09-29 # specific epoch anchor
  python3 tools/reward_actuals.py --no-write         # print only
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

LOG_DIR = Path(REPO_ROOT) / "logs"
LEDGER_PATH = LOG_DIR / "volume_ledger.jsonl"
MANUAL_PATH = LOG_DIR / "reward_manual_entries.jsonl"
OUT_PATH = LOG_DIR / "reward_actuals.jsonl"

VOLUME_PER_BOX_USD = 10_000.0
SODEX_VENUE = "sodex"
CASH_KINDS = {"box_opened", "usdc_payout"}
POINTS_KINDS = {"points_snapshot"}
# Venue → default points program name (nullable points until APIs exist).
DEFAULT_POINTS_PROGRAM = {
    "sodex": "sopoints",
    "ondo": "ondo_points",
    "extended": "extended_points",
    "vooi": "vooi_points",
}

BERLIN = timezone(timedelta(hours=2), name="Europe/Berlin")


# ── epoch math ─────────────────────────────────────────────────────────────

def epoch_bounds(now_ts: float) -> tuple[float, float]:
    """[start, end) of the points week containing now_ts.
    Weeks anchor Tuesday 00:00 UTC. A now exactly at Tue 00:00 starts a NEW
    epoch; Mon 23:59:59.999 belongs to the epoch that began the prior Tue."""
    dt = datetime.fromtimestamp(float(now_ts), timezone.utc)
    # weekday(): Mon=0 ... Sun=6; Tuesday=1
    days_since_tue = (dt.weekday() - 1) % 7
    anchor = datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc) - timedelta(
        days=days_since_tue
    )
    start = anchor.timestamp()
    return start, start + 7 * 86400.0


def _parse_ts(raw) -> float | None:
    """Epoch seconds or ISO-8601 → epoch seconds. None on garbage (caller
    skips — one-bad-line doctrine)."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(
                raw.replace("Z", "+00:00")
            ).timestamp()
        except ValueError:
            return None
    return None


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(ts)))


# ── data reads (one-bad-line, fail-closed) ─────────────────────────────────

def read_ledger_rows(path: Path) -> tuple[list[dict], int, bool]:
    """Returns (rows, skipped, file_present). Missing file = empty honest
    zero, NOT an error — but the row is flagged so the record says so."""
    rows: list[dict] = []
    skipped = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    skipped += 1
                    continue
                if not isinstance(row, dict):
                    skipped += 1
                    continue
                ts = _parse_ts(row.get("ts"))
                if ts is None:
                    skipped += 1
                    continue
                try:
                    rows.append({
                        "ts": ts,
                        "notional_usd": float(row.get("notional_usd") or 0.0),
                        "fee_usd": float(row.get("fee_usd") or 0.0),
                        "realized_pnl_usd": float(
                            row.get("realized_pnl_usd") or 0.0),
                    })
                except (TypeError, ValueError):
                    skipped += 1
                    continue
    except FileNotFoundError:
        return rows, skipped, False
    except Exception:
        return rows, skipped, False
    return rows, skipped, True


def read_manual_entries(path: Path) -> tuple[list[dict], int]:
    """Manual reward events. Unknown venues pass through untouched."""
    entries: list[dict] = []
    skipped = 0
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    skipped += 1
                    continue
                if not isinstance(row, dict):
                    skipped += 1
                    continue
                ts = _parse_ts(row.get("ts"))
                venue = row.get("venue")
                kind = row.get("kind")
                if ts is None or not venue or not kind:
                    skipped += 1
                    continue
                entries.append({
                    "ts": ts,
                    "venue": str(venue).strip().lower(),
                    "kind": str(kind).strip(),
                    "amount_usd": row.get("amount_usd"),
                    "points": row.get("points"),
                    "program": row.get("program"),
                    "notes": row.get("notes"),
                })
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return entries, skipped


# ── per-venue record assembly ──────────────────────────────────────────────

def compute_epoch_record(
    venue: str,
    epoch_start: float,
    epoch_end: float,
    as_of: float,
    ledger_rows: list[dict] | None,
    manual_entries: list[dict],
    computed_at: float,
    ledger_present: bool = True,
) -> dict:
    """One venue's weekly actuals row. Pure — all inputs injected.

    epoch_start/epoch_end define the full week (display + bucketing); as_of
    is the accumulation cutoff (== now for an in-progress epoch, == epoch_end
    for a closed one) so a live week never reads ahead.

    ledger_rows=None → no ledger lane for this venue (skeleton row from
    manual entries only; the ondo/extended/vooi pre-lane shape)."""
    cutoff = min(as_of, epoch_end)
    in_epoch_vol = in_epoch_fees = in_epoch_pnl = 0.0
    cum_through_end = cum_through_start = 0.0
    notes: list[str] = []

    if ledger_rows is not None:
        if not ledger_present:
            notes.append("ledger_missing")
        for r in ledger_rows:
            ts = r["ts"]
            if ts < cutoff:
                cum_through_end += r["notional_usd"]
            if ts < epoch_start:
                cum_through_start += r["notional_usd"]
            if epoch_start <= ts < cutoff:
                in_epoch_vol += r["notional_usd"]
                in_epoch_fees += r["fee_usd"]
                in_epoch_pnl += r["realized_pnl_usd"]

    rewards_cash = 0.0
    points_val = None
    points_program = DEFAULT_POINTS_PROGRAM.get(venue)
    boxes_opened = 0
    for e in manual_entries:
        if e["venue"] != venue or not (epoch_start <= e["ts"] < epoch_end):
            continue
        if e["kind"] in CASH_KINDS:
            try:
                rewards_cash += float(e.get("amount_usd") or 0.0)
            except (TypeError, ValueError):
                pass
            if e["kind"] == "box_opened":
                boxes_opened += 1
        elif e["kind"] in POINTS_KINDS:
            if e.get("program"):
                points_program = str(e["program"])
            try:
                points_val = float(e["points"]) if e.get("points") is not None else points_val
            except (TypeError, ValueError):
                pass

    fee_per_100k = (
        round(in_epoch_fees / in_epoch_vol * 100_000.0, 4)
        if in_epoch_vol > 0 else None
    )

    boxes = None
    if venue == SODEX_VENUE:
        earned_cum = math.floor(cum_through_end / VOLUME_PER_BOX_USD)
        earned_at_start = math.floor(cum_through_start / VOLUME_PER_BOX_USD)
        boxes = {
            "earned_cumulative": int(earned_cum),
            "delta": int(earned_cum - earned_at_start),
            "opened_manual_count": boxes_opened,
            "volume_per_box_usd": int(VOLUME_PER_BOX_USD),
        }

    rec = {
        "epoch_start": _iso(epoch_start),
        "epoch_end": _iso(epoch_end - 1.0),  # Mon 23:59:59Z display bound
        "computed_at": _iso(computed_at),
        "venue": venue,
        "volume_usd": round(in_epoch_vol, 2),
        "fees_usd": round(in_epoch_fees, 4),
        "fee_per_100k": fee_per_100k,
        "rewards_cash_usd": round(rewards_cash, 4),
        "rewards_points": {"program": points_program, "points": points_val},
        "boxes": boxes,
        # Informational only — HARVEST ledger stays separate from the alpha
        # book's WR/EV stats; net_usd NEVER includes trading pnl.
        "realized_pnl_usd_info": round(in_epoch_pnl, 4),
        "net_usd": round(rewards_cash - in_epoch_fees, 4),
        "notes": ";".join(notes) if notes else None,
    }
    return rec


def build_records(
    epoch_start: float,
    epoch_end: float,
    as_of: float,
    ledger_rows: list[dict],
    ledger_present: bool,
    manual_entries: list[dict],
    computed_at: float,
) -> list[dict]:
    """sodex always (its ledger lane is live); any venue with a manual entry
    inside the epoch gets a row — unknown venues pass through as skeletons."""
    venues = {SODEX_VENUE}
    for e in manual_entries:
        if epoch_start <= e["ts"] < epoch_end:
            venues.add(e["venue"])
    records = []
    for venue in sorted(venues):
        lane = ledger_rows if venue == SODEX_VENUE else None
        records.append(compute_epoch_record(
            venue, epoch_start, epoch_end, as_of, lane, manual_entries,
            computed_at, ledger_present=ledger_present,
        ))
    return records


# ── idempotent output ──────────────────────────────────────────────────────

def upsert_records(out_path: Path, records: list[dict]) -> tuple[int, int]:
    """Replace existing (epoch_start, venue) lines with the fresh records;
    append the rest. Atomic tmp+replace. Returns (written, replaced)."""
    keys = {(r["epoch_start"], r["venue"]) for r in records}
    kept: list[str] = []
    replaced = 0
    try:
        with open(out_path, "r", encoding="utf-8") as fh:
            for line in fh:
                s = line.strip()
                if not s:
                    continue
                try:
                    row = json.loads(s)
                except Exception:
                    kept.append(line.rstrip("\n"))  # one-bad-line: keep as-is
                    continue
                if isinstance(row, dict) and (
                    row.get("epoch_start"), row.get("venue")
                ) in keys:
                    replaced += 1
                    continue
                kept.append(line.rstrip("\n"))
    except FileNotFoundError:
        pass
    except Exception:
        pass
    out_lines = kept + [json.dumps(r, separators=(",", ":")) for r in records]
    tmp = str(out_path) + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write("\n".join(out_lines) + ("\n" if out_lines else ""))
        os.replace(tmp, out_path)
    except Exception as _e:
        print(f"reward_actuals_write_failed: {str(_e)[:120]}", file=sys.stderr)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:
            pass
        return 0, 0
    return len(records), replaced


# ── stdout table (Governor-visible: Berlin timestamps) ─────────────────────

def print_table(records: list[dict], computed_at: float) -> None:
    berlin_now = datetime.fromtimestamp(computed_at, BERLIN)
    print(f"reward_actuals — computed {berlin_now:%Y-%m-%d %H:%M:%S} Berlin "
          f"({_iso(computed_at)})")
    if not records:
        print("  (no venue records)")
        return
    hdr = (f"{'epoch_start':<12}{'venue':<10}{'volume_usd':>12}{'fees_usd':>10}"
           f"{'fee/100k':>10}{'rewards$':>10}{'net_usd':>10}{'boxes':>7} {'points'}")
    print(hdr)
    print("-" * len(hdr))
    for r in records:
        boxes = r.get("boxes")
        boxes_s = (f"{boxes['earned_cumulative']}+{boxes['delta']}"
                   if boxes else "-")
        pts = r.get("rewards_points") or {}
        pts_s = ("-" if pts.get("points") is None
                 else f"{pts.get('program')}:{pts.get('points')}")
        fee100 = r.get("fee_per_100k")
        print(f"{r['epoch_start'][:10]:<12}{r['venue']:<10}"
              f"{r['volume_usd']:>12.2f}{r['fees_usd']:>10.4f}"
              f"{(f'{fee100:.2f}' if fee100 is not None else '-'):>10}"
              f"{r['rewards_cash_usd']:>10.4f}{r['net_usd']:>10.4f}"
              f"{boxes_s:>7} {pts_s}")
        if r.get("notes"):
            print(f"  notes: {r['notes']}")


# ── main ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Weekly harvest-venue reward actuals")
    ap.add_argument("--epoch", metavar="YYYY-MM-DD",
                    help="Tuesday anchor date of the epoch to compute "
                         "(default: current epoch)")
    ap.add_argument("--no-write", action="store_true",
                    help="print only, do not touch logs/reward_actuals.jsonl")
    ap.add_argument("--ledger", default=str(LEDGER_PATH))
    ap.add_argument("--manual", default=str(MANUAL_PATH))
    ap.add_argument("--out", default=str(OUT_PATH))
    args = ap.parse_args(argv)

    now = time.time()
    if args.epoch:
        try:
            anchor = datetime.strptime(args.epoch, "%Y-%m-%d").replace(
                tzinfo=timezone.utc)
        except ValueError:
            print(f"bad --epoch {args.epoch!r}: want YYYY-MM-DD", file=sys.stderr)
            return 0  # best-effort doctrine: never non-zero
        epoch_start, epoch_end = epoch_bounds(anchor.timestamp() + 1.0)
        as_of = epoch_end  # closed epoch: accumulate through the full week
    else:
        epoch_start, epoch_end = epoch_bounds(now)
        as_of = now  # live week: never read ahead of now

    ledger_rows, _lskipped, ledger_present = read_ledger_rows(Path(args.ledger))
    manual_entries, _mskipped = read_manual_entries(Path(args.manual))

    records = build_records(epoch_start, epoch_end, as_of, ledger_rows,
                            ledger_present, manual_entries, now)

    print_table(records, now)
    if not args.no_write:
        written, replaced = upsert_records(Path(args.out), records)
        print(f"reward_actuals.jsonl: wrote {written} venue row(s) for epoch "
              f"{_iso(epoch_start)[:10]} ({replaced} replaced)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
