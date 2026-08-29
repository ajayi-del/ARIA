"""Bull-run structural bundle pins (2026-08-21).

Fix A — close-verify: find_residual_qty venue-shape matrix.
Fix B — aster fixed-fractional sizing in build_candidate (Tharp/Vince: the
        fraction of the SLEEVE's equity is the ceiling; multipliers scale down).
Fix C — AsterFeed markPrice write-through to shared mark_price_stores (the
        MUBARAK-class silence: aster-routed symbols had no store writer, so
        mark_ok=False forever and symbol_ready was unreachable) + the 5000ms
        freshness margin (Horowitz: sample >=5x the 1Hz signal frequency).
Fix E — prune_age_expired contract (never re-absorbed while HELD, dropped
        once flat; the clear() oscillation fired 8 cycles/60s on 08-20).
"""
import time
import types

import pytest

from main import find_residual_qty, prune_age_expired
from data.aster_feed import AsterFeed
from data.mark_price_store import MarkPriceStore


# ── Fix A: find_residual_qty ─────────────────────────────────────────────────

def test_residual_normalized_aster_shape():
    live = [{"symbol": "AAVE-USD", "coin": "AAVE-USD", "side": "long",
             "size": 0.1, "qty": 0.1}]
    assert find_residual_qty(live, "AAVE-USD", "long") == pytest.approx(0.1)


def test_residual_wrong_side_is_flat():
    live = [{"symbol": "AAVE-USD", "side": "long", "size": 0.1}]
    assert find_residual_qty(live, "AAVE-USD", "short") == 0.0


def test_residual_sodex_raw_signed_size_infers_side():
    live = [{"symbol": "X-USD", "size": -0.5}]   # short, no side field
    assert find_residual_qty(live, "X-USD", "short") == pytest.approx(0.5)
    assert find_residual_qty(live, "X-USD", "long") == 0.0


def test_residual_sodex_buy_sell_strings():
    live = [{"symbol": "X-USD", "side": "SELL", "qty": 2.0}]
    assert find_residual_qty(live, "X-USD", "short") == pytest.approx(2.0)
    live2 = [{"symbol": "X-USD", "side": "BUY", "qty": 2.0}]
    assert find_residual_qty(live2, "X-USD", "long") == pytest.approx(2.0)


def test_residual_int_side_codes():
    assert find_residual_qty([{"symbol": "X-USD", "side": 2, "size": 1.0}],
                             "X-USD", "short") == pytest.approx(1.0)
    assert find_residual_qty([{"symbol": "X-USD", "side": 1, "size": 1.0}],
                             "X-USD", "long") == pytest.approx(1.0)


def test_residual_garbage_and_empty():
    assert find_residual_qty(None, "X-USD", "long") == 0.0
    assert find_residual_qty([], "X-USD", "long") == 0.0
    assert find_residual_qty(["not-a-dict", {"symbol": "Y-USD", "size": 1.0}],
                             "X-USD", "long") == 0.0
    assert find_residual_qty([{"symbol": "X-USD", "size": 0.0}],
                             "X-USD", "long") == 0.0
    assert find_residual_qty([{"symbol": "X-USD", "size": 1.0}], "", "long") == 0.0


# ── Fix E: prune_age_expired ─────────────────────────────────────────────────

def test_prune_keeps_held_drops_flat():
    assert prune_age_expired({"A-USD", "B-USD"}, {"A-USD"}) == {"A-USD"}


def test_prune_all_flat_empties():
    # Once the book is flat the expiry is history — a FUTURE position on the
    # symbol is a new instance and may be treasury-managed.
    assert prune_age_expired({"A-USD"}, set()) == set()


def test_prune_empty_expired_stays_empty():
    assert prune_age_expired(set(), {"A-USD"}) == set()


# ── Fix C: AsterFeed mark write-through ──────────────────────────────────────

def _mark_msg(sym_aster: str, px: float) -> dict:
    return {"s": sym_aster, "p": str(px), "i": str(px), "r": "0.0001",
            "E": int(time.time() * 1000)}


def test_aster_feed_writes_shared_store_for_routed_symbol():
    store = MarkPriceStore(symbol="MUBARAK-USD")
    feed = AsterFeed(symbols=["MUBARAK-USD"],
                     mark_price_stores={"MUBARAK-USD": store})
    feed._handle_mark_price(_mark_msg("MUBARAKUSDT", 0.0195))
    assert store.mark_price == pytest.approx(0.0195)
    assert store.is_healthy(5000)


