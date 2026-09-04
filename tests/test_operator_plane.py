"""Pins for the operator-trades observability plane (2026-09-03/04 operator
directives: manual trades run ASIDE ARIA's — "ARIA's ROE and TP or baskets
should NOT touch my trades; it's observatory only")."""
import os

_MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "main.py")


def _src():
    with open(_MAIN) as f:
        return f.read()


def test_universe_firewall_observes_never_adopts():
    src = _src()
    i_fw = src.index("if sym not in config.assets:")
    block = src[i_fw:i_fw + 600]
    assert "_observe_operator_position(sym, size, pos_data)" in block
    assert "continue" in block  # never reaches the adoption branch


def test_kill_switch_present():
    src = _src()
    assert "OPERATOR_TRADES_TELEMETRY_ENABLED" in src


def test_close_detection_drains_registry():
    src = _src()
    i = src.index("operator_position_closed")
    block = src[max(0, i - 700):i + 900]
    assert "_operator_pos_open.pop" in block
    assert "not in exchange_open" in block
    assert "_operator_pos_last.pop" in block


def test_side_parse_shapes():
    # The closure is not importable; pin the accepted dialect shapes instead
    # so a refactor keeps long/buy/1 and short/sell/2 and signed-size fallback.
    src = _src()
    i = src.index("def _observe_operator_position")
    block = src[i:i + 2200]
    for needle in ('"long", "buy", "1"', '"short", "sell", "2"',
                   'startswith("-")', "avgEntryPrice", "entryPrice",
                   "unrealizedPnl", "leverage"):
        assert needle in block, needle


def test_observatory_events_are_warnings_on_state_change():
    src = _src()
    assert 'logger.warning("operator_position_observed"' in src
    assert 'logger.info("operator_position_update"' in src
    assert 'logger.warning("operator_position_closed"' in src
