"""Treasury subsystem: ledger, clusters, harvest decisions, margin recycling."""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from intelligence.treasury import (  # noqa: E402
    Treasury, LedgerEntry, compute_thresholds, cluster_of,
    CLUSTER_CRYPTO, CLUSTER_EQUITY, CLUSTER_COMMODITY,
)


class _Cfg:
    treasury_enabled = True
    treasury_runaway_trim_ratio = 0.5
    treasury_recycle_enabled = True
    treasury_recycle_margin_util = 0.75
    treasury_recycle_min_age_s = 2700.0
    treasury_recycle_flat_roe_band = 1.5
    trend_day_tp_room_enabled = True
    trend_day_winner_escape_mult = 1.5


class _Pos:
    def __init__(self, symbol, side, size, entry, im=0.0, age_ms=0):
        self.symbol = symbol
        self.side = side
        self.size = size
        self.entry_price = entry
        self.initial_margin = im
        self.opened_at_ms = 1_000_000 - age_ms


NOW_MS = 1_000_000
NOW_S = 1000.0


def _step(sym, venue):
    return 0.001


def _min_notional(sym, venue):
    return 10.0 if venue == "sodex" else 1.0


def _build(treasury, positions, marks, venues=None, cats=None,
           day_types=None, htf=None):
    venues = venues or {}
    cats = cats or {}
    day_types = day_types or {}
    htf = htf or {}
    return treasury.build_ledger(
        positions,
        mark_fn=lambda s: marks.get(s),
        venue_fn=lambda s: venues.get(s, "sodex"),
        category_fn=lambda s: cats.get(s, "unknown"),
        day_type_fn=lambda s: day_types.get(s, "unknown"),
        depth_fn=lambda s, side: 1.0,
        htf_fn=lambda s: htf.get(s, "neutral"),
        now_ms=NOW_MS,
    )


def _decide(treasury, ledger, active, balance=500.0, cooldowns=None):
    return treasury.decide(
        ledger, active, cascade_phase="idle", meta_tp_mult=None,
        balance=balance, cooldowns=cooldowns or {},
        now=NOW_S, now_ms=NOW_MS,
        step_fn=_step, min_notional_fn=_min_notional,
    )


# ── cluster mapping ──────────────────────────────────────────────────────

def test_cluster_mapping():
    assert cluster_of("equity") == CLUSTER_EQUITY
    assert cluster_of("equity_index") == CLUSTER_EQUITY
    assert cluster_of("commodity") == CLUSTER_COMMODITY
    assert cluster_of("large_cap") == CLUSTER_CRYPTO
    assert cluster_of("meme") == CLUSTER_CRYPTO
    assert cluster_of("unknown") == CLUSTER_CRYPTO


# ── ledger build ─────────────────────────────────────────────────────────

def test_margin_reconstruction_ghost_repair():
    t = Treasury(_Cfg)
    positions = [_Pos("BTC-USD", "long", 0.01, 100.0, im=0.0)]
    ledger = _build(t, positions, {"BTC-USD": 110.0})
    assert len(ledger) == 1
    # im reconstructed: 0.01 * 100 / 5 = 0.2
    assert abs(ledger[0].initial_margin - 0.2) < 1e-9
    assert ledger[0].pnl > 0
    assert ledger[0].roe > 0


def test_ledger_skips_bad_marks():
    t = Treasury(_Cfg)
    positions = [_Pos("BTC-USD", "long", 0.01, 100.0, im=1.0)]
    assert _build(t, positions, {"BTC-USD": None}) == []
    assert _build(t, positions, {"BTC-USD": 0.0}) == []


# ── group_active: per-cluster, no range-day 3-position deadlock ─────────