def test_aster_feed_without_store_injection_still_updates_local_dict():
    feed = AsterFeed(symbols=["DOGE-USD"])
    feed._handle_mark_price(_mark_msg("DOGEUSDT", 0.0702))
    assert feed.mark_prices["DOGE-USD"]["mark_price"] == pytest.approx(0.0702)
    assert feed._mark_stores == {}


def test_aster_feed_zero_price_does_not_write_store():
    store = MarkPriceStore(symbol="MUBARAK-USD")
    feed = AsterFeed(symbols=["MUBARAK-USD"],
                     mark_price_stores={"MUBARAK-USD": store})
    feed._handle_mark_price(_mark_msg("MUBARAKUSDT", 0.0))
    assert store.mark_price is None


def test_mark_freshness_margin_5000_vs_500():
    # Aster pushes at 1Hz: a 4s-old mark is INSIDE the 5x engineering margin
    # but OUTSIDE the old 500ms window that aliased a healthy stream dead.
    store = MarkPriceStore(symbol="MUBARAK-USD")
    store.update(0.02, 0.02, int(time.time() * 1000) - 4000)
    assert store.is_healthy(5000)
    assert not store.is_healthy(500)


# ── Fix B: aster fixed-fractional sizing in build_candidate ─────────────────

class _MockParamStore:
    def get_ai_param(self, key, default=None):
        return default

    def get_stop_mult(self, symbol):
        return 2.5


class _MockCfg:
    ASSET_CONFIG = {
        "DOGE-USD": {"category": "crypto", "preferred_leverage": 5,
                     "max_leverage": 10},
        "XAUT-USD": {"category": "commodity", "preferred_leverage": 5,
                     "max_leverage": 10},
    }
    max_notional_usd = 500.0
    min_trade_notional_usd = 100.0
    default_leverage = 5
    max_margin_per_trade_pct = 0.20
    stop_atr_mult = 2.5
    base_trade_usd = 200.0
    max_trade_usd = 250.0
    small_account_balance_threshold = 150.0
    small_account_max_margin_pct = 0.30
    aster_margin_pct = 0.40
    aster_tradfi_margin_pct = 0.50
    aster_standard_path_fixed_fraction = True

    def effective_base_trade(self, balance, drawdown_pct=0.0, win_streak=0,
                             loss_streak=0):
        return self.base_trade_usd

    def effective_max_margin_pct(self, balance):
        if balance < self.small_account_balance_threshold:
            return self.small_account_max_margin_pct
        return self.max_margin_per_trade_pct


def _state(symbol="DOGE-USD", price=0.07, atr=0.0014, coherence=5.0):
    return types.SimpleNamespace(
        symbol=symbol, trade_direction="long", mark_price=price, atr=atr,
        coherence_score=coherence, drawdown_pct=0.0, win_streak=0,
        loss_streak=0, session_type="US", atr_vs_baseline=1.0,
        timestamp_ms=0, signal_age_ms=0, macro_bias="long",
        invalidation_reason="", size_multiplier=1.0,
    )


@pytest.fixture
def aster_routed():
    from execution import venue
    venue._venue_by_symbol["DOGE-USD"] = "aster"
    venue._venue_by_symbol["XAUT-USD"] = "aster"
    yield
    venue._venue_by_symbol.pop("DOGE-USD", None)
    venue._venue_by_symbol.pop("XAUT-USD", None)


def test_aster_conv2_hits_the_fraction_cap(aster_routed):
    from main import build_candidate
    # $203 sleeve × 0.40 × 5x = $406 cap; conv 2.0 (coh ≥4.5) reaches it.
    cand = build_candidate(_state(coherence=5.0), balance=203.0,
                           margin_engine=None, config=_MockCfg(),
                           param_store=_MockParamStore())
    assert cand is not None
    assert cand.size * cand.entry_price == pytest.approx(406.0, rel=1e-6)


