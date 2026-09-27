"""tests/test_main_campaign_m2_splice.py — pins for the M2 campaign splice
(2026-09-26, splice 2 of 2: rung ladder + cross-side probes + OBOB gates +
narrative compass wiring in main.py).

Conventions follow tests/test_main_fast_cycle_splice.py: source pins on
main.py for the closure wiring (the parts that cannot boot without main())
plus behavioral pins on the config kill switches. Every kill-switch
off-state must reproduce the pre-splice system bit-for-bit:

  ratchet_coordinator_enabled=False   -> _ratchet_coord is None -> the
      rung seam never executes (guarded `if _ratchet_coord is not None`)
  cross_side_scanner_enabled=False    -> _cross_side is None -> no intents,
      no scan, no drain, no release calls
  adl_lens_enabled=False              -> _adl_lens is None -> no advisory
  obob_enabled=False                  -> _plan_allowed stays True, ceiling
      None -> legacy placement flow bit-for-bit
  narrative_compass_enabled=False     -> compass_verdict returns None for
      every symbol -> _cmp_map empty -> original symbol order, both sides,
      full density (the pre-module lottery)
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MAIN_SRC = os.path.join(_REPO, "main.py")


def _main_src() -> str:
    with open(_MAIN_SRC) as fh:
        return fh.read()


# ── splice A: rung-ladder seam on the EXISTING _roe_ratchet_loop ─────────────

class TestSpliceARungLadder:
    def test_rides_existing_roe_loop_never_a_second_one(self):
        src = _main_src()
        # One loop definition only — the campaign ladder is a seam inside it.
        assert src.count("async def _roe_ratchet_loop(") == 1
        assert "_ratchet_coord.on_roe_tick(" in src

    def test_boot_seed_with_rungs_at_or_below_peak(self):
        src = _main_src()
        # Restart must not re-fire rungs an adopted position already crossed.
        assert "_ratchet_coord.seed_fired(" in src
        assert "_ratchet_seeded" in src

    def test_counter_trend_shadow_field_on_intents(self):
        # R3: intents fire regardless of trend verdicts — the shadow field
        # scores the bypass from birth.
        src = _main_src()
        assert "counter_trend=_ctd" in src

    def test_rung80_adl_advisory_only(self):
        src = _main_src()
        assert "campaign_adl_advisory" in src
        # Advisory ONLY: the advisory site never closes a position.
        i = src.index("campaign_adl_advisory")
        window = src[max(0, i - 2000):i + 2000]
        assert "_close_with_retry" not in window
        assert "close_position" not in window

    def test_cross_side_intent_arms_pending_probe(self):
        src = _main_src()
        assert "_cross_side.on_cross_side_intent(" in src
        assert "_xpr_pending.append(" in src

    def test_close_seam_releases_ladder_and_probe_key(self):
        src = _main_src()
        assert "_ratchet_coord.on_position_closed(" in src
        assert "_ratchet_seeded.discard(" in src
        # A closed winner releases the counter-side key; a closed probe
        # releases its own key (the stuck-key class).
        assert "_cross_side.release(sym, counter_side=_counter_side)" in src
        assert "_cross_side.release(" in src


# ── splice B: cross-side scanner + probe pipeline ────────────────────────────

class TestSpliceBCrossSide:
    def test_scan_feeds_pending(self):
        src = _main_src()
        assert "_cross_side.scan(" in src

    def test_probe_tag_prefix_and_journal_contract(self):
        src = _main_src()
        assert 'f"xpr-{_xspec.symbol.split' in src
        assert 'strategy_tag="cross_side_probe"' in src
        # Probes are CAMPAIGN-personality journaled (R1 — APEX/main-book
        # stats never ingest them).
        i = src.index('strategy_tag="cross_side_probe"')
        window = src[i:i + 1200]
        assert 'personality="CAMPAIGN"' in window

    def test_probe_margin_capped_by_doctrine_budget(self):
        # entry_verdict sizes from fast_cycle_margin_per_trade; the probe
        # never out-sizes its own premium (frac of winner uPnL).
        src = _main_src()
        assert "_xmargin = min(float(_xspec.budget_usd)," in src

    def test_place_failure_releases_key(self):
        src = _main_src()
        assert 'reason="place_failed"' in src
        assert 'reason="entry_refused"' in src
        assert 'reason="expired"' in src

    def test_prune_pass_releases_xpr_key(self):
        src = _main_src()
        i = src.index("anticipator_order_pruned")
        window = src[max(0, i - 800):i]
        assert 'startswith("xpr-")' in window
        assert "_cross_side.release(" in window

    def test_evict_branch_releases_xpr_key(self):
        src = _main_src()
        i = src.index("anticipator_order_evicted")
        window = src[i:i + 1200]
        assert 'startswith(' in window and '"xpr-"' in window
        assert "_cross_side.release(" in window

    def test_immune_purge_exemption_extended_to_xpr(self):
        # The M1 exemption matched "ant-" only; xpr- probes are designed
        # long-lived and the prune pass owns their lifecycle too.
        src = _main_src()
        assert 'startswith(\n                                ("ant-", "xpr-"))' in src


# ── splice C: OBOB budget gates ──────────────────────────────────────────────

class TestSpliceCObob:
    def test_master_gate_wraps_all_three_gates(self):
        src = _main_src()
        assert 'if bool(getattr(config, "obob_enabled", True)):' in src

    def test_loss_cap_standdown_event(self):
        src = _main_src()
        assert "obob_daily_cap_standdown" in src
        # cap = pct x pool ($37.50 at 0.15 x $250, Governor 2026-09-27)
        assert '"obob_daily_loss_cap_pct", 0.15' in src
        assert '"fast_cycle_pool_usd", 250.0' in src

    def test_volume_homeostat_events(self):
        src = _main_src()
        assert "obob_volume_target_met" in src
        assert "obob_density_cap" in src
        assert "_ObobGovernor.placements_for_volume(" in src

    def test_loss_budget_trade_ceiling(self):
        src = _main_src()
        assert "obob_loss_budget_exhausted" in src
        assert "_ObobGovernor.max_trades_by_loss_cap(" in src

    def test_exits_and_prunes_never_gated(self):
        # The prune pass runs BEFORE the obob verdicts; _plan_allowed only
        # gates NEW placements (scan/drain + plan loop).
        src = _main_src()
        i_prune = src.index("anticipator.prune_verdicts(")
        i_obob = src.index('if bool(getattr(config, "obob_enabled", True)):')
        assert i_prune < i_obob
        assert "for _sym in (_sym_order if _plan_allowed else []):" in src

    def test_probes_gated_by_standdown(self):
        src = _main_src()
        assert "if _plan_allowed and _cross_side is not None:" in src


# ── compass wiring ───────────────────────────────────────────────────────────

class TestCompassWiring:
    def test_verdict_logged_per_symbol(self):
        src = _main_src()
        assert "narrative_compass_verdict" in src
        assert "_compass.compass_verdict(" in src

    def test_permutation_orders_iteration_and_logs(self):
        src = _main_src()
        assert "_compass.weighted_slot_permutation(" in src
        assert "narrative_compass_permutation" in src
        assert "_sym_order = [_c[\"symbol\"] for _c in _perm_out]" in src

    def test_side_selection_drops_counter_specs(self):
        src = _main_src()
        assert "_specs = [_s for _s in _specs" in src
        assert "if _s.side == _cbias]" in src or "if _s.side == _cbias" in src

    def test_density_scales_never_zeroed(self):
        src = _main_src()
        # floor 1 while any spec survives (Free Option axiom)
        assert "_specs = _specs[:max(\n" in src
        assert "int(round(len(_specs) * _cw)))]" in src

    def test_tide_injected_lean_conditional_majors_only(self):
        src = _main_src()
        i = src.index("narrative_compass_verdict")
        window = src[max(0, i - 4000):i]
        assert 'in ("BTC", "ETH", "SOL")' in window
        assert "tide_aligned(" in window

    def test_trend_day_composed_to_suffixed_form(self):
        src = _main_src()
        assert '_ctd_v = "aligned_long"' in src
        assert '_ctd_v = "aligned_short"' in src


# ── kill switches (config) ───────────────────────────────────────────────────

class TestKillSwitches:
    def test_obob_enabled_default_true(self):
        from core.config import Settings
        assert Settings().obob_enabled is True

    def test_narrative_compass_enabled_default_true(self):
        from core.config import Settings
        assert Settings().narrative_compass_enabled is True

    def test_module_gates_construct_only_when_fast_cycle_live(self):
        src = _main_src()
        # All three brains construct inside the fast-cycle guard — flag
        # False leaves every one of them None (never constructed, never
        # mutated).
        for name in ("_ratchet_coord", "_cross_side", "_adl_lens"):
            assert f"{name} = (" in src
            i = src.index(f"{name} = (")
            window = src[i:i + 400]
            assert "None" in window


# ── ast health ───────────────────────────────────────────────────────────────

def test_main_parses_clean():
    import ast
    with open(_MAIN_SRC) as fh:
        ast.parse(fh.read())
