"""Pins: M8 rho-box clamp + per-symbol fire floor (2026-11-17).

rho_box_clamp: resize above rho x headroom, deny below min-viable,
unknown on dark headroom (legacy risk returned), kill/rho<=0 bit-for-bit.
symbol floor: parse idiom + precedence (symbol > class floor handled at
the consult site) + fail-open on dark coherence.
"""

from __future__ import annotations

from prop_harness.brains import eval_profile as ep


class TestRhoBoxClamp:
    def test_under_cap_admits_unchanged(self):
        risk, v = ep.rho_box_clamp(10.0, 200.0, rho=0.12)
        assert risk == 10.0 and v is None

    def test_over_cap_resizes(self):
        risk, v = ep.rho_box_clamp(40.0, 200.0, rho=0.12)
        assert v == "resize" and risk == 24.0

    def test_sub_min_viable_denies(self):
        # cap = 0.12 x 40 = 4.8 < 7.0 -> deny, not dust
        risk, v = ep.rho_box_clamp(30.0, 40.0, rho=0.12)
        assert v == "deny" and risk == 0.0

    def test_min_viable_boundary(self):
        # cap = 0.12 x 58.33 = 7.0 -> resize allowed at exactly min_viable
        risk, v = ep.rho_box_clamp(50.0, 58.3334, rho=0.12,
                                   min_viable=7.0)
        assert v == "resize" and abs(risk - 7.0) < 0.01

    def test_headroom_unknown_legacy_risk(self):
        risk, v = ep.rho_box_clamp(40.0, None, rho=0.12)
        assert risk == 40.0 and v == "unknown"

    def test_kill_off_bit_for_bit(self):
        risk, v = ep.rho_box_clamp(40.0, 10.0, rho=0.12, enabled=False)
        assert risk == 40.0 and v is None

    def test_rho_zero_bit_for_bit(self):
        risk, v = ep.rho_box_clamp(40.0, 10.0, rho=0.0)
        assert risk == 40.0 and v is None

    def test_nonpositive_risk_passthrough(self):
        assert ep.rho_box_clamp(0.0, 100.0)[0] == 0.0
        assert ep.rho_box_clamp(-5.0, 100.0)[0] == -5.0


class TestSymbolMinCohFire:
    def test_parse_map(self):
        m = ep.parse_symbol_min_coh_map("XAUT:4.0,GOLD:4.0")
        assert m == {"XAUT": 4.0, "GOLD": 4.0}

    def test_parse_blank_and_zero_disable(self):
        assert ep.parse_symbol_min_coh_map("") == {}
        assert ep.parse_symbol_min_coh_map(None) == {}
        assert ep.parse_symbol_min_coh_map("0") == {}

    def test_parse_skips_garbage(self):
        m = ep.parse_symbol_min_coh_map("XAUT:4.0,bad,GOLD:zzz,BTC:0")
        assert m == {"XAUT": 4.0}

    def test_floor_binds_via_signal_symbol(self):
        # XAUT-USD tap row: normalized asset is xyz:GOLD but XAUT key
        # binds through the signal symbol / -USD base.
        m = ep.parse_symbol_min_coh_map("XAUT:4.0")
        reason, thr = ep.symbol_min_coh_fire_verdict(
            3.5, "xyz:GOLD", "XAUT-USD", floor_map=m)
        assert reason == ep.SYMBOL_MIN_COH_FIRE_STANDDOWN
        assert thr == 4.0

    def test_floor_binds_via_normalized_asset(self):
        m = ep.parse_symbol_min_coh_map("GOLD:4.0")
        reason, thr = ep.symbol_min_coh_fire_verdict(
            3.5, "xyz:GOLD", "XAUT-USD", floor_map=m)
        assert reason == ep.SYMBOL_MIN_COH_FIRE_STANDDOWN

    def test_above_floor_admits(self):
        m = ep.parse_symbol_min_coh_map("XAUT:4.0")
        reason, thr = ep.symbol_min_coh_fire_verdict(
            4.5, "xyz:GOLD", "XAUT-USD", floor_map=m)
        assert reason is None and thr == 4.0

    def test_unmapped_symbol_admits(self):
        m = ep.parse_symbol_min_coh_map("XAUT:4.0")
        assert ep.symbol_min_coh_fire_verdict(
            1.0, "BTC", "BTC-USD", floor_map=m)[0] is None

    def test_empty_map_legacy(self):
        assert ep.symbol_min_coh_fire_verdict(
            1.0, "xyz:GOLD", "XAUT-USD", floor_map={}) == (None, None)
        assert ep.symbol_min_coh_fire_verdict(
            1.0, "xyz:GOLD", "XAUT-USD", floor_map=None) == (None, None)

    def test_dark_coh_fail_open(self):
        m = ep.parse_symbol_min_coh_map("XAUT:4.0")
        reason, thr = ep.symbol_min_coh_fire_verdict(
            None, "xyz:GOLD", "XAUT-USD", floor_map=m)
        assert reason is None and thr == 4.0

    def test_crypto_symbol_binds(self):
        # XAUT floor must not leak onto unrelated crypto; a mapped crypto
        # symbol binds by its own key.
        m = ep.parse_symbol_min_coh_map("BTC:6.0")
        reason, _ = ep.symbol_min_coh_fire_verdict(
            5.0, "BTC", "BTC-USD", floor_map=m)
        assert reason == ep.SYMBOL_MIN_COH_FIRE_STANDDOWN


