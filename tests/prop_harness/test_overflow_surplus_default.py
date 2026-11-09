"""ATS-59: overflow sleeves default capital_block surplus OFF.

Funded turbos (non-overflow) keep surplus ON when unset. Explicit
PROP_CAPITAL_SURPLUS always wins. Demo must not scoop surplus fills.
"""

from __future__ import annotations

import os

import pytest

from prop_harness.config import PropConfig


@pytest.fixture(autouse=True)
def _clean_prop_env(monkeypatch):
    for k in list(os.environ):
        if k.startswith("PROP_"):
            monkeypatch.delenv(k, raising=False)
    # Minimal required so from_env does not invent a live account.
    monkeypatch.setenv("PROP_ENABLED", "false")
    yield


def test_owner_sleeve_surplus_defaults_on(monkeypatch):
    monkeypatch.setenv("PROP_OVERFLOW_ONLY", "0")
    cfg = PropConfig.from_env()
    assert cfg.overflow_only is False
    assert cfg.capital_surplus_enabled is True


def test_overflow_sleeve_surplus_defaults_off(monkeypatch):
    monkeypatch.setenv("PROP_OVERFLOW_ONLY", "1")
    cfg = PropConfig.from_env()
    assert cfg.overflow_only is True
    assert cfg.capital_surplus_enabled is False


def test_explicit_surplus_on_overrides_overflow_default(monkeypatch):
    monkeypatch.setenv("PROP_OVERFLOW_ONLY", "1")
    monkeypatch.setenv("PROP_CAPITAL_SURPLUS", "1")
    cfg = PropConfig.from_env()
    assert cfg.capital_surplus_enabled is True


def test_explicit_surplus_off_overrides_owner_default(monkeypatch):
    monkeypatch.setenv("PROP_OVERFLOW_ONLY", "0")
    monkeypatch.setenv("PROP_CAPITAL_SURPLUS", "0")
    cfg = PropConfig.from_env()
    assert cfg.capital_surplus_enabled is False
