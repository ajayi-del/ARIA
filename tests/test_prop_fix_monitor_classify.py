"""Pins for tools/prop_fix_monitor/classify.py (pure, no Vultr I/O)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools" / "prop_fix_monitor"))

from classify import (  # noqa: E402
    classify_loop_error,
    gold_floor_ok,
    metal_veto_verdict,
    opposite_pairs,
    parse_symbol_min_coh_map,
    slot_geometry,
)


def test_classify_loop_error_403_and_book_view():
    assert classify_loop_error("[403] None: <html>Forbidden</html>") == "html_403"
    assert classify_loop_error(
        "[Errno 2] No such file or directory: '/root/prop_harness_state/program/book_view.json.tmp'"
    ) == "book_view_tmp"
    assert classify_loop_error("something else") == "other"


def test_gold_floor_keep_4():
    m = parse_symbol_min_coh_map("XAUT:4.0,GOLD:4.0")
    assert gold_floor_ok(m)["ok"] is True
    bad = parse_symbol_min_coh_map("XAUT:3.5,GOLD:3.5")
    assert gold_floor_ok(bad)["ok"] is False


def test_slot_geometry_probe_standdown():
    g = slot_geometry(4, 3, 1)
    assert g["free"] == 1
    assert g["probe_must_standdown"] is True
    g2 = slot_geometry(8, 2, 1)
    assert g2["probe_must_standdown"] is False


def test_opposite_pairs_company():
    opps = opposite_pairs([
        ("turbo", "BTC", "sell"),
        ("ns_crypto", "BTC", "buy"),
        ("turbo", "HYPE", "sell"),
    ])
    assert len(opps) == 1
    assert opps[0]["asset"] == "BTC"


def test_metal_veto_immaterial_rearm_alerts():
    released = metal_veto_verdict({
        "veto": False, "veto_suppressed_immaterial": True, "pnl": -0.95, "n": 5,
    })
    assert released["ok"] is True
    assert released["alert"] is False
    rearmed = metal_veto_verdict({"veto": True, "pnl": -0.95, "n": 5})
    assert rearmed["alert"] is True
    material = metal_veto_verdict({"veto": True, "pnl": -91.0, "n": 5})
    assert material["alert"] is False
    assert material["ok"] is True