class TestConfigKnobs:
    def test_defaults(self, monkeypatch, tmp_path):
        from prop_harness.config import PropConfig
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        cfg = PropConfig.from_env()
        assert cfg.rho_box_enabled is True
        assert cfg.rho_box == 0.12
        assert cfg.rho_box_min_viable_usd == 7.0
        assert cfg.post_loss_cooloff_enabled is True
        assert cfg.post_loss_cooloff_ttl_s == 1800.0
        assert cfg.reentry_latch_enabled is True
        assert cfg.reentry_latch_ttl_s == 1800.0
        assert cfg.tap_seen_confirm_enabled is True
        assert cfg.tap_ttl_prune_enabled is True
        assert cfg.arm_boot_replay is False
        assert cfg.symbol_min_coh_fire == "XAUT:4.0,GOLD:4.0"
        assert cfg.book_view_enabled is True

    def test_env_binds_and_kills(self, monkeypatch, tmp_path):
        from prop_harness.config import PropConfig
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        monkeypatch.setenv("PROP_RHO_BOX", "0.20")
        monkeypatch.setenv("PROP_RHO_BOX_ENABLED", "0")
        monkeypatch.setenv("PROP_POST_LOSS_COOLOFF", "0")
        monkeypatch.setenv("PROP_REENTRY_LATCH_TTL_S", "600")
        monkeypatch.setenv("PROP_ARM_BOOT_REPLAY", "1")
        monkeypatch.setenv("PROP_SYMBOL_MIN_COH_FIRE", "BTC:6.0")
        monkeypatch.setenv("PROP_BOOK_VIEW", "0")
        cfg = PropConfig.from_env()
        assert cfg.rho_box == 0.20
        assert cfg.rho_box_enabled is False
        assert cfg.post_loss_cooloff_enabled is False
        assert cfg.reentry_latch_ttl_s == 600.0
        assert cfg.arm_boot_replay is True
        assert cfg.symbol_min_coh_fire == "BTC:6.0"
        assert cfg.book_view_enabled is False

    def test_book_view_artifact(self, monkeypatch, tmp_path):
        from prop_harness.config import PropConfig
        monkeypatch.setenv("PROP_STATE_ROOT", str(tmp_path))
        cfg = PropConfig.from_env()
        assert cfg.book_view_artifact == (
            tmp_path / "program" / "book_view.json")
        monkeypatch.setenv("PROP_BOOK_VIEW_PATH", str(tmp_path / "bv.json"))
        cfg2 = PropConfig.from_env()
        assert cfg2.book_view_artifact == tmp_path / "bv.json"