def test_group_active_two_crypto_is_active():
    t = Treasury(_Cfg)
    positions = [
        _Pos("BTC-USD", "long", 0.01, 100.0, im=1.0),
        _Pos("ETH-USD", "long", 0.1, 100.0, im=1.0),
        _Pos("SPCX-USD", "long", 1.0, 100.0, im=1.0),
    ]
    ledger = _build(t, positions,
                    {"BTC-USD": 100.0, "ETH-USD": 100.0, "SPCX-USD": 100.0},
                    cats={"SPCX-USD": "equity_index"})
    active = t.group_active(ledger, set())
    assert CLUSTER_CRYPTO in active and len(active[CLUSTER_CRYPTO]) == 2
    assert CLUSTER_EQUITY not in active   # single equity — keeps native TPs


def test_group_active_respects_age_excluded():
    t = Treasury(_Cfg)
    positions = [
        _Pos("BTC-USD", "long", 0.01, 100.0, im=1.0),
        _Pos("ETH-USD", "long", 0.1, 100.0, im=1.0),
    ]
    ledger = _build(t, positions, {"BTC-USD": 100.0, "ETH-USD": 100.0})
    active = t.group_active(ledger, {"BTC-USD"})
    assert active == {}


# ── threshold stack ──────────────────────────────────────────────────────

def test_thresholds_small_account_caps():
    th = compute_thresholds("trend", "primed", 1.0, 1.0, None, small_account=True)
    assert th.tp1_pct == 15.0
    assert th.tp2_pct == 25.0


def test_thresholds_chop_tighter_than_trend():
    # 15% doctrine: chop and trend share the TP1 base (15); chop tightens
    # through a LOWER TP2 (20 vs 25) and a HIGHER harvest ratio (bank more
    # per trim in mean-reverting tape).
    chop = compute_thresholds("chop", "idle", 1.0, 0.0, None, small_account=False)
    trend = compute_thresholds("trend", "idle", 1.0, 0.0, None, small_account=False)
    assert chop.tp1_pct == trend.tp1_pct
    assert chop.tp2_pct < trend.tp2_pct
    assert chop.harvest_ratio > trend.harvest_ratio


def test_thresholds_meta_tighten():
    base = compute_thresholds("range", "idle", 1.0, 0.0, None, small_account=False)
    meta = compute_thresholds("range", "idle", 1.0, 0.0, 0.8, small_account=False)
    assert abs(meta.tp1_pct - base.tp1_pct * 0.8) < 1e-9


# ── harvest decisions ────────────────────────────────────────────────────

def _winner_book(roe_pct=10.0, n=2, cluster_cat="unknown"):
    """n long winners at ~roe_pct ROE, im=10 each, entry 100."""
    positions, marks, cats = [], {}, {}
    syms = ["AAA-USD", "BBB-USD", "CCC-USD"][:n]
    mult = 1.0 + roe_pct / 100.0 * (10.0 / 100.0)  # pnl/im = roe → mark = entry*(1+roe*im/(size*entry*100))
    for s in syms:
        positions.append(_Pos(s, "long", 1.0, 100.0, im=10.0))
        marks[s] = 100.0 * (1 + roe_pct / 100.0 * 10.0 / 100.0 * 100.0 / 100.0)
        # simpler: pnl = (mark-100)*1 = roe% of 10 → mark = 100 + roe/10
        marks[s] = 100.0 + roe_pct / 10.0
        cats[s] = cluster_cat
    return positions, marks, cats, syms


def test_tp1_trims_profitable_members():
    t = Treasury(_Cfg)
    # 20% ROE: above TP1 (15 base × 1.25 depth = 18.75) but below TP2
    # (25 × 1.20 = 30). Members already carry TP1 orders so the 15% runaway
    # threshold does not double-order them.
    positions, marks, cats, syms = _winner_book(roe_pct=20.0)
    ledger = _build(t, positions, marks, cats=cats)
    active = t.group_active(ledger, set())
    d = _decide(t, ledger, active)
    assert len(d.orders) == 2
    for o in d.orders:
        assert o.reason == "treasury_tp1"
        assert o.partial is True
        # harvest 0.60 × depth 0.80 = 0.48 → floor(1.0*0.48/0.001)*0.001
        assert abs(o.size - 0.48) < 1e-9


