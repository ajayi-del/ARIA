"""
tests/test_min_viable_tp.py — pins for intelligence/min_viable_tp.py

Covers: worked sanity cells, fee schedule derived from core/fee_engine's
actual constants (with and without the SOSO 5% discount), negative-expectancy
flagging (incl. the exact-breakeven edge wr*(1+1/rr) <= 1/rr), degenerate
inputs (rr<=0, wr outside [0,1], rt<0), fee_ratio retirement rule, table
shape, and the CLI renderer.
"""
import pytest

from intelligence.min_viable_tp import (
    DEFAULT_SOSO_STAKED,
    FEE_RATIO_RETIRE,
    RR_AXIS,
    WR_AXIS,
    breakeven_wr,
    build_table,
    default_fee_schedule,
    expectancy_bps,
    fee_ratio,
    main,
    min_viable_tp_bps,
    render_table,
)


# ── Worked sanity cells (Governor's arithmetic, pinned) ─────────────────────

def test_sanity_taker_8bp_rr2_wr50():
    assert min_viable_tp_bps(8.0, 2.0, 0.5) == pytest.approx(32.0)


def test_sanity_maker_2_4bp_rr2_wr50():
    assert min_viable_tp_bps(2.4, 2.0, 0.5) == pytest.approx(9.6)


def test_sanity_mixed_5_2bp_rr2_wr50():
    assert min_viable_tp_bps(5.2, 2.0, 0.5) == pytest.approx(20.8)


# ── Fee schedule from fee_engine's ACTUAL constants ─────────────────────────

def test_live_schedule_with_soso_discount():
    """ARIA live: Tier 0 + 168 SOSO -> 5% off -> 7.60 / 2.28 / 4.94 bp RT."""
    s = default_fee_schedule(soso_staked=168.0, tier=0)
    assert s["taker"] == pytest.approx(7.60)
    assert s["maker"] == pytest.approx(2.28)
    assert s["mixed_maker_taker"] == pytest.approx(4.94)


def test_schedule_without_discount_matches_governor_grid():
    """No SOSO -> raw Tier-0 grid 8.0 / 2.4 / 5.2 bp RT."""
    s = default_fee_schedule(soso_staked=0.0, tier=0)
    assert s["taker"] == pytest.approx(8.0)
    assert s["maker"] == pytest.approx(2.4)
    assert s["mixed_maker_taker"] == pytest.approx(5.2)


def test_default_soso_constant_is_live_state():
    assert DEFAULT_SOSO_STAKED == 168.0


def test_schedule_rejects_bad_inputs():
    with pytest.raises(ValueError):
        default_fee_schedule(soso_staked=-1.0)
    with pytest.raises(ValueError):
        default_fee_schedule(tier=99)


# ── Viability / negative-expectancy flagging ────────────────────────────────

def test_breakeven_wr_formula():
    assert breakeven_wr(1.5) == pytest.approx(0.4)
    assert breakeven_wr(2.0) == pytest.approx(1.0 / 3.0)
    assert breakeven_wr(3.0) == pytest.approx(0.25)


def test_exact_breakeven_edge_is_non_viable():
    """wr*(1+1/rr) == 1 exactly (rr=1.5, wr=0.40): expectancy = -rt at ALL tp."""
    assert min_viable_tp_bps(7.6, 1.5, 0.40) is None
    assert min_viable_tp_bps(2.28, 1.5, 0.40) is None
    # and expectancy is strictly negative even at absurd tp
    assert expectancy_bps(1e6, 7.6, 1.5, 0.40) < 0


def test_below_breakeven_non_viable():
    """The '1:2/50%-and-worse' class: anything at or under wr = 1/(1+rr)."""
    assert min_viable_tp_bps(7.6, 2.0, 1.0 / 3.0) is None      # exact edge
    assert min_viable_tp_bps(7.6, 2.0, 0.30) is None           # below edge
    assert min_viable_tp_bps(7.6, 3.0, 0.20) is None


def test_above_breakeven_viable():
    tp = min_viable_tp_bps(7.6, 1.5, 0.45)
    assert tp is not None and tp > 0
    # expectancy is exactly zero at the minimum viable tp
    assert expectancy_bps(tp, 7.6, 1.5, 0.45) == pytest.approx(0.0, abs=1e-9)
    # and positive beyond it
    assert expectancy_bps(2 * tp, 7.6, 1.5, 0.45) > 0


def test_expectancy_negative_below_min_tp():
    tp = min_viable_tp_bps(7.6, 2.0, 0.5)
    assert expectancy_bps(tp / 2, 7.6, 2.0, 0.5) < 0


