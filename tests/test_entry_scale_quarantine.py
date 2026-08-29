"""Pins for _entry_scale_quarantined scope fix (watchdog 2026-08-29).

The 2026-08-28 24h auto-tier deploy hoisted the helper to module level while it
still referenced main()-local mark_price_stores as a bare global — every call
raised NameError, killing both cascade fast paths for 18h (95 momentum + 3
aftermath exceptions). These pins lock the stores-param contract.
"""
import importlib.util
import time

import pytest


def _load_quarantine_fn():
    """Import main.py is heavy; extract just the helper via exec of its source."""
    import ast
    src = open("main.py").read()
    tree = ast.parse(src)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_entry_scale_quarantined":
            fn_src = ast.get_source_segment(src, node)
            ns = {"time": time, "_scale_mismatch": {}}
            exec(fn_src, ns)
            return ns["_entry_scale_quarantined"], ns["_scale_mismatch"]
    pytest.fail("_entry_scale_quarantined not found at module level")


class _FakeStore:
    def __init__(self, mark):
        self.mark_price = mark


def test_no_stores_param_falls_back_to_registry_only():
    fn, registry = _load_quarantine_fn()
    # stores=None: live check skipped, registry read works — must NOT raise.
    assert fn("AAA-USD") is False
    registry["AAA-USD"] = time.time()
    assert fn("AAA-USD") is True


def test_scale_mismatch_quarantines_with_explicit_stores():
    fn, registry = _load_quarantine_fn()
    stores = {"SPCX-USD": _FakeStore(765.72)}
    assert fn("SPCX-USD", ref_price=135.0, stores=stores) is True
    assert "SPCX-USD" in registry
    # Agreeing mark plane passes.
    stores2 = {"SPCX-USD": _FakeStore(135.5)}
    registry.clear()
    assert fn("SPCX-USD", ref_price=135.0, stores=stores2) is False


def test_ttl_expiry_releases_quarantine():
    fn, registry = _load_quarantine_fn()
    registry["BBB-USD"] = time.time() - 1000  # beyond 900s TTL
    assert fn("BBB-USD", ttl_s=900.0) is False
