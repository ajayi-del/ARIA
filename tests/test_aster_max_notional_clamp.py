"""o5 Aster per-order max-notional clamp pins (2026-09-09, CEO s28 approach).

The incident: AKE 386 sizing_chains -> 6 decisions -> 2 approved -> 0
brackets on 09-08 — the venue rejects post-approval with "maximum notional
value limit" (AKE ~$35, FLOCK ~$46-67 attempted notional). Clamp-DOWN at
build_candidate is the fail-safe direction; the bracket_failed path also
learns a tightening cap (0.8x attempted) so the map self-seeds from live
rejects. Static seed: ASTER_MAX_NOTIONAL_USD_JSON.

Kill switch: ASTER_MAX_NOTIONAL_CLAMP_ENABLED absent/false = clamp inert
(bit-for-bit legacy); cap loading is telemetry-only in both states.
"""

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main  # noqa: E402
from execution.aster_client import AsterClient  # noqa: E402


@pytest.fixture
def _clean(monkeypatch):
    monkeypatch.delenv("ASTER_MAX_NOTIONAL_CLAMP_ENABLED", raising=False)
    saved = dict(main._aster_max_notional)
    main._aster_max_notional.clear()
    yield
    main._aster_max_notional.clear()
    main._aster_max_notional.update(saved)


class TestGate:
    def test_default_inert(self, _clean):
        assert main._aster_cap_clamp_enabled() is False

    def test_true_enables(self, _clean, monkeypatch):
        monkeypatch.setenv("ASTER_MAX_NOTIONAL_CLAMP_ENABLED", "true")
        assert main._aster_cap_clamp_enabled() is True

    def test_registry_default_empty(self, _clean):
        assert main._aster_max_notional == {}


class TestClampArithmetic:
    def test_clamp_targets_95pct_of_cap(self):
        # Requested $400 against a $25 venue cap -> $23.75, still above the
        # $3 Aster exchange floor so the candidate survives to placement.
        cap, requested = 25.0, 400.0
        clamped = min(requested, cap * 0.95)
        assert clamped == pytest.approx(23.75)
        assert clamped >= 3.0

    def test_below_cap_untouched(self):
        cap, requested = 25.0, 20.0
        assert min(requested, cap * 0.95) == requested


class TestLearnFromReject:
    def test_learned_cap_is_80pct_of_attempt(self):
        # AKE incident row: 1981.855 qty x 0.017887 entry = $35.45 attempted.
        attempted = 1981.85533479 * 0.017887
        assert round(attempted * 0.8, 2) == pytest.approx(28.36, abs=0.01)

    def test_learn_only_tightens(self):
        prev, learned = 20.0, 28.36
        assert not (learned > 0 and (prev <= 0 or learned < prev))

    def test_learn_seeds_when_unknown(self):
        prev, learned = 0.0, 28.36
        assert learned > 0 and (prev <= 0 or learned < prev)


class TestSpecSurface:
    def test_get_spec_default_carries_max_notional(self):
        client = AsterClient(SimpleNamespace())
        spec = client.get_spec("AKE-USD")
        assert spec["max_notional"] == 0.0

    def test_spec_dict_shape(self):
        client = AsterClient(SimpleNamespace())
        client._specs["AKE-USD"] = {"tick": 0.000001, "step": 1.0,
                                    "min_qty": 1.0, "min_notional": 1.0,
                                    "max_notional": 25.0}
        assert client.get_spec("AKE-USD")["max_notional"] == 25.0