def test_wr_one_is_viable():
    tp = min_viable_tp_bps(7.6, 2.0, 1.0)
    assert tp == pytest.approx(7.6)  # only fees to cover


# ── Degenerate inputs ───────────────────────────────────────────────────────

@pytest.mark.parametrize("rr", [0.0, -1.0, -2.5])
def test_rr_nonpositive_raises(rr):
    with pytest.raises(ValueError):
        min_viable_tp_bps(7.6, rr, 0.5)
    with pytest.raises(ValueError):
        expectancy_bps(20.0, 7.6, rr, 0.5)
    with pytest.raises(ValueError):
        breakeven_wr(rr)


@pytest.mark.parametrize("wr", [-0.01, 1.01, -1.0, 2.0])
def test_wr_outside_unit_interval_raises(wr):
    with pytest.raises(ValueError):
        min_viable_tp_bps(7.6, 2.0, wr)
    with pytest.raises(ValueError):
        expectancy_bps(20.0, 7.6, 2.0, wr)


def test_negative_rt_raises():
    with pytest.raises(ValueError):
        min_viable_tp_bps(-1.0, 2.0, 0.5)
    with pytest.raises(ValueError):
        expectancy_bps(20.0, -1.0, 2.0, 0.5)
    with pytest.raises(ValueError):
        fee_ratio(20.0, -1.0)


# ── fee_ratio retirement rule ───────────────────────────────────────────────

def test_fee_ratio_basic():
    assert fee_ratio(32.0, 8.0) == pytest.approx(0.25)
    assert fee_ratio(32.0, 8.0) <= FEE_RATIO_RETIRE


def test_fee_ratio_retirement_boundary():
    # 7.6bp RT against a 24bp planned TP = 0.3167 -> retire (> 0.30)
    assert fee_ratio(24.0, 7.6) == pytest.approx(7.6 / 24.0)
    assert fee_ratio(24.0, 7.6) > FEE_RATIO_RETIRE


def test_fee_ratio_zero_planned_raises():
    with pytest.raises(ValueError):
        fee_ratio(0.0, 7.6)
    with pytest.raises(ValueError):
        fee_ratio(-5.0, 7.6)


# ── build_table ─────────────────────────────────────────────────────────────

def test_build_table_shape_and_flags():
    schedule = default_fee_schedule()
    table = build_table(schedule)
    assert set(table) == {"taker", "maker", "mixed_maker_taker"}
    flagged = []
    for mode in table:
        assert set(table[mode]) == set(RR_AXIS)
        for rr in RR_AXIS:
            assert set(table[mode][rr]) == set(WR_AXIS)
            for wr in WR_AXIS:
                cell = table[mode][rr][wr]
                if not cell["viable"]:
                    flagged.append((mode, rr, wr))
                    assert cell["min_tp_bps"] is None
                else:
                    assert cell["min_tp_bps"] > 0
    # Only the exact-breakeven (rr=1.5, wr=0.40) cell is flagged, once per mode.
    assert sorted(flagged) == [
        ("maker", 1.5, 0.40),
        ("mixed_maker_taker", 1.5, 0.40),
        ("taker", 1.5, 0.40),
    ]


def test_build_table_live_sanity_cells():
    """The Governor's 32/9.6/20.8 cells under the live 5%-discount schedule."""
    table = build_table(default_fee_schedule())
    assert table["taker"][2.0][0.5]["min_tp_bps"] == pytest.approx(
        7.6 / (0.5 - 0.5 / 2.0))        # = 30.4
    assert table["maker"][2.0][0.5]["min_tp_bps"] == pytest.approx(
        2.28 / (0.5 - 0.5 / 2.0))       # = 9.12
    assert table["mixed_maker_taker"][2.0][0.5]["min_tp_bps"] == pytest.approx(
        4.94 / (0.5 - 0.5 / 2.0))       # = 19.76


# ── CLI / renderer ──────────────────────────────────────────────────────────

def test_render_table_dense_lines_and_flag_text():
    schedule = default_fee_schedule()
    out = render_table(build_table(schedule), schedule)
    lines = out.strip().splitlines()
    # 4 header lines (incl. the funding/slippage scope disclaimer) + 45 cells
    assert len(lines) == 4 + 3 * len(RR_AXIS) * len(WR_AXIS)
    assert "NON-VIABLE" in out
    assert "funding/slippage/spread NOT in RT" in out  # scope said out loud
    assert "taker=7.60" in out and "maker=2.28" in out and "mixed_maker_taker=4.94" in out


def test_cli_main_prints_table(capsys):
    main()
    out = capsys.readouterr().out
    assert "min_viable_tp" in out
    assert out.count("NON-VIABLE") == 3
