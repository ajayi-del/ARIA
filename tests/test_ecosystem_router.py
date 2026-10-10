"""
tests/test_ecosystem_router.py — pins for the ecosystem family router
(Three-Layer Network Phase 1, 2026-10-10).

Doctrine under test:
  - family map per 2026-10-10 contract; SILVER-USD absent everywhere;
    1000BONK/WIF/PENGU carry the data_dark note.
  - fan-out: primary keeps grade; proxies demote A->B (B never promotes);
    proxy prediction_score -10; high-beta proxy tp/stop x1.5; leverage
    clamped min(requested, leverage_map[symbol], 8).
  - active_family filters against the caller-supplied live universe
    (None = no filter, empty iterable = nothing alive).
  - unmapped symbol = singleton family (never refuse a signal).
  - RoutedOrder frozen; route_signal fail-closed on degenerate grades.

No network, no Settings() real env — SimpleNamespace grades per repo idiom.
"""
import dataclasses
import json
from types import SimpleNamespace

import pytest

from intelligence.ecosystem_router import (
    DEFAULT_FAMILIES,
    DEFAULT_HIGH_BETA,
    DEFAULT_MAX_LEVERAGE,
    EcosystemRouter,
    RoutedOrder,
    base_name,
)


def grade(g="A", side="long", sym="BTC", score=80, lev=8,
          margin=2.0, tp=0.01, stop=0.005):
    return SimpleNamespace(grade=g, direction=side, primary_symbol=sym,
                           prediction_score=score, leverage=lev,
                           margin_per_position=margin, tp_pct=tp,
                           stop_pct=stop)


# ── family map contract ──────────────────────────────────────────────────────

class TestFamilyMap:
    def test_families_exact_membership(self):
        r = EcosystemRouter()
        fam = r.families()
        # MSTR joined the BTC family 2026-10-10 (ecosystem-spec reconcile:
        # the one real BTC-family gap; MSTR-USD is universe-registered).
        assert fam["BTC"]["symbols"] == ("BTC", "COIN", "HOOD", "MSTR")
        assert fam["ETH_DEFI"]["symbols"] == ("ETH", "LINK", "UNI", "AAVE")
        assert fam["SOLANA"]["symbols"] == \
            ("SOL", "1000BONK", "WIF", "PENGU", "NEAR", "SUI")
        assert fam["AI_TECH"]["symbols"] == ("USTECH100", "NVDA", "AMD", "SPCX")
        assert fam["RWA_MACRO"]["symbols"] == ("XAUT", "COPPER", "CL")

    def test_silver_absent_everywhere(self):
        blob = json.dumps(DEFAULT_FAMILIES) + json.dumps(
            sorted(DEFAULT_HIGH_BETA))
        assert "SILVER" not in blob
        r = EcosystemRouter()
        assert r.get_family("SILVER-USD") == ["SILVER"]   # singleton, not routed into a family
        assert r.family_name("SILVER-USD") is None

    def test_data_dark_note(self):
        r = EcosystemRouter()
        assert r.families()["SOLANA"]["data_dark"] == \
            ("1000BONK", "WIF", "PENGU")
        assert r.data_dark_symbols("SOL-USD") == ["1000BONK", "WIF", "PENGU"]
        assert r.data_dark_symbols("BTC-USD") == []
        assert r.data_dark_symbols("DOGE-USD") == []   # unmapped

    def test_base_name_suffix_stripping(self):
        assert base_name("BTC-USD") == "BTC"
        assert base_name("btc") == "BTC"
        assert base_name("BTC-USD.P") == "BTC"
        assert base_name("1000BONK-USD") == "1000BONK"
        assert base_name("") == "" and base_name(None) == ""
        assert base_name("None") == ""

    def test_family_lookup_and_singleton(self):
        r = EcosystemRouter()
        assert r.family_name("COIN-USD") == "BTC"
        assert r.family_name("AAVE") == "ETH_DEFI"
        assert r.family_name("PENGU-USD") == "SOLANA"
        assert r.family_name("ZZZ-USD") is None
        # leader-first family list; unmapped symbol is its own ecosystem
        assert r.get_family("HOOD-USD") == ["BTC", "COIN", "HOOD", "MSTR"]
        assert r.get_family("ZZZ-USD") == ["ZZZ"]
        assert r.get_family("") == []

    def test_active_family_live_universe_filter(self):
        r = EcosystemRouter()
        full = ["SOL", "1000BONK", "WIF", "PENGU", "NEAR", "SUI"]
        # None = no filter
        assert r.active_family("SOL-USD", None) == full
        # caller asserts the dark trio is dead -> filtered out
        live = ["SOL", "NEAR", "SUI"]
        assert r.active_family("SOL-USD", live) == ["SOL", "NEAR", "SUI"]
        # venue-suffixed live universe is normalized
        assert r.active_family("SOL-USD", ["SOL-USD", "NEAR-USD"]) == \
            ["SOL", "NEAR"]
        # empty iterable = nothing alive
        assert r.active_family("SOL-USD", []) == []
        # unmapped symbol: singleton filtered the same way
        assert r.active_family("ZZZ-USD", ["ZZZ"]) == ["ZZZ"]
        assert r.active_family("ZZZ-USD", ["BTC"]) == []