def test_tp2_closes_all_profitable_full():
    t = Treasury(_Cfg)
    positions, marks, cats, syms = _winner_book(roe_pct=35.0)
    # force thresholds low so ROE >= tp2
    t2 = Treasury(_Cfg)
    ledger = _build(t2, positions, marks, cats=cats)
    active = t2.group_active(ledger, set())
    d = t2.decide(ledger, active, cascade_phase="idle", meta_tp_mult=None,
                  balance=5000.0, cooldowns={}, now=NOW_S, now_ms=NOW_MS,
                  step_fn=_step, min_notional_fn=_min_notional)
    # balance 5000 → not small account; ROE 35 >= tp2 25*1.2(depth)=30
    assert d.orders and all(o.reason == "treasury_tp2" for o in d.orders)
    assert all(o.partial is False for o in d.orders)


def test_trailing_lock_fires_on_giveback():
    t = Treasury(_Cfg)
    # Seed a 20% peak, then decide at 3% ROE: below TP1 (5.0), but a 60%+
    # giveback from peak → TP1-level harvest with trail_lock reason.
    # size 5.0 keeps cluster pnl (2 × 5 × 0.3 = 3.0) above the $1 min guard.
    t._peak_roe[CLUSTER_CRYPTO] = 20.0
    positions = [
        _Pos("AAA-USD", "long", 5.0, 100.0, im=20.0),
        _Pos("BBB-USD", "long", 5.0, 100.0, im=20.0),
    ]
    marks = {"AAA-USD": 100.12, "BBB-USD": 100.12}  # 3% ROE each, pnl 1.2 total
    ledger = _build(t, positions, marks)
    active = t.group_active(ledger, set())
    d = _decide(t, ledger, active)
    assert d.orders and all(o.reason == "treasury_trail_lock" for o in d.orders)


def test_cooldown_blocks_refire_not_ledger_membership():
    t = Treasury(_Cfg)
    positions, marks, cats, syms = _winner_book(roe_pct=20.0)
    ledger = _build(t, positions, marks, cats=cats)
    active = t.group_active(ledger, set())
    d = _decide(t, ledger, active, cooldowns={syms[0]: NOW_S + 60})
    assert all(o.symbol != syms[0] for o in d.orders)
    # cooling position still counted in cluster ROE (book visible)
    assert d.book_margin == 20.0


def test_runaway_trim_banks_half():
    t = Treasury(_Cfg)
    # AAA runaway at 20% ROE (threshold 15%), BBB slightly red so the cluster
    # itself stays below harvest thresholds — isolates the runaway path.
    positions = [
        _Pos("AAA-USD", "long", 1.0, 100.0, im=10.0),
        _Pos("BBB-USD", "long", 1.0, 100.0, im=10.0),
    ]
    marks = {"AAA-USD": 102.0, "BBB-USD": 99.5}
    ledger = _build(t, positions, marks)
    active = t.group_active(ledger, set())
    d = _decide(t, ledger, active)
    trims = [o for o in d.orders if o.reason == "treasury_runaway_trim"]
    assert len(trims) == 1
    assert trims[0].symbol == "AAA-USD"
    assert abs(trims[0].size - 0.5) < 1e-9
    assert trims[0].partial is True


def test_runaway_trim_trend_room():
    t = Treasury(_Cfg)
    positions = [
        _Pos("AAA-USD", "long", 1.0, 100.0, im=10.0),
        _Pos("BBB-USD", "long", 1.0, 100.0, im=10.0),
    ]
    # 18% ROE: above flat 15% but below trend-widened 22.5%
    marks = {"AAA-USD": 101.8, "BBB-USD": 100.0}
    ledger = _build(t, positions, marks, day_types={"AAA-USD": "trend"})
    active = t.group_active(ledger, set())
    d = _decide(t, ledger, active)
    assert not [o for o in d.orders if o.reason == "treasury_runaway_trim"]