def test_aster_conv1_sizes_conviction_base_frac(aster_routed):
    from main import build_candidate
    # Conviction ladder: 1.0 conviction = cap × aster_conviction_base_frac.
    # 2026-08-29 operator directive ("9usd is not efficient margin use"):
    # default frac 0.5 → 0.75 (+50% standard aster size; HYPE-class fill
    # $62.5 → ~$94). _MockCfg carries no knob → getattr default 0.75 binds.
    # Old pin asserted cap/2 = $203 — re-encoded for the new doctrine.
    cand = build_candidate(_state(coherence=2.0), balance=203.0,
                           margin_engine=None, config=_MockCfg(),
                           param_store=_MockParamStore())
    assert cand is not None
    assert cand.size * cand.entry_price == pytest.approx(406.0 * 0.75, rel=1e-6)


def test_aster_conv1_frac_05_legacy_bit_for_bit(aster_routed):
    from main import build_candidate
    # Knob at 0.5 reproduces the pre-2026-08-29 ladder exactly (cap/2 = $203).
    class _LegacyCfg(_MockCfg):
        aster_conviction_base_frac = 0.5
    cand = build_candidate(_state(coherence=2.0), balance=203.0,
                           margin_engine=None, config=_LegacyCfg(),
                           param_store=_MockParamStore())
    assert cand is not None
    assert cand.size * cand.entry_price == pytest.approx(203.0, rel=1e-6)


def test_aster_margin_cap_not_crushed_by_sodex_margin_pct(aster_routed):
    # The generic 20% SoDEX margin cap would produce $203 at conv 2.0 —
    # exactly half the operator-set 40% doctrine. Regression pin for the
    # balance_cap neutralization.
    from main import build_candidate
    cand = build_candidate(_state(coherence=5.0), balance=203.0,
                           margin_engine=None, config=_MockCfg(),
                           param_store=_MockParamStore())
    assert cand is not None
    assert cand.initial_margin == pytest.approx(406.0 / 5, rel=1e-6)


def test_aster_zero_equity_fails_closed(aster_routed):
    from main import build_candidate
    cand = build_candidate(_state(), balance=0.0,
                           margin_engine=None, config=_MockCfg(),
                           param_store=_MockParamStore())
    assert cand is None


def test_aster_tradfi_uses_tradfi_margin_pct(aster_routed):
    from main import build_candidate
    # Commodity tier: 0.50 × $203 × 5x = $507.50 at conv 2.0.
    cand = build_candidate(_state(symbol="XAUT-USD", price=2400.0, atr=12.0,
                                  coherence=5.0),
                           balance=203.0, margin_engine=None,
                           config=_MockCfg(), param_store=_MockParamStore())
    assert cand is not None
    assert cand.size * cand.entry_price == pytest.approx(507.5, rel=1e-6)


def test_aster_kill_switch_restores_sodex_chain(aster_routed):
    from main import build_candidate
    cfg = _MockCfg()
    cfg.aster_standard_path_fixed_fraction = False
    cand = build_candidate(_state(coherence=5.0), balance=203.0,
                           margin_engine=None, config=cfg,
                           param_store=_MockParamStore())
    assert cand is not None
    # Legacy SoDEX chain: conv 2.0 → $400 target, crushed to the 20% margin
    # cap ($203) — the pre-fix behavior, preserved behind the switch.
    assert cand.size * cand.entry_price == pytest.approx(203.0, rel=1e-6)


def test_aster_below_exchange_floor_fails_closed(aster_routed):
    from main import build_candidate
    # base = balance × 0.40 × 5 / 2 < $1 → the sleeve cannot fund even the
    # exchange minimum. Fail closed, never dust.
    cand = build_candidate(_state(), balance=0.5,
                           margin_engine=None, config=_MockCfg(),
                           param_store=_MockParamStore())
    assert cand is None


def test_aster_fixed_fraction_config_default():
    from core.config import Settings
    assert Settings().aster_standard_path_fixed_fraction is True


# ── DOGE migration (2026-08-21 operator directive) ──────────────────────────

def test_doge_in_aster_assets_and_universe():
    from core.config import Settings
    c = Settings()
    assert "DOGE-USD" in c.aster_assets
    assert "DOGE-USD" in c.assets
    # Shadow trio stays data-only — routing isolation intact.
    assert "DOGE-USD" not in c.aster_shadow_assets
    assert c.ASSET_CONFIG["DOGE-USD"]["category"] == "meme"


def test_doge_bybit_signal_path_mapped():
    from data.bybit_feed import BYBIT_SYMBOL_MAP
    assert BYBIT_SYMBOL_MAP.get("DOGE-USD") == "DOGEUSDT"
