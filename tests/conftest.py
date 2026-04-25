import tempfile
from pathlib import Path
import risk.drawdown_manager
import pytest

# Isolate DrawdownManager state across tests to prevent cross-test contamination
# from logs/drawdown_state.json.
_tmp_state = Path(tempfile.gettempdir()) / "aria_test_drawdown_state.json"
risk.drawdown_manager._STATE_FILE = _tmp_state


@pytest.fixture(autouse=True)
def _clear_drawdown_state():
    """Delete the temp drawdown state file before every test."""
    try:
        _tmp_state.unlink(missing_ok=True)
    except Exception:
        pass
    yield