def test_margin_recycle_cuts_oldest_stale_flat():
    t = Treasury(_Cfg)
    positions = [
        _Pos("OLD-USD", "long", 1.0, 100.0, im=100.0, age_ms=3_600_000),
        _Pos("NEW-USD", "long", 1.0, 100.0, im=100.0, age_ms=60_000),
    ]
    # book margin 200 >= 0.75 * 250 balance; both flat (ROE ~0)
    marks = {"OLD-USD": 100.05, "NEW-USD": 100.05}
    ledger = _build(t, positions, marks)
    d = _decide(t, ledger, {}, balance=250.0)
    recycles = [o for o in d.orders if o.reason == "treasury_margin_recycle"]
    assert len(recycles) == 1
    assert recycles[0].symbol == "OLD-USD"


def test_margin_recycle_needs_pressure():
    t = Treasury(_Cfg)
    positions = [_Pos("OLD-USD", "long", 1.0, 100.0, im=100.0, age_ms=3_600_000)]
    ledger = _build(t, positions, {"OLD-USD": 100.0})
    # margin 100 < 0.75 * 500 → no recycle
    d = _decide(t, ledger, {}, balance=500.0)
    assert not [o for o in d.orders if o.reason == "treasury_margin_recycle"]


def test_loss_cut_picks_worst():
    t = Treasury(_Cfg)
    positions = [
        _Pos("BAD-USD", "long", 1.0, 100.0, im=10.0, age_ms=600_000),
        _Pos("OK-USD", "long", 1.0, 100.0, im=10.0, age_ms=600_000),
    ]
    # BAD -20% ROE, OK -1% → book ROE -10.5% < -3
    marks = {"BAD-USD": 98.0, "OK-USD": 99.9}
    ledger = _build(t, positions, marks)
    d = _decide(t, ledger, {})
    assert len(d.orders) == 1
    assert d.orders[0].reason == "portfolio_loss_cut"
    assert d.orders[0].symbol == "BAD-USD"


def test_loss_cut_min_hold_grace():
    t = Treasury(_Cfg)
    positions = [
        _Pos("BAD-USD", "long", 1.0, 100.0, im=10.0, age_ms=10_000),
        _Pos("OK-USD", "long", 1.0, 100.0, im=10.0, age_ms=10_000),
    ]
    marks = {"BAD-USD": 98.0, "OK-USD": 99.9}
    ledger = _build(t, positions, marks)
    d = _decide(t, ledger, {})
    assert d.orders == []
    assert d.loss_cut_grace is True


def test_trim_dust_falls_back_to_full_close():
    t = Treasury(_Cfg)
    # Chop day-type: harvest 0.85 × depth 0.80 = 0.68. AAA is small enough
    # that the 32% remainder is sub-min-notional dust → full close instead.
    # AAA 40% ROE (pnl 4.0), BBB flat: cluster ROE 20% sits between chop TP1
    # (15 × 1.25 = 18.75) and TP2 (20 × 1.20 = 24). AAA's TP1 order lands
    # first, so the 15% runaway threshold never double-orders it.
    positions = [
        _Pos("AAA-USD", "long", 0.2, 100.0, im=10.0),
        _Pos("BBB-USD", "long", 1.0, 100.0, im=10.0),
    ]
    marks = {"AAA-USD": 120.0, "BBB-USD": 100.0}
    ledger = _build(t, positions, marks,
                    day_types={"AAA-USD": "chop", "BBB-USD": "chop"})
    active = t.group_active(ledger, set())
    d = _decide(t, ledger, active)
    trims = [o for o in d.orders if o.symbol == "AAA-USD"]
    assert len(trims) == 1
    assert trims[0].partial is False   # remainder dust → full close
    assert abs(trims[0].size - 0.2) < 1e-9


def test_no_double_orders_same_symbol():
    t = Treasury(_Cfg)
    # AAA qualifies for both TP1 (cluster at threshold) and runaway trim
    positions, marks, cats, syms = _winner_book(roe_pct=20.0)
    ledger = _build(t, positions, marks, cats=cats)
    active = t.group_active(ledger, set())
    d = _decide(t, ledger, active)
    order_syms = [o.symbol for o in d.orders]
    assert len(order_syms) == len(set(order_syms))


def test_kill_switch_cfg():
    class _Off(_Cfg):
        treasury_enabled = False
    t = Treasury(_Off)
    assert t.enabled is False