# ── fan-out doctrine ─────────────────────────────────────────────────────────

class TestRouteSignal:
    def test_primary_keeps_grade_proxies_demote(self):
        r = EcosystemRouter()
        orders = r.route_signal(grade("A", sym="BTC"))
        assert [o.symbol for o in orders] == ["BTC", "COIN", "HOOD", "MSTR"]
        prim = orders[0]
        assert (prim.role, prim.grade, prim.prediction_score) == ("primary", "A", 80)
        assert (prim.tp_pct, prim.stop_pct) == (0.01, 0.005)
        for o in orders[1:]:
            assert o.role == "proxy" and o.grade == "B"          # A->B
            assert o.prediction_score == 70                      # score -10
            assert o.family == "BTC" and o.direction == "long"

    def test_b_grade_primary_never_promotes(self):
        r = EcosystemRouter()
        orders = r.route_signal(grade("B", sym="ETH", score=50))
        assert orders[0].grade == "B"
        assert all(o.grade == "B" for o in orders[1:])
        assert orders[1].prediction_score == 40

    def test_score_decay_floors_at_zero(self):
        r = EcosystemRouter()
        orders = r.route_signal(grade("B", sym="BTC", score=5))
        assert orders[1].prediction_score == 0

    def test_high_beta_geometry_widens(self):
        r = EcosystemRouter()
        # BTC family: COIN/HOOD high-beta -> tp/stop x1.5
        orders = r.route_signal(grade("A", sym="BTC"))
        assert orders[1].tp_pct == pytest.approx(0.015)
        assert orders[1].stop_pct == pytest.approx(0.0075)
        # ETH family: LINK/UNI/AAVE NOT high-beta -> geometry unchanged
        orders = r.route_signal(grade("A", sym="ETH"))
        for o in orders[1:]:
            assert o.tp_pct == 0.01 and o.stop_pct == 0.005
        # SOLANA family: all five proxies are high-beta
        orders = r.route_signal(grade("A", sym="SOL"))
        assert all(o.tp_pct == pytest.approx(0.015) for o in orders[1:])
        # AI_TECH: NVDA/AMD high-beta, SPCX not
        orders = {o.symbol: o for o in r.route_signal(grade("A", sym="USTECH100"))}
        assert orders["NVDA"].tp_pct == pytest.approx(0.015)
        assert orders["AMD"].tp_pct == pytest.approx(0.015)
        assert orders["SPCX"].tp_pct == pytest.approx(0.01)

    def test_leverage_clamp_ladder(self):
        r = EcosystemRouter()
        # requested 8, no map -> 8 (the class ceiling)
        orders = r.route_signal(grade("A", sym="BTC", lev=8))
        assert all(o.leverage == 8 for o in orders)
        # map value lower binds
        orders = r.route_signal(grade("A", sym="BTC", lev=8),
                                leverage_map={"BTC": 4})
        assert orders[0].leverage == 4 and orders[1].leverage == 8
        # map value higher can never exceed the 8x ceiling
        orders = r.route_signal(grade("A", sym="BTC", lev=8),
                                leverage_map={"BTC": 40, "COIN": 15})
        assert orders[0].leverage == 8 and orders[1].leverage == 8
        # requested above ceiling clamps to 8
        orders = r.route_signal(grade("A", sym="BTC", lev=25))
        assert orders[0].leverage == DEFAULT_MAX_LEVERAGE == 8
        # venue-suffixed map keys resolve
        orders = r.route_signal(grade("A", sym="BTC", lev=8),
                                leverage_map={"BTC-USD": 3})
        assert orders[0].leverage == 3

    def test_live_universe_filters_routes(self):
        r = EcosystemRouter()
        orders = r.route_signal(grade("A", sym="SOL"),
                                live_universe=["SOL", "NEAR"])
        assert [o.symbol for o in orders] == ["SOL", "NEAR"]
        assert orders[0].role == "primary" and orders[1].role == "proxy"
        # dark primary: family still routes the live remainder as proxies
        orders = r.route_signal(grade("A", sym="SOL"),
                                live_universe=["NEAR", "SUI"])
        assert [o.symbol for o in orders] == ["NEAR", "SUI"]
        assert all(o.role == "proxy" for o in orders)
        # nothing alive -> no routes
        assert r.route_signal(grade("A", sym="SOL"),
                              live_universe=[]) == []

    def test_unmapped_symbol_singleton_route(self):
        r = EcosystemRouter()
        orders = r.route_signal(grade("A", sym="ZZZ"))
        assert len(orders) == 1
        assert orders[0].symbol == "ZZZ" and orders[0].role == "primary"
        assert orders[0].family == "singleton"

    def test_venue_suffixed_primary_routes_by_base(self):
        r = EcosystemRouter()
        orders = r.route_signal(grade("A", sym="BTC-USD"))
        assert [o.symbol for o in orders] == ["BTC", "COIN", "HOOD", "MSTR"]

    def test_routed_order_frozen(self):
        r = EcosystemRouter()
        o = r.route_signal(grade())[0]
        assert isinstance(o, RoutedOrder)
        with pytest.raises(dataclasses.FrozenInstanceError):
            o.leverage = 1

    def test_degenerate_grade_fails_closed(self):
        r = EcosystemRouter()
        assert r.route_signal(grade(g="C")) == []            # grade outside A/B
        assert r.route_signal(grade(side="up")) == []        # bad direction
        assert r.route_signal(grade(sym="")) == []           # no primary
        assert r.route_signal(grade(margin=0.0)) == []       # zero economics
        assert r.route_signal(grade(tp=0.0)) == []
        assert r.route_signal(grade(stop=-1.0)) == []
        assert r.route_signal(None) == []                    # foreign object
        assert r.route_signal(object()) == []
        assert r.route_signal(grade(score="junk")) == []     # uncoercible score
        # None score coerces to 0, never raises
        orders = r.route_signal(grade(score=None))
        assert orders[0].prediction_score == 0

    def test_custom_families_injectable(self):
        # config-driven shape: caller may inject a different map
        r = EcosystemRouter(families={"X": {"symbols": ("AAA", "BBB"),
                                            "data_dark": ("BBB",)}},
                            high_beta={"BBB"})
        assert r.get_family("AAA-USD") == ["AAA", "BBB"]
        assert r.data_dark_symbols("AAA") == ["BBB"]
        orders = r.route_signal(grade("A", sym="AAA"))
        assert orders[1].tp_pct == pytest.approx(0.015)   # BBB high-beta
        # garbage injection falls back to doctrine defaults
        r2 = EcosystemRouter(families="not-a-mapping")
        assert r2.get_family("BTC") == ["BTC", "COIN", "HOOD", "MSTR"]
