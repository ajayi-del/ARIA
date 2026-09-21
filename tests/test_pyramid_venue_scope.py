"""Pyramid venue-scope warmup counter (CEO DIR PYRAMID-VENUE-SCOPE 2026-09-20).

The legacy counter read 0.25 in 1,017/1,017 blocks: 1 of 4 legs live, the
0.80 bar above the counter's support (OI ring + HV ring memory-only, 7 boots
in 4.4d). The repair venue-scopes the funding leg (the venue the position
trades on, never the Bybit signal plane), abstains the OI leg explicitly
(no SoDEX/Aster OI plane wired), and persists ONLY the fed HV leg across
boots. WARMUP_MIN_FRAC untouched.
"""
import json
import os

import main as m


# ── _pyramid_warmup_legs: support + bar arithmetic ──────────────────────

def test_warmup_support_full():
    warm, flags = m._pyramid_warmup_legs(0.0001, 0.7, 6.5)
    assert warm == 1.0
    assert flags == {"fr_ok": True, "rvr_ok": True,
                     "oi_ok": "abstain_no_venue_feed", "coh_ok": True}


def test_warmup_support_empty():
    warm, flags = m._pyramid_warmup_legs(None, None, None)
    assert warm == 0.0
    assert flags["fr_ok"] is False and flags["rvr_ok"] is False
    assert flags["coh_ok"] is False
    assert flags["oi_ok"] == "abstain_no_venue_feed"


def test_warmup_support_partial_values():
    assert m._pyramid_warmup_legs(0.1, None, None)[0] == 1 / 3.0
    assert m._pyramid_warmup_legs(0.1, 0.5, None)[0] == 2 / 3.0


def test_warmup_bar_still_demands_all_legs():
    """WARMUP_MIN_FRAC 0.80 is untouched: 2/3 defers, 3/3 passes. The OI leg
    can never again pin the counter below the bar — but no partial credit
    either."""
    from intelligence.pyramid import WARMUP_MIN_FRAC
    assert WARMUP_MIN_FRAC == 0.80  # DIR: do NOT lower the bar
    assert (2 / 3.0) < WARMUP_MIN_FRAC
    assert not (1.0 < WARMUP_MIN_FRAC)


def test_warmup_zero_values_count_as_present():
    """0.0 is a real funding read / real rv_rank — only None abstains."""
    warm, flags = m._pyramid_warmup_legs(0.0, 0.0, 0.0)
    assert warm == 1.0
    assert flags["fr_ok"] is True


# ── _pyramid_venue_funding: venue-scope, no cross-venue bleed ───────────

def test_aster_position_reads_aster_plane():
    fr = m._pyramid_venue_funding(
        "XMR-USD", "aster",
        {"XMR-USD": 0.111},                       # SoDEX map (wrong venue)
        {"XMR-USD": {"funding_rate": 0.0002}},    # Aster mark payload
        {"XMR-USD": {"funding_rate": 0.333}},     # Bybit ticker (signal)
    )
    assert fr == 0.0002


def test_sodex_position_reads_sodex_plane():
    fr = m._pyramid_venue_funding(
        "BTC-USD", "sodex",
        {"BTC-USD": 0.0001},
        {"BTC-USD": {"funding_rate": 0.222}},
        {"BTC-USD": {"funding_rate": 0.333}},
    )
    assert fr == 0.0001


def test_missing_venue_value_abstains_no_bybit_fallthrough():
    """The DIR's core rule: venue or abstain — the Bybit signal plane must
    NOT rescue a missing venue read."""
    fr = m._pyramid_venue_funding(
        "WLD-USD", "aster", {}, {}, {"WLD-USD": {"funding_rate": 0.5}})
    assert fr is None
    fr2 = m._pyramid_venue_funding(
        "BTC-USD", "sodex", {}, {}, {"BTC-USD": {"funding_rate": 0.5}})
    assert fr2 is None


def test_legacy_mode_keeps_bybit_fallback():
    fr = m._pyramid_venue_funding(
        "XMR-USD", "aster", {}, {}, {"XMR-USD": {"funding_rate": 0.5}},
        venue_scope=False)
    assert fr == 0.5
    fr2 = m._pyramid_venue_funding(
        "BTC-USD", "sodex", {"BTC-USD": 0.0001}, {}, {},
        venue_scope=False)
    assert fr2 == 0.0001


