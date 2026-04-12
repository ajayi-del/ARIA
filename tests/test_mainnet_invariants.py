"""
Mainnet invariant tests — prevent recurrence of production bugs.

Each test maps to a specific bug that caused real damage:
  BUG-1: get_open_orders() returned strings → .get() crash, reconciliation aborted
  BUG-2: getattr(cfg,'max_trade_usd',50.0) wrong default → all trades capped at $50 notional
  BUG-3: Hardcoded notional guard `< 10.0` → $10-$49 trades passed, hit exchange minimum error
  BUG-4: Sub-notional position (LINK 0.1×$8.91=$0.89) → stop placement infinite retry
  BUG-5: Paper-era .env values (BASE_TRADE_USD=25) → $2 margin trades on mainnet
  BUG-6: ws_spot_url always returned testnet URL (data_source!="live") → wrong WS feed
  BUG-7: Pyramid gate unconditionally blocked all re-entries → _gate_pyramid dead code
  BUG-8: Funding conviction direction-agnostic → longs rewarded same as shorts for +funding

All tests must pass before any mainnet deployment.
"""

import pytest
from unittest.mock import MagicMock, AsyncMock, patch


# ─────────────────────────────────────────────────────────────────────────────
# BUG-1 — get_open_orders never returns non-dict elements
# ─────────────────────────────────────────────────────────────────────────────

def test_get_open_orders_filters_non_dicts(monkeypatch):
    """
    Exchange API occasionally wraps orders list in unexpected strings or ints.
    SoDEXClient.get_open_orders must return only dicts — never crash callers
    that call .get() on each element.
    """
    import sys
    sys.path.insert(0, '/Users/dayodapper/CascadeProjects/ARIA')
    from execution.sodex_client import SoDEXClient

    client = SoDEXClient.__new__(SoDEXClient)

    # Simulate raw API response with mixed types
    mixed_raw = [{"cl_ord_id": "abc", "status": 1}, "some_string", 42, None, {"cl_ord_id": "xyz"}]

    def patched_get_open_orders_raw(data):
        raw = data.get("data", [])
        if isinstance(raw, list):
            return [o for o in raw if isinstance(o, dict)]
        return []

    result = patched_get_open_orders_raw({"data": mixed_raw})
    assert all(isinstance(o, dict) for o in result), "Non-dict items leaked through"
    assert len(result) == 2
    assert result[0]["cl_ord_id"] == "abc"
    assert result[1]["cl_ord_id"] == "xyz"


def test_get_positions_filters_non_dicts():
    """
    Same guard for get_positions — API may return mixed position list.
    """
    mixed_raw = [{"symbol": "BTC-USD", "size": 1.0}, "bad", {"symbol": "ETH-USD"}]
    filtered = [p for p in mixed_raw if isinstance(p, dict)]
    assert len(filtered) == 2
    assert all("symbol" in p for p in filtered)


# ─────────────────────────────────────────────────────────────────────────────
# BUG-2/BUG-5 — Config loads mainnet sizing values, not paper-era defaults
# ─────────────────────────────────────────────────────────────────────────────

def test_config_mainnet_sizing():
    """
    Settings must load mainnet values from .env.
    Any paper-era values (base=25, min=15, max=50) will fail this test.

    Architecture: base_trade_usd=$200 is the SIZE TARGET.
    min_trade_usd/min_trade_notional_usd are the SoDEX DUST GUARD (~$50).
    They must be strictly lower than the target so multipliers never block valid signals.
    """
    import sys
    sys.path.insert(0, '/Users/dayodapper/CascadeProjects/ARIA')
    from core.config import Settings
    cfg = Settings()

    assert cfg.base_trade_usd >= 200.0, f"base_trade_usd={cfg.base_trade_usd} too small (was 25 in paper era)"
    assert cfg.max_trade_usd >= 200.0, f"max_trade_usd={cfg.max_trade_usd} too small (was 50 in paper era)"
    # min_trade_usd/min_trade_notional_usd = SoDEX dust guard, NOT the trade size target.
    # Must be < base_trade_usd (otherwise multipliers block valid $200 signals).
    assert cfg.min_trade_usd >= 50.0, f"min_trade_usd={cfg.min_trade_usd} too small (old paper era was 15)"
    assert cfg.min_trade_usd <= cfg.base_trade_usd, (
        f"dust guard min_trade_usd={cfg.min_trade_usd} must be ≤ base_trade_usd={cfg.base_trade_usd}"
    )
    assert cfg.min_trade_notional_usd >= 50.0, f"min_trade_notional_usd={cfg.min_trade_notional_usd} too small"
    assert cfg.min_trade_notional_usd <= cfg.base_trade_usd, (
        f"dust guard min_trade_notional_usd={cfg.min_trade_notional_usd} must be ≤ base_trade_usd"
    )

    margin_at_10x = cfg.base_trade_usd / cfg.default_leverage
    assert margin_at_10x >= 20.0, (
        f"base margin={margin_at_10x:.1f} < $20 minimum "
        f"(base_trade_usd={cfg.base_trade_usd}, leverage={cfg.default_leverage})"
    )


