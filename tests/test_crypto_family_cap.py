"""Crypto family concurrent cap pins (2026-10-19).

Mirrors the equity correlation cap for the crypto families in
ASSET_CATEGORIES (alt_l1/defi_infra/meme/large_cap/cex_ecosystem): family
members share beta — 4 alt_l1 longs is one 4x alt-beta position in costume.

Ladder (one notch tighter than equity): 1st concurrent same-direction → pass;
2nd → ×0.5 (state flag consumed post-candidate with the elite-half
notional-floor idiom); 3rd+ → reject (shadow gate crypto_family_cap, scored
in BOTH live and disabled modes via record_would_block).

Kill switch CRYPTO_FAMILY_CAP_ENABLED: OFF = pre-module bit-for-bit (no
count side-effects, no flag stamped, no reject).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.crypto_family_cap import (  # noqa: E402
    CRYPTO_FAMILIES, FAMILY_SIZE_MULT, crypto_family_cap_enabled,
    crypto_family_of, family_cap_verdict)


class TestKillSwitch:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("CRYPTO_FAMILY_CAP_ENABLED", raising=False)
        assert crypto_family_cap_enabled() is True

    def test_explicit_off(self, monkeypatch):
        monkeypatch.setenv("CRYPTO_FAMILY_CAP_ENABLED", "false")
        assert crypto_family_cap_enabled() is False

    def test_env_read_at_call_time(self, monkeypatch):
        monkeypatch.setenv("CRYPTO_FAMILY_CAP_ENABLED", "false")
        assert crypto_family_cap_enabled() is False
        monkeypatch.setenv("CRYPTO_FAMILY_CAP_ENABLED", "true")
        assert crypto_family_cap_enabled() is True


class TestFamilyOf:
    def test_alt_l1(self):
        assert crypto_family_of("SOL-USD") == "alt_l1"

    def test_large_cap(self):
        assert crypto_family_of("BTC-USD") == "large_cap"

    def test_defi_infra(self):
        assert crypto_family_of("LINK-USD") == "defi_infra"

    def test_meme(self):
        assert crypto_family_of("DOGE-USD") == "meme"

    def test_cex_ecosystem(self):
        assert crypto_family_of("HYPE-USD") == "cex_ecosystem"

    def test_uncapped_commodity(self):
        # Commodities are not in the five capped crypto families.
        assert crypto_family_of("XAUT-USD") is None

    def test_uncapped_equity_index(self):
        # index_tech/index_broad carry the equity correlation cap, not this one.
        assert crypto_family_of("NVDA-USD") is None
        assert crypto_family_of("SPCX-USD") is None

    def test_unknown_symbol_passes(self):
        # The cap never invents a family.
        assert crypto_family_of("NOTREAL-USD") is None

    def test_garbage_passes(self):
        assert crypto_family_of(None) is None
        assert crypto_family_of("") is None


class TestFamilyCapVerdict:
    def test_first_concurrent_passes(self):
        assert family_cap_verdict(0) == "pass"

    def test_second_concurrent_halves(self):
        assert family_cap_verdict(1) == "half"

    def test_third_concurrent_blocks(self):
        assert family_cap_verdict(2) == "block"

    def test_fourth_plus_blocks(self):
        assert family_cap_verdict(5) == "block"

    def test_garbage_fails_closed(self):
        assert family_cap_verdict(None) == "block"
        assert family_cap_verdict("x") == "block"


class TestConstants:
    def test_capped_family_set(self):
        assert CRYPTO_FAMILIES == frozenset({
            "alt_l1", "defi_infra", "meme", "large_cap", "cex_ecosystem"})

    def test_size_mult(self):
        assert FAMILY_SIZE_MULT == 0.5
