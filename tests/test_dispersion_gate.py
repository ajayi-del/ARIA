"""Dispersion gate self-move exemption (2026-08-27): three vol-aware legs
(Raschke fast / Clenow-Carver z / Murphy rank) + Steenbarger participation
veto replace the flat 8% bar. All-None evidence = legacy bit-for-bit."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.dispersion_gate import DispersionGate, self_move_leg  # noqa: E402

G = DispersionGate()
LOW = 0.010   # below LOW_DISP


# ── Legacy bit-for-bit (no evidence) ────────────────────────────────────────

def test_legacy_low_disp_rejects_alt():
    ok, reason = G.should_trade("SOL-USD", LOW)
    assert ok is False
    assert reason == "low_dispersion_0.01_alts_no_edge"


def test_legacy_large_cap_passes():
    assert G.should_trade("BTC-USD", LOW)[0] is True
    assert G.should_trade("BTC-USD", LOW)[1] == "large_cap_always_ok"


def test_legacy_non_crypto_exempt():
    ok, reason = G.should_trade("XAUT-USD", LOW, asset_category="commodity_precious")
    assert (ok, reason) == (True, "non_crypto_exempt")


def test_legacy_campaign_bypass():
    ok, reason = G.should_trade("SPCX-USD", LOW, campaign_symbol="SPCX-USD")
    assert (ok, reason) == (True, "campaign_symbol_bypass")


def test_legacy_mid_and_high_unchanged():
    assert G.should_trade("SOL-USD", 0.02)[1] == "mid_dispersion_normal"
    ok, reason = G.should_trade("SOL-USD", 0.05, leading_sector="defi",
                                asset_category="alt_l1")
    assert ok is False and reason.startswith("high_dispersion_not_leader_")


# ── Fast leg (Raschke expansion trigger) ────────────────────────────────────

def test_fast_leg_fires():
    ok, reason = G.should_trade("LIT-USD", LOW, ret_1h_pct=1.2,
                                sigma_1h_pct=0.5, vol_ratio=2.5)
    assert ok is True and reason == "self_move_exempt_fast_z2.40"


def test_fast_leg_direction_agnostic():
    ok, _ = G.should_trade("LIT-USD", LOW, ret_1h_pct=-1.3,
                           sigma_1h_pct=0.5, vol_ratio=2.5)
    assert ok is True


def test_fast_leg_blocked_on_thin_volume():
    ok, reason = G.should_trade("LIT-USD", LOW, ret_1h_pct=1.2,
                                sigma_1h_pct=0.5, vol_ratio=1.9)
    assert ok is False and reason.startswith("low_dispersion_")


def test_fast_leg_blocked_below_sigma():
    ok, _ = G.should_trade("LIT-USD", LOW, ret_1h_pct=0.9,
                           sigma_1h_pct=0.5, vol_ratio=3.0)
    assert ok is False


def test_fast_leg_sigma_floor_prevents_z_explosion():
    # dead book σ→0: floored at 0.10 — a 0.15% 1h move must NOT exempt
    ok, _ = G.should_trade("DOS-USD", LOW, ret_1h_pct=0.15,
                           sigma_1h_pct=0.0, vol_ratio=3.0)
    assert ok is False
    ok, _ = G.should_take if False else G.should_trade(
        "DOS-USD", LOW, ret_1h_pct=0.25, sigma_1h_pct=0.0, vol_ratio=3.0)
    assert ok is True


# ── Vol-z leg (Clenow/Carver, Brownian √t) ──────────────────────────────────

def test_z_leg_fires():
    # σ_elapsed = 3.5 × √(21600/86400) = 1.75; z = 6.0/1.75 ≈ 3.43
    ok, reason = G.should_trade("SOL-USD", LOW, day_move_pct=6.0,
                                day_elapsed_s=21600.0, daily_sigma_pct=3.5,
                                vol_ratio=1.6)
    assert ok is True and reason == "self_move_exempt_z3.43"


def test_z_leg_vetoed_on_dying_volume():
    ok, _ = G.should_trade("SOL-USD", LOW, day_move_pct=6.0,
                           day_elapsed_s=21600.0, daily_sigma_pct=3.5,
                           vol_ratio=1.2)
    assert ok is False


def test_z_leg_dead_first_hour():
    ok, _ = G.should_trade("SOL-USD", LOW, day_move_pct=9.0,
                           day_elapsed_s=1800.0, daily_sigma_pct=3.5,
                           vol_ratio=3.0)
    assert ok is False


def test_z_leg_daily_sigma_floor():
    # σ daily floored at 0.5: σ_elapsed = 0.5 × √0.25 = 0.25 → z = 1.5/0.25 = 6
    ok, _ = G.should_trade("PAXG-USD", LOW, day_move_pct=1.5,
                           day_elapsed_s=21600.0, daily_sigma_pct=0.2,
                           vol_ratio=2.0)
    assert ok is True


# ── Rank leg (Murphy relative strength) ─────────────────────────────────────

def test_rank_leg_fires():
    ok, reason = G.should_trade("BONK-USD", LOW, move_rank_pctile=0.93,
                                vol_ratio=2.0)
    assert ok is True and reason == "self_move_exempt_rank0.93"


def test_rank_leg_fail_closed_without_volume():
    ok, _ = G.should_trade("BONK-USD", LOW, move_rank_pctile=0.95,
                           vol_ratio=None)
    assert ok is False


def test_rank_leg_below_decile():
    ok, _ = G.should_trade("BONK-USD", LOW, move_rank_pctile=0.85,
                           vol_ratio=3.0)
    assert ok is False


# ── Leg precedence + exemptions still win first ──────────────────────────────

def test_fast_leg_precedes_z_leg():
    _, reason = G.should_trade(
        "SOL-USD", LOW, ret_1h_pct=1.2, sigma_1h_pct=0.5, vol_ratio=2.5,
        day_move_pct=6.0, day_elapsed_s=21600.0, daily_sigma_pct=3.5)
    assert reason.startswith("self_move_exempt_fast")


def test_large_cap_never_needs_evidence():
    ok, reason = G.should_trade("ETH-USD", LOW)
    assert (ok, reason) == (True, "large_cap_always_ok")


def test_non_crypto_never_needs_evidence():
    ok, reason = G.should_trade("TSLA-USD", LOW, asset_category="equity",
                                vol_ratio=None)
    assert (ok, reason) == (True, "non_crypto_exempt")


# ── self_move_leg pure function pins ────────────────────────────────────────

def test_self_move_leg_empty_evidence():
    assert self_move_leg(None, 0.0, None, None, None, None, None) == ""


def test_self_move_leg_fast_requires_both():
    assert self_move_leg(None, 0.0, None, 1.2, 0.5, None, None) == ""
    assert self_move_leg(None, 0.0, None, 1.2, 0.5, 2.5, None) == "fast_z2.40"