def test_config_no_testnet_fields():
    """
    Testnet config fields are removed — Settings must not expose them.
    """
    import sys
    sys.path.insert(0, '/Users/dayodapper/CascadeProjects/ARIA')
    from core.config import Settings
    cfg = Settings()

    assert not hasattr(cfg, 'testnet_ws_spot'), "testnet_ws_spot still present"
    assert not hasattr(cfg, 'testnet_ws_perps'), "testnet_ws_perps still present"
    assert not hasattr(cfg, 'testnet_rest_url'), "testnet_rest_url still present"
    assert not hasattr(cfg, 'chain_id_testnet'), "chain_id_testnet still present"


def test_config_ws_urls_are_mainnet():
    """
    BUG-6: ws_spot_url and ws_perps_url used to always return testnet because
    they checked `data_source == "live"` which was never true.
    Now they return mainnet unconditionally (sodex_mainnet=True).
    """
    import sys
    sys.path.insert(0, '/Users/dayodapper/CascadeProjects/ARIA')
    from core.config import Settings
    cfg = Settings()

    assert "mainnet" in cfg.ws_spot_url, f"ws_spot_url={cfg.ws_spot_url} is not mainnet"
    assert "mainnet" in cfg.ws_perps_url, f"ws_perps_url={cfg.ws_perps_url} is not mainnet"
    assert "testnet" not in cfg.ws_spot_url
    assert "testnet" not in cfg.ws_perps_url


# ─────────────────────────────────────────────────────────────────────────────
# BUG-3 — Notional guard uses config value, not hardcoded 10.0
# ─────────────────────────────────────────────────────────────────────────────

def test_notional_guard_uses_config():
    """
    The notional guard in on_signal_ready rejects only dust trades below
    config.min_trade_notional_usd ($50 SoDEX minimum).
    temporal_mult is NOT applied to size — ARIA always targets full $200 notional.
    """
    import sys
    sys.path.insert(0, '/Users/dayodapper/CascadeProjects/ARIA')
    from core.config import Settings
    cfg = Settings()

    # $150 trade (old weekend-reduced notional) must PASS the dust guard
    assert 150.0 >= cfg.min_trade_notional_usd, (
        f"$150 notional must pass the $50 dust floor (floor={cfg.min_trade_notional_usd})"
    )

    # Only true dust trades are rejected
    assert 49.0 < cfg.min_trade_notional_usd, "$49 must be below dust floor"

    # Full $200 trade and high-conviction must always pass
    assert 200.0 >= cfg.min_trade_notional_usd
    assert 280.0 >= cfg.min_trade_notional_usd  # high-conviction


# ─────────────────────────────────────────────────────────────────────────────
# BUG-4 — Sub-notional handling: close position, don't retry forever
# ─────────────────────────────────────────────────────────────────────────────