def test_funding_garbage_abstains():
    assert m._pyramid_venue_funding(
        "X", "aster", {"X": "not-a-float"}, {"X": {"funding_rate": "bad"}},
        None) is None


# ── HV ring persistence: the fed leg survives boots ─────────────────────

def test_hv_hist_round_trip(tmp_path):
    p = str(tmp_path / "hv.json")
    saved = m._BC_HV_HIST
    try:
        m._BC_HV_HIST = {"BTC-USD": [(1000.0, 0.42), (1900.0, 0.43)],
                         "XMR-USD": [(1000.0, 0.9)]}
        m._bc_hv_hist_save(p)
        m._BC_HV_HIST = {}
        m._bc_hv_hist_load(p)
        assert m._BC_HV_HIST == {"BTC-USD": [(1000.0, 0.42), (1900.0, 0.43)],
                                 "XMR-USD": [(1000.0, 0.9)]}
        # tuples restored (append site unpacks `for _, v in ring`)
        assert isinstance(m._BC_HV_HIST["BTC-USD"][0], tuple)
    finally:
        m._BC_HV_HIST = saved


def test_hv_hist_load_missing_file_noop(tmp_path):
    saved = m._BC_HV_HIST
    try:
        m._BC_HV_HIST = {"A": [(1.0, 1.0)]}
        m._bc_hv_hist_load(str(tmp_path / "nope.json"))
        assert m._BC_HV_HIST == {"A": [(1.0, 1.0)]}  # untouched
    finally:
        m._BC_HV_HIST = saved


def test_hv_hist_load_corrupt_file_noop(tmp_path):
    p = tmp_path / "hv.json"
    p.write_text("{not json")
    saved = m._BC_HV_HIST
    try:
        m._BC_HV_HIST = {"A": [(1.0, 1.0)]}
        m._bc_hv_hist_load(str(p))
        assert m._BC_HV_HIST == {"A": [(1.0, 1.0)]}
    finally:
        m._BC_HV_HIST = saved


def test_hv_hist_load_non_dict_noop(tmp_path):
    p = tmp_path / "hv.json"
    p.write_text(json.dumps([1, 2, 3]))
    saved = m._BC_HV_HIST
    try:
        m._BC_HV_HIST = {"A": [(1.0, 1.0)]}
        m._bc_hv_hist_load(str(p))
        assert m._BC_HV_HIST == {"A": [(1.0, 1.0)]}
    finally:
        m._BC_HV_HIST = saved


def test_hv_hist_load_one_bad_line_doctrine(tmp_path):
    p = tmp_path / "hv.json"
    p.write_text(json.dumps({
        "GOOD": [[1000.0, 0.5], ["bad-row"], [1900.0, 0.6]],
        "BADSYM": "not-a-list",
        "EMPTY": [],
    }))
    saved = m._BC_HV_HIST
    try:
        m._bc_hv_hist_load(str(p))
        assert m._BC_HV_HIST == {"GOOD": [(1000.0, 0.5), (1900.0, 0.6)]}
    finally:
        m._BC_HV_HIST = saved


def test_hv_hist_load_prunes_to_96(tmp_path):
    p = tmp_path / "hv.json"
    p.write_text(json.dumps({"BTC-USD": [[float(i), 0.1] for i in range(150)]}))
    saved = m._BC_HV_HIST
    try:
        m._bc_hv_hist_load(str(p))
        assert len(m._BC_HV_HIST["BTC-USD"]) == 96
        assert m._BC_HV_HIST["BTC-USD"][0] == (54.0, 0.1)  # newest 96 kept
    finally:
        m._BC_HV_HIST = saved


# ── Legacy counter (kill-switch off) is bit-for-bit the 0.25 trap ───────

def test_legacy_counter_reproduces_the_stuck_025():
    """Pin the defect the DIR repairs: with only the funding leg live the
    legacy 4-leg counter reads exactly 0.25 < 0.80 forever."""
    fr, rvr, oi, coh = 0.0001, None, None, None
    warm = sum(1 for x in (fr, rvr, oi, coh) if x is not None) / 4.0
    assert warm == 0.25
    from intelligence.pyramid import WARMUP_MIN_FRAC
    assert warm < WARMUP_MIN_FRAC


def test_kill_switch_default_on():
    assert m.PYRAMID_VENUE_SCOPE_ENABLED is True
