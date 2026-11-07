"""Budget + drawdown brain tests — the survival registers."""

import pytest

from prop_harness.brains.budget import BudgetBrain
from prop_harness.brains.drawdown import BANDS, DrawdownBrain


@pytest.fixture
def budget(tmp_path):
    return BudgetBrain(tmp_path / "budget_day.json")


class TestBudget:
    def test_day_one_budget(self, budget):
        budget.roll(headroom=150.0)
        assert budget.state.budget == 15.0

    def test_never_bankrupt_recursion(self, budget):
        headroom = 150.0
        for _ in range(15):
            budget.roll(headroom)
            if budget.state.budget <= 0:
                break
            headroom -= budget.state.budget
            # simulate day passing
            budget.state.day = "1900-01-01"
        assert headroom > 0.0, f"recursion bankrupt: headroom ${headroom:.2f}"
        budget.roll(headroom)
        if headroom <= 30.0:
            assert budget.state.budget == 0.0

    def test_hard_zero(self, budget):
        budget.roll(headroom=30.0)
        assert budget.state.budget == 0.0
        assert budget.risk_cap(1.0) == 0.0
        budget.roll(headroom=29.9)
        assert budget.risk_cap(1.0) == 0.0

    def test_risk_cap_registers(self, budget):
        budget.roll(headroom=150.0)
        assert budget.risk_cap(1.0) == 5.0       # r_base
        assert budget.risk_cap(2.0) == 7.5       # r_max clamp
        assert budget.risk_cap(0.6) == 3.0       # band mult
        budget.commit(12.0)
        assert budget.risk_cap(2.0) == 3.0       # budget remaining clamp
        budget.release(12.0, -12.0)
        assert budget.state.realized == -12.0

    def test_no_state_abstains(self, tmp_path):
        brain = BudgetBrain(tmp_path / "fresh.json")
        assert brain.risk_cap(1.0) == 0.0


@pytest.fixture
def drawdown(tmp_path):
    return DrawdownBrain(tmp_path / "dd.json", floor_line=4850.0, floor_usd=150.0)


class TestDrawdown:
    def test_band_ladder(self, drawdown):
        cases = [
            (5000.0, "ACTIVE", 150.0),
            (4930.0, "CAUTIOUS", 80.0),
            (4910.0, "DEFENSIVE", 60.0),
            (4890.0, "SURVIVAL", 40.0),
            (4865.0, "LOCKED", 15.0),
            (4850.0, "BREACHED", 0.0),
        ]
        for equity, band, headroom in cases:
            state, _ = drawdown.update(equity)
            assert state.band == band, f"equity {equity}: {state.band} != {band}"
            assert state.headroom == pytest.approx(headroom)

    def test_mult_zero_when_locked(self, drawdown):
        drawdown.update(4865.0)
        assert drawdown.mult == 0.0
        drawdown.update(5000.0)
        assert drawdown.mult == 1.0

    def test_transition_reported(self, drawdown):
        _, t1 = drawdown.update(5000.0)
        assert t1 is None  # no prior state
        _, t2 = drawdown.update(4930.0)
        assert t2 == "ACTIVE->CAUTIOUS"

    def test_breach_flag(self, drawdown):
        state, _ = drawdown.update(4849.0)
        assert state.breached and state.band == "BREACHED"

    def test_band_table_covers_internal_floor(self):
        # LOCKED must start at used=120 == headroom $30 (quant hard zero)
        locked = [b for b in BANDS if b[0] == "LOCKED"][0]
        assert locked[1] == 120.0