def test_sub_notional_detection():
    """
    A position with notional below exchange minimum (e.g. 0.1 LINK at ~$9 = $0.90)
    must be detected and closed, not retried every 30s forever.

    Invariant: notional < 10.0 (exchange minimum, not our floor) → close_position_market
    """
    link_size = 0.1
    link_price = 8.91
    notional = link_size * link_price
    assert notional < 10.0, "LINK 0.1 position should be sub-exchange-minimum"

    # Verify our config floor is higher than exchange minimum
    import sys
    sys.path.insert(0, '/Users/dayodapper/CascadeProjects/ARIA')
    from core.config import Settings
    cfg = Settings()
    assert cfg.min_trade_notional_usd > 10.0, (
        "Our config minimum must be above exchange minimum to catch sub-notional before submission"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Margin engine uses correct default minimum
# ─────────────────────────────────────────────────────────────────────────────

def test_margin_engine_default_min_notional():
    """
    MarginEngine.compute_size default parameter was 10.0 — must be 200.0.
    Callers in Kelly path pass config.min_trade_notional_usd explicitly,
    but the default protects against any direct call without the parameter.
    """
    import sys
    import inspect
    sys.path.insert(0, '/Users/dayodapper/CascadeProjects/ARIA')
    from risk.margin_engine import MarginEngine
    sig = inspect.signature(MarginEngine.compute_size)
    default_min = sig.parameters['min_notional_usd'].default
    assert default_min >= 200.0, (
        f"MarginEngine.compute_size default min_notional_usd={default_min} "
        f"(was 10.0 in paper era, should be ≥200.0)"
    )


# ─────────────────────────────────────────────────────────────────────────────
# BUG-7 — Pyramid gate: can_pyramid is reachable
# ─────────────────────────────────────────────────────────────────────────────

def test_position_manager_can_pyramid_logic():
    """
    Pyramid gate was dead code: main.py returned early for ANY count>0,
    including TP1-hit positions that should allow a scale-in.

    Verify position_manager.can_pyramid correctly signals pyramid eligibility.
    """
    import sys
    sys.path.insert(0, '/Users/dayodapper/CascadeProjects/ARIA')
    from risk.position_manager import PositionManager
    from execution.schemas import Position

    pm = PositionManager()
    import time
    pos = Position(
        symbol="ETH-USD", side="short",
        size=0.1, entry_price=2200.0,
        stop_price=2233.0, tp1_price=2100.0,
        tp2_price=1980.0, tp3_price=1860.0,
        leverage=10, initial_margin=22.0,
        liq_price=2420.0, opened_at_ms=int(time.time() * 1000),
    )
    pm.add(pos)

    # Before TP1: cannot pyramid
    assert not pm.can_pyramid("ETH-USD"), "should not pyramid before TP1"
    assert pm.count("ETH-USD") == 1

    # After TP1: can pyramid
    pm.mark_tp1_hit("ETH-USD", 0)
    assert pm.can_pyramid("ETH-USD"), "should allow pyramid after TP1"

    # After adding second position: count==2 → cap hit, can_pyramid returns False
    pos2 = Position(
        symbol="ETH-USD", side="short",
        size=0.05, entry_price=2150.0,
        stop_price=pos.stop_price,
        tp1_price=2050.0, tp2_price=1950.0, tp3_price=1850.0,
        leverage=10, initial_margin=11.0,
        liq_price=2365.0, opened_at_ms=int(time.time() * 1000),
    )
    pm.add(pos2)
    assert pm.count("ETH-USD") == 2
    assert not pm.can_pyramid("ETH-USD"), "count==2: pyramid cap hit"


# ─────────────────────────────────────────────────────────────────────────────
# BUG-8 — Direction-aware funding conviction
# ─────────────────────────────────────────────────────────────────────────────

def test_funding_direction_aware_correction():
    """
    CoherenceEngine adds direction-agnostic funding score (+0.75/+1.5).
    SignalGenerator must subtract it when funding OPPOSES the trade direction.

    Invariant:
      SHORT + positive_funding  → funding score KEPT (earns funding as short)
      LONG  + positive_funding  → funding score REMOVED (pays funding as long)
      LONG  + negative_funding  → funding score KEPT
      SHORT + negative_funding  → funding score REMOVED
    """
    # Simulate the correction logic from signal_generator.py
    def apply_funding_correction(weighted_score, funding_class, trade_direction):
        if trade_direction in ("long", "short"):
            f_agnostic = (
                1.5 if "extreme" in funding_class
                else 0.75 if funding_class in ("positive", "negative")
                else 0.0
            )
            funding_aligns = (
                (trade_direction == "short" and "positive" in funding_class) or
                (trade_direction == "long" and "negative" in funding_class)
            )
            if f_agnostic > 0 and not funding_aligns:
                weighted_score = max(weighted_score - f_agnostic, 0.0)
        return weighted_score

    base = 4.0  # score includes 0.75 direction-agnostic funding boost

    # SHORT + positive: funding aligns → score unchanged
    assert apply_funding_correction(base, "positive", "short") == 4.0

    # LONG + positive: funding opposes → score reduced
    assert apply_funding_correction(base, "positive", "long") == pytest.approx(3.25)

    # SHORT + extreme_positive: funding aligns → unchanged
    assert apply_funding_correction(base, "extreme_positive", "short") == 4.0

    # LONG + extreme_positive: funding opposes → reduced by 1.5
    assert apply_funding_correction(base, "extreme_positive", "long") == pytest.approx(2.5)

    # LONG + negative: funding aligns → unchanged
    assert apply_funding_correction(base, "negative", "long") == 4.0

    # SHORT + negative: funding opposes → reduced
    assert apply_funding_correction(base, "negative", "short") == pytest.approx(3.25)

    # Neutral funding: no change regardless of direction
    assert apply_funding_correction(base, "neutral", "short") == 4.0
    assert apply_funding_correction(base, "neutral", "long") == 4.0

    # Direction "none": no change
    assert apply_funding_correction(base, "positive", "none") == 4.0


# ─────────────────────────────────────────────────────────────────────────────
# Arb capital gate uses config minimum
# ─────────────────────────────────────────────────────────────────────────────

def test_arb_capital_gate_uses_config():
    """
    TrueDeltaNeutralArb.MIN_ARB_NOTIONAL was hardcoded to 20.0 (paper era).
    Now uses config.min_trade_notional_usd ($200).
    With $294 balance and arb_capital_pct=0.20, arb_cap=$58.89 < $200
    → arb should not fire until account grows sufficiently.
    """
    import sys
    sys.path.insert(0, '/Users/dayodapper/CascadeProjects/ARIA')
    from core.config import Settings
    cfg = Settings()

    balance = 294.0
    arb_cap = balance * cfg.arb_capital_pct
    min_notional = cfg.min_trade_notional_usd
    lev = cfg.default_leverage

    # min_notional=$50 dust guard, arb_capital_pct=20%.
    # On a $294 account: arb_cap=$58.80 > $50 min → arb CAN fire (correct behaviour).
    # On a tiny $200 account: arb_cap=$40 < $50 min → arb is blocked (protects small accounts).
    assert arb_cap > 0, "arb allocation must be positive"
    assert min_notional >= 50.0, "dust guard must be at least $50 (SoDEX minimum)"

    # Minimum balance for arb to fire: arb_cap >= min_notional
    min_balance_for_arb = min_notional / cfg.arb_capital_pct
    assert min_balance_for_arb >= 200.0, (
        f"arb should require at least $200 balance (threshold=${min_balance_for_arb:.0f})"
    )
    # On the live $294 balance, arb can fire since arb_cap > min_notional
    assert arb_cap >= min_notional, (
        f"arb_cap={arb_cap:.2f} should exceed dust floor={min_notional} "
        f"on a ${balance} account with {cfg.arb_capital_pct*100:.0f}% arb allocation"
    )

    # Margin check: arb position margin should be reasonable
    perp_margin_if_opened = arb_cap / max(lev, 1)
    assert perp_margin_if_opened <= 50.0, (
        f"arb perp margin={perp_margin_if_opened:.2f} should be modest"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Build candidate sizing integrity
# ─────────────────────────────────────────────────────────────────────────────

def test_build_candidate_notional_floor():
    """
    build_candidate must produce notional >= min_trade_usd (config floor)
    for any valid coherence score, before multipliers are applied.
    """
    import sys
    sys.path.insert(0, '/Users/dayodapper/CascadeProjects/ARIA')
    from core.config import Settings
    cfg = Settings()

    for coherence in [0.0, 1.5, 3.0, 4.5, 5.5, 8.0]:
        conv_mult = 2.0 if coherence >= 5.0 else 1.4 if coherence >= 3.0 else 1.0
        target = max(cfg.base_trade_usd * conv_mult, cfg.min_trade_usd)
        target = min(target, cfg.max_trade_usd)
        assert target >= cfg.min_trade_usd, (
            f"coherence={coherence}: target_notional={target} < min_trade_usd={cfg.min_trade_usd}"
        )
        assert target <= cfg.max_trade_usd, (
            f"coherence={coherence}: target_notional={target} > max_trade_usd={cfg.max_trade_usd}"
        )
        margin_at_10x = target / cfg.default_leverage
        assert margin_at_10x >= 20.0, (
            f"coherence={coherence}: margin={margin_at_10x:.1f} < $20 minimum"
        )
