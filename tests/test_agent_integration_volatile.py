"""
tests/test_agent_integration_volatile.py
─────────────────────────────────────────────────────────────────────────────
Volatile-market scenario matrix for MicroAgent and StructureAgent.

Philosophical mapping
─────────────────────
Kant controls ARIA's epistemology: an agent fires ONLY when the evidence
satisfies a universalizable rule.  MicroAgent encodes this as the hard gate —
a sweep must be cluster-validated before action.  StructureAgent requires
measurable ATR expansion or directional consistency.  Without evidence,
the categorical imperative is silence (neutral).

Nietzsche controls ARIA's will-to-truth: confidence is proportional to
conviction, never dishonestly inflated.  A 0.85 reading means maximum
evidence; 0.50 means the agent demurs.  The will-to-truth rises with signal
strength and falls when the orderbook is ambiguous.

Scenarios
─────────
1.  FLASH_CRASH         — violent sell-side sweep, high VPIN, deep imbalance
2.  PUMP_AND_DUMP       — buy-side sweep with divergence reversal
3.  LIQUIDITY_VACUUM    — large spread, negligible depth, unvalidated sweep
4.  INSTITUTIONAL_BUY   — clean buy sweep + stop cluster = Kantian validation
5.  COMPRESSION         — ATR collapsed, no energy for trend
6.  ORDERLY_TREND_UP    — high trend_consistency, StructureAgent fires long
7.  EXPANSION_SPIKE     — ATR blows out 2× baseline, expansion fires
8.  VPIN_CROSSOVER      — VPIN transitions from sub-threshold to above threshold
9.  SELL_DIVERGENCE     — price rising but net flow negative → bearish reversion
10. DOUBLE_SIDED_SWEEP  — alternating imbalance, net result neutral

Integration mandate
───────────────────
These tests use *actual* CandleBuffer objects (not dicts) to prevent the
production failure where candle-buffer unit tests passed but runtime raised
"object of type 'CandleBuffer' has no len()".
"""

from __future__ import annotations

import asyncio
import pytest
import time
from unittest.mock import MagicMock
from collections import namedtuple

from data.candle_buffer import CandleBuffer, Candle
from intelligence.agents.micro_agent import MicroAgent
from intelligence.agents.structure_agent import StructureAgent


# ─────────────────────────────────────────────────────────────────────────────
# CandleBuffer fixture helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_candle(
    close: float,
    high: float | None = None,
    low: float | None = None,
    open_: float | None = None,
    volume: float = 100.0,
    offset_ms: int = 0,
) -> Candle:
    t = int(time.time() * 1000) - offset_ms
    h = high  if high  is not None else close * 1.002
    lo = low  if low   is not None else close * 0.998
    op = open_ if open_ is not None else close
    return Candle(open_time=t, open=op, high=h, low=lo, close=close,
                  volume=volume, close_time=t + 59_000)


def _buffer_with_candles(candles: list[Candle], interval: str = "1m") -> CandleBuffer:
    buf = CandleBuffer("BTC-USD", interval, maxlen=200)
    for c in candles:
        buf.add(c)
    return buf


def _flat_candles(price: float = 50_000.0, n: int = 60) -> list[Candle]:
    """Flat price, normal ATR — baseline for ATR ratio comparisons."""
    return [
        _make_candle(
            close=price,
            high=price * 1.001,
            low=price * 0.999,
            offset_ms=(n - i) * 60_000,
        )
        for i in range(n)
    ]


def _trending_candles(
    start: float = 50_000.0,
    step_pct: float = 0.003,
    n: int = 60,
    direction: str = "up",
) -> list[Candle]:
    """Monotone trend; trend_consistency approaches 1.0."""
    candles = []
    price = start
    for i in range(n):
        delta = step_pct if direction == "up" else -step_pct
        price = price * (1 + delta)
        candles.append(
            _make_candle(
                close=price,
                high=price * 1.001,
                low=price * 0.999,
                offset_ms=(n - i) * 60_000,
            )
        )
    return candles


def _expansion_candles(
    base_price: float = 50_000.0,
    n_flat: int = 50,
    n_volatile: int = 10,
    vol_mult: float = 4.0,
    direction: str = "up",
) -> list[Candle]:
    """Flat baseline followed by high-ATR expansion burst."""
    candles = _flat_candles(base_price, n_flat)
    price = base_price
    for i in range(n_volatile):
        delta = 0.006 if direction == "up" else -0.006
        price = price * (1 + delta)
        candles.append(
            _make_candle(
                close=price,
                high=price * (1 + 0.008 * vol_mult),
                low=price * (1 - 0.008 * vol_mult),
                offset_ms=(n_volatile - i) * 60_000,
            )
        )
    return candles


def _flash_crash_candles(
    start: float = 50_000.0, n: int = 60
) -> list[Candle]:
    """Price drops 8% in last 5 candles — massive ATR expansion down."""
    candles = _flat_candles(start, n - 5)
    price = start
    for i in range(5):
        price *= 0.984  # ~1.6% per candle → 8% total
        candles.append(
            _make_candle(
                close=price,
                high=price * 1.003,
                low=price * 0.980,
                offset_ms=(5 - i) * 60_000,
            )
        )
    return candles


# ─────────────────────────────────────────────────────────────────────────────
# Orderbook mock helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_ob(bid_depth: float, ask_depth: float, price: float = 50_000.0):
    """Creates a mock orderbook with 5 levels each side."""
    spread = price * 0.0001
    ob = MagicMock()
    ob.bids = [[price - spread * (i + 1), bid_depth / 5] for i in range(5)]
    ob.asks = [[price + spread * (i + 1), ask_depth / 5] for i in range(5)]
    return ob


def _make_mark(price: float):
    mp = MagicMock()
    mp.mark_price = price
    return mp


def _make_stop_cluster(price: float):
    """Returns a stop_cluster_map that always returns a cluster near `price`."""
    cluster = MagicMock()
    cluster.price = price
    sc = MagicMock()
    sc.get_clusters_near.return_value = [cluster]
    return sc


def _make_empty_clusters():
    sc = MagicMock()
    sc.get_clusters_near.return_value = []
    return sc


# ─────────────────────────────────────────────────────────────────────────────
# MicroAgent factory
# ─────────────────────────────────────────────────────────────────────────────

def _micro(
    ob=None,
    mark_price: float = 50_000.0,
    vpin: float = 0.5,
    candle_buf: CandleBuffer | None = None,
    stop_clusters=None,
    net_flow: float = 0.0,
) -> MicroAgent:
    sym = "BTC-USD"
    ob_store     = {sym: ob} if ob else {}
    mark_store   = {sym: _make_mark(mark_price)}
    vpin_calc    = {sym: vpin}
    flow_store   = MagicMock()
    flow_store.net_flow = net_flow
    tf_store     = {sym: flow_store}
    candles_dict = {sym: {"1m": candle_buf}} if candle_buf else {}

    agent = MicroAgent(
        orderbook_stores  = ob_store,
        mark_price_stores = mark_store,
        trade_flow_stores = tf_store,
        candle_buffers    = candles_dict,
        stop_cluster_map  = stop_clusters,
        vpin_calculator   = vpin_calc,
        symbols           = [sym],
    )
    return agent


# ─────────────────────────────────────────────────────────────────────────────
# StructureAgent factory
# ─────────────────────────────────────────────────────────────────────────────

def _structure(candle_buf: CandleBuffer) -> StructureAgent:
    sym = "BTC-USD"
    agent = StructureAgent(
        candle_buffers={sym: {"1m": candle_buf}},
        symbols=[sym],
    )
    return agent


# ─────────────────────────────────────────────────────────────────────────────
# Section 1: CandleBuffer ↔ Agent integration invariants
# These tests specifically guard against the production failure where
# CandleBuffer had no __len__ and agents silently returned neutral.
# ─────────────────────────────────────────────────────────────────────────────

class TestCandleBufferIntegration:
    """Guard the production bug: CandleBuffer must work as list[dict] for agents."""

    def test_len_on_real_buffer(self):
        buf = _buffer_with_candles(_flat_candles(n=60))
        assert len(buf) == 60, "CandleBuffer.__len__ must return candle count"

    def test_slice_returns_list_of_dicts(self):
        buf = _buffer_with_candles(_flat_candles(n=60))
        sliced = buf[-20:]
        assert isinstance(sliced, list)
        assert len(sliced) == 20
        assert "close" in sliced[0]
        assert "high"  in sliced[0]
        assert "low"   in sliced[0]

    def test_single_index_returns_dict(self):
        buf = _buffer_with_candles(_flat_candles(n=30))
        item = buf[-1]
        assert isinstance(item, dict)
        assert item["close"] > 0

    def test_iteration_yields_dicts(self):
        buf = _buffer_with_candles(_flat_candles(n=10))
        for d in buf:
            assert "close" in d
            assert "open_time" in d

    def test_dict_get_method_on_slice(self):
        buf = _buffer_with_candles(_flat_candles(n=30))
        candle = buf[-1]
        # Agents use c.get("high", 0) — this must not raise
        assert candle.get("high", 0) > 0
        assert candle.get("nonexistent", 99) == 99

    def test_structure_agent_does_not_fail_with_real_buffer(self):
        """StructureAgent must not raise TypeError from len(candles_1m)."""
        buf = _buffer_with_candles(_flat_candles(n=60))
        agent = _structure(buf)
        out = asyncio.get_event_loop().run_until_complete(
            agent.perceive("BTC-USD", reason="test")
        )
        # Agent ran without exception — the result can be anything
        assert out is not None
        assert out.symbol == "BTC-USD"

    def test_micro_agent_does_not_fail_with_real_buffer(self):
        """MicroAgent must not raise TypeError from len(candle_buf)."""
        buf = _buffer_with_candles(_flat_candles(n=60))
        ob  = _make_ob(bid_depth=200.0, ask_depth=100.0)  # buy-side imbalance
        agent = _micro(ob=ob, candle_buf=buf)
        out = asyncio.get_event_loop().run_until_complete(
            agent.perceive("BTC-USD", reason="test")
        )
        assert out is not None


# ─────────────────────────────────────────────────────────────────────────────
# Section 2: Volatile market scenario matrix — MicroAgent
#
# Philosophical frame:
#   Each scenario is a probe of the Kantian hard gate.
#   The agent fires only when the categorical imperative holds:
#     "Act only on sweeps that can be universally validated by stop clusters."
#   Nietzsche's will-to-truth: confidence ∝ evidence strength.
# ─────────────────────────────────────────────────────────────────────────────

class TestMicroAgentVolatileScenarios:
    """
    Volatile market scenario matrix.
    Maps scenario → expected behavior under Kant/Nietzsche framework.
    """

    SYM = "BTC-USD"

    # ── Scenario 1: FLASH CRASH ───────────────────────────────────────────────
    # Violent sell-side sweep; high ask-depth imbalance; high VPIN.
    # Kant: sweep is real — clusters at the stop level → fires short.
    # Nietzsche: VPIN=0.92 → confidence = min(0.85, 0.55 + 0.92×0.30) = 0.826
    def test_flash_crash_fires_short(self):
        ob = _make_ob(bid_depth=50.0, ask_depth=400.0, price=50_000)
        sc = _make_stop_cluster(50_000 * 1.01)
        agent = _micro(ob=ob, mark_price=50_000.0, vpin=0.92, stop_clusters=sc)
        out = asyncio.get_event_loop().run_until_complete(
            agent.perceive(self.SYM, reason="flash_crash")
        )
        assert out.fired
        assert out.direction == "short"
        assert out.raw_data["sweep"] == "sell_side"
        assert out.raw_data["sweep_validated"]
        assert out.confidence >= 0.75, (
            f"Flash crash VPIN=0.92 should yield high confidence; got {out.confidence}"
        )
        # Nietzsche invariant: confidence proportional to VPIN strength
        assert out.confidence <= 0.85

    # ── Scenario 2: PUMP AND DUMP — buy sweep, then divergence ────────────────
    # Phase 1: buy-side sweep fires long (Kantian gate passes).
    # Phase 2: price rises but net_flow is negative → bearish divergence.
    # This tests that the agent correctly reads flow data through CandleBuffer.
    def test_pump_buy_sweep_validated(self):
        ob = _make_ob(bid_depth=500.0, ask_depth=80.0, price=50_000)
        sc = _make_stop_cluster(49_500)
        agent = _micro(ob=ob, mark_price=50_000.0, vpin=0.75, stop_clusters=sc)
        out = asyncio.get_event_loop().run_until_complete(
            agent.perceive(self.SYM, reason="pump_phase1")
        )
        assert out.fired
        assert out.direction == "long"
        assert out.raw_data["sweep_validated"]

    def test_dump_divergence_fires_short(self):
        """
        Price has risen (last close > first close in window) but net_flow < 0.
        MicroAgent detects bearish reversion divergence → short.
        """
        # Build 20 candles with upward price (pump) and negative net flow
        rising = [
            _make_candle(
                close=50_000.0 + i * 50,
                offset_ms=(20 - i) * 60_000,
            )
            for i in range(20)
        ]
        buf = _buffer_with_candles(rising)
        # No active sweep (balanced book), but divergence signal from net_flow
        ob = _make_ob(bid_depth=100.0, ask_depth=100.0)
        agent = _micro(
            ob=ob,
            mark_price=50_950.0,
            vpin=0.50,
            candle_buf=buf,
            net_flow=-50_000,  # large negative flow → distribution
        )
        out = asyncio.get_event_loop().run_until_complete(
            agent.perceive(self.SYM, reason="pump_divergence")
        )
        # Divergence fires only if price_trend > 0.1% AND net_flow < 0
        # price_trend = closes[-1] - closes[-10] = (50k + 19*50) - (50k + 9*50)
        #             = 50*10 = 500, which is 500/50950 ≈ 0.0098 → > 0.001 threshold
        assert out.fired, "Pump/dump divergence must fire bearish reversion"
        assert out.direction == "short"
        assert out.raw_data["divergence"] == "bearish_reversion"
        assert out.confidence >= 0.60

    # ── Scenario 3: LIQUIDITY VACUUM — unvalidated sweep ──────────────────────
    # Kant: sweep detected but NO stop cluster nearby → categorical veto.
    # The agent must stay silent even with extreme imbalance.
    def test_liquidity_vacuum_does_not_fire(self):
        ob = _make_ob(bid_depth=600.0, ask_depth=20.0)  # extreme buy imbalance
        sc = _make_empty_clusters()  # NO clusters near mark price
        agent = _micro(ob=ob, mark_price=50_000.0, vpin=0.80, stop_clusters=sc)
        out = asyncio.get_event_loop().run_until_complete(
            agent.perceive(self.SYM, reason="liq_vacuum")
        )
        # Sweep detected but NOT validated — Kantian veto
        assert out.raw_data["sweep"] == "buy_side"
        assert not out.raw_data["sweep_validated"]
        assert not out.fired, (
            "Liquidity vacuum: sweep without cluster validation must NOT fire"
        )
        assert out.direction == "neutral"

    # ── Scenario 4: INSTITUTIONAL BUY — clean cluster-validated sweep ─────────
    # Kant: sweep + cluster = universalizable rule → fires long.
    # Nietzsche: VPIN=0.85 → confidence = min(0.85, 0.55 + 0.85×0.30) = 0.805
    def test_institutional_buy_fires_long(self):
        ob = _make_ob(bid_depth=800.0, ask_depth=100.0)
        sc = _make_stop_cluster(49_800)  # cluster just below mark
        agent = _micro(ob=ob, mark_price=50_000.0, vpin=0.85, stop_clusters=sc)
        out = asyncio.get_event_loop().run_until_complete(
            agent.perceive(self.SYM, reason="institutional_buy")
        )
        assert out.fired
        assert out.direction == "long"
        assert out.raw_data["sweep_validated"]
        expected_conf = round(min(0.85, 0.55 + 0.85 * 0.30), 3)
        assert abs(out.confidence - expected_conf) < 0.001, (
            f"Institutional buy: expected {expected_conf}, got {out.confidence}"
        )

    # ── Scenario 5: BALANCED BOOK — no sweep → Kantian silence ───────────────
    # No directional imbalance, neutral VPIN, no divergence.
    def test_balanced_book_is_silent(self):
        ob = _make_ob(bid_depth=200.0, ask_depth=200.0)
        agent = _micro(ob=ob, mark_price=50_000.0, vpin=0.50)
        out = asyncio.get_event_loop().run_until_complete(
            agent.perceive(self.SYM, reason="balanced")
        )
        assert not out.fired
        assert out.direction == "neutral"
        # Nietzsche: neutral ≤ 0.50 (epistemic humility)
        assert out.confidence <= 0.50

    # ── Scenario 6: VPIN CROSSOVER — below vs above threshold ────────────────
    # Below _VPIN_MIN_THRESHOLD (0.40): sweep fires but low conviction.
    # At _VPIN_HIGH_THRESHOLD (0.70): absorption kicks in and confidence jumps.
    def test_vpin_below_threshold_lower_confidence(self):
        ob = _make_ob(bid_depth=400.0, ask_depth=80.0)
        sc = _make_stop_cluster(49_900)
        agent_low  = _micro(ob=ob, mark_price=50_000.0, vpin=0.42, stop_clusters=sc)
        agent_high = _micro(ob=ob, mark_price=50_000.0, vpin=0.90, stop_clusters=sc)

        out_low  = asyncio.get_event_loop().run_until_complete(
            agent_low.perceive(self.SYM, reason="vpin_low")
        )
        out_high = asyncio.get_event_loop().run_until_complete(
            agent_high.perceive(self.SYM, reason="vpin_high")
        )
        # Both fire (sweep + cluster), but Nietzsche: higher VPIN = higher confidence
        assert out_low.fired  and out_high.fired
        assert out_low.confidence < out_high.confidence, (
            f"VPIN 0.42 confidence ({out_low.confidence}) must be lower than "
            f"VPIN 0.90 confidence ({out_high.confidence})"
        )

    # ── Scenario 7: SELL DIVERGENCE ──────────────────────────────────────────
    # Price falls but net_flow is positive → bullish reversion signal.
    def test_bullish_reversion_divergence(self):
        falling = [
            _make_candle(
                close=50_000.0 - i * 50,
                offset_ms=(20 - i) * 60_000,
            )
            for i in range(20)
        ]
        buf = _buffer_with_candles(falling)
        ob  = _make_ob(bid_depth=100.0, ask_depth=100.0)
        agent = _micro(
            ob=ob,
            mark_price=49_050.0,
            vpin=0.50,
            candle_buf=buf,
            net_flow=80_000,  # large positive flow → absorption while price falls
        )
        out = asyncio.get_event_loop().run_until_complete(
            agent.perceive(self.SYM, reason="sell_divergence")
        )
        assert out.fired, "Falling price + positive flow = bullish reversion must fire"
        assert out.direction == "long"
        assert out.raw_data["divergence"] == "bullish_reversion"

    # ── Scenario 8: DOUBLE-SIDED SWEEP (chaotic market) ──────────────────────
    # Near-zero imbalance (bid ≈ ask) → no sweep → neutral.
    def test_near_zero_imbalance_is_neutral(self):
        ob = _make_ob(bid_depth=201.0, ask_depth=200.0)  # imbalance = 0.0025
        agent = _micro(ob=ob, mark_price=50_000.0, vpin=0.65)
        out = asyncio.get_event_loop().run_until_complete(
            agent.perceive(self.SYM, reason="double_sided")
        )
        # Imbalance (201-200)/(201+200) = 0.0025 < 0.35 threshold → no sweep
        assert not out.fired
        assert out.raw_data["sweep"] == "none"

    # ── Scenario 9: confidence monotonicity across VPIN levels ───────────────
    # Nietzsche invariant: higher evidence = higher will-to-truth.
    def test_vpin_confidence_is_monotone(self):
        sc = _make_stop_cluster(49_900)
        ob = _make_ob(bid_depth=500.0, ask_depth=80.0)
        vpins = [0.41, 0.55, 0.70, 0.85, 1.00]
        confs = []
        for v in vpins:
            agent = _micro(ob=ob, mark_price=50_000.0, vpin=v, stop_clusters=sc)
            out = asyncio.get_event_loop().run_until_complete(
                agent.perceive(self.SYM, reason="mono_test")
            )
            confs.append(out.confidence)

        for i in range(len(confs) - 1):
            assert confs[i] <= confs[i + 1], (
                f"Confidence not monotone: vpin[{vpins[i]}]={confs[i]} > "
                f"vpin[{vpins[i+1]}]={confs[i+1]}"
            )

    # ── Scenario 10: confidence cap at 0.85 (Kantian epistemic humility) ─────
    def test_vpin_at_max_capped_at_085(self):
        sc = _make_stop_cluster(49_900)
        ob = _make_ob(bid_depth=1000.0, ask_depth=50.0)
        agent = _micro(ob=ob, mark_price=50_000.0, vpin=1.0, stop_clusters=sc)
        out = asyncio.get_event_loop().run_until_complete(
            agent.perceive(self.SYM, reason="cap_test")
        )
        assert out.fired
        assert out.confidence == 0.85, (
            f"VPIN=1.0 must cap at 0.85 (epistemic humility), got {out.confidence}"
        )

    # ── Scenario 11: no orderbook → always neutral ────────────────────────────
    def test_no_orderbook_is_always_neutral(self):
        agent = _micro(ob=None, mark_price=50_000.0, vpin=0.99)
        out = asyncio.get_event_loop().run_until_complete(
            agent.perceive(self.SYM, reason="no_ob")
        )
        assert not out.fired
        assert out.direction == "neutral"


# ─────────────────────────────────────────────────────────────────────────────
# Section 3: StructureAgent volatile market scenarios
#
# Kant maps to: the agent requires measurable, repeatable structural evidence
#   (ATR expansion or trend consistency) — no pattern, no action.
# Nietzsche maps to: confidence = will-to-structure; it rises with ATR excess
#   or trend consistency and is capped at 0.85.
# ─────────────────────────────────────────────────────────────────────────────

class TestStructureAgentVolatileScenarios:
    """
    StructureAgent scenario matrix using real CandleBuffer objects.
    """

    SYM = "BTC-USD"

    def _run(self, buf: CandleBuffer) -> object:
        agent = _structure(buf)
        return asyncio.get_event_loop().run_until_complete(
            agent.perceive(self.SYM, reason="test")
        )

    # ── Scenario 1: FLASH CRASH — expansion + downward trend ─────────────────
    def test_flash_crash_fires_short(self):
        buf = _buffer_with_candles(_flash_crash_candles())
        out = self._run(buf)
        # Flash crash should produce high ATR → expansion → down direction
        assert out.fired, f"Flash crash should fire, got {out.raw_data}"
        assert out.direction == "short", (
            f"Flash crash expansion should be short, got {out.direction}"
        )
        assert out.raw_data["market_type"] == "expansion"
        assert out.raw_data["atr_ratio"] > 1.30

    # ── Scenario 2: ORDERLY TREND UP ────────────────────────────────────────
    def test_orderly_trend_up_fires_long(self):
        buf = _buffer_with_candles(_trending_candles(direction="up", step_pct=0.002, n=60))
        out = self._run(buf)
        assert out.fired, f"Uptrend must fire, got {out.raw_data}"
        assert out.direction == "long"
        assert out.raw_data["market_type"] in ("trend", "expansion")

    # ── Scenario 3: ORDERLY TREND DOWN ──────────────────────────────────────
    def test_orderly_trend_down_fires_short(self):
        buf = _buffer_with_candles(_trending_candles(direction="down", step_pct=0.002, n=60))
        out = self._run(buf)
        assert out.fired
        assert out.direction == "short"

    # ── Scenario 4: EXPANSION SPIKE UP ──────────────────────────────────────
    def test_expansion_spike_up_fires_long(self):
        buf = _buffer_with_candles(_expansion_candles(direction="up", vol_mult=4.0))
        out = self._run(buf)
        assert out.fired
        assert out.direction == "long"
        assert out.raw_data["market_type"] == "expansion"
        # Nietzsche: high ATR excess → high confidence
        assert out.confidence >= 0.70, (
            f"Expansion spike should yield high confidence, got {out.confidence}"
        )

    # ── Scenario 5: EXPANSION SPIKE DOWN ────────────────────────────────────
    def test_expansion_spike_down_fires_short(self):
        buf = _buffer_with_candles(_expansion_candles(direction="down", vol_mult=4.0))
        out = self._run(buf)
        assert out.fired
        assert out.direction == "short"
        assert out.raw_data["market_type"] == "expansion"

    # ── Scenario 6: COMPRESSION / CHOP — Kantian silence ────────────────────
    def test_compression_is_neutral(self):
        """ATR well below baseline (tight range candles) → compression → neutral."""
        tight = [
            _make_candle(
                close=50_000.0,
                high=50_000.0 * 1.0002,
                low=50_000.0 * 0.9998,
                offset_ms=(60 - i) * 60_000,
            )
            for i in range(60)
        ]
        buf = _buffer_with_candles(tight)
        out = self._run(buf)
        # compression or chop → neutral
        assert not out.fired, (
            f"Compression market must not fire, got direction={out.direction} "
            f"market_type={out.raw_data.get('market_type')}"
        )
        # Kant: absence of evidence = silence; Nietzsche: confidence ≤ 0.50
        assert out.confidence <= 0.50

    # ── Scenario 7: WARMUP — fewer than 20 candles ──────────────────────────
    def test_warmup_is_neutral(self):
        buf = _buffer_with_candles(_flat_candles(n=15))
        out = self._run(buf)
        assert not out.fired
        assert out.raw_data.get("market_type") == "warmup"

    # ── Scenario 8: ATR ratio monotone confidence ────────────────────────────
    # Nietzsche: higher expansion → higher will-to-truth.
    def test_expansion_confidence_increases_with_atr_excess(self):
        """Confidence must rise as ATR excess grows above 1.30× threshold."""
        base = 50_000.0
        n_flat = 50

        confs = []
        for vol_mult in [1.5, 2.5, 4.0]:
            buf = _buffer_with_candles(
                _expansion_candles(base_price=base, n_flat=n_flat,
                                   vol_mult=vol_mult, direction="up")
            )
            out = self._run(buf)
            confs.append((vol_mult, out.confidence, out.raw_data.get("atr_ratio", 0)))

        # Strictly increasing confidence with vol_mult
        for i in range(len(confs) - 1):
            vm_lo, c_lo, _ = confs[i]
            vm_hi, c_hi, _ = confs[i + 1]
            assert c_lo <= c_hi, (
                f"Confidence should increase with ATR excess: "
                f"vol_mult={vm_lo} → conf={c_lo}, "
                f"vol_mult={vm_hi} → conf={c_hi}"
            )

    # ── Scenario 9: trend_consistency in raw_data ───────────────────────────
    def test_trend_consistency_present_in_raw_data(self):
        buf = _buffer_with_candles(_trending_candles(n=60))
        out = self._run(buf)
        assert "trend_consistency" in out.raw_data, (
            "trend_consistency must be in raw_data for accountability logging"
        )
        assert 0.0 <= out.raw_data["trend_consistency"] <= 1.0

    # ── Scenario 10: confidence cap at 0.85 ─────────────────────────────────
    def test_confidence_never_exceeds_085(self):
        # Maximum possible expansion
        extreme = _expansion_candles(vol_mult=10.0, n_volatile=20, direction="up")
        buf = _buffer_with_candles(extreme)
        out = self._run(buf)
        assert out.confidence <= 0.85, (
            f"Epistemic humility cap violated: {out.confidence}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Section 4: Cross-scenario comparative table
#
# Verifies the philosophical ordering: flash crash > institutional > trend >
# compression in terms of structural severity and agent conviction.
# ─────────────────────────────────────────────────────────────────────────────

class TestScenarioComparison:
    """
    Compares agent outputs across scenarios to ensure the conviction ordering
    matches the philosophical and mathematical hierarchy.

    Nietzsche's hierarchy of will-to-truth:
      Institutional sweep (VPIN=0.85, cluster) > Sweep (VPIN=0.50) > Neutral
    Kant's structural hierarchy:
      Expansion > Trend > Compression = no action
    """

    SYM = "BTC-USD"

    def test_micro_conviction_ordering(self):
        """
        Conviction order: institutional_sweep > low_vpin_sweep > neutral book.
        """
        sc = _make_stop_cluster(49_900)
        ob_sweep = _make_ob(bid_depth=500.0, ask_depth=80.0)

        # Institutional: VPIN=0.85, cluster
        agent_inst = _micro(ob=ob_sweep, mark_price=50_000.0, vpin=0.85, stop_clusters=sc)
        out_inst   = asyncio.get_event_loop().run_until_complete(
            agent_inst.perceive(self.SYM, reason="inst")
        )

        # Low VPIN sweep: VPIN=0.45, cluster
        agent_low  = _micro(ob=ob_sweep, mark_price=50_000.0, vpin=0.45, stop_clusters=sc)
        out_low    = asyncio.get_event_loop().run_until_complete(
            agent_low.perceive(self.SYM, reason="low_vpin")
        )

        # Neutral book: no sweep
        ob_neutral = _make_ob(bid_depth=200.0, ask_depth=200.0)
        agent_neu  = _micro(ob=ob_neutral, mark_price=50_000.0, vpin=0.50)
        out_neu    = asyncio.get_event_loop().run_until_complete(
            agent_neu.perceive(self.SYM, reason="neutral")
        )

        assert out_inst.confidence > out_low.confidence, (
            "Institutional sweep must outrank low-VPIN sweep"
        )
        assert out_low.confidence > out_neu.confidence, (
            "Any validated sweep must outrank neutral"
        )

    def test_structure_conviction_ordering(self):
        """
        Conviction order (fired): expansion > trend > chop/compression.
        Compression = not fired, expansion/trend = fired.
        """
        loop = asyncio.get_event_loop()

        # Expansion
        buf_exp = _buffer_with_candles(_expansion_candles(vol_mult=4.0, direction="up"))
        ag_exp  = _structure(buf_exp)
        out_exp = loop.run_until_complete(ag_exp.perceive(self.SYM, reason="exp"))

        # Strong trend
        buf_trend = _buffer_with_candles(_trending_candles(step_pct=0.002, n=60))
        ag_trend  = _structure(buf_trend)
        out_trend = loop.run_until_complete(ag_trend.perceive(self.SYM, reason="trend"))

        # Compression
        tight = [
            _make_candle(close=50_000.0, high=50_000.05, low=49_999.95,
                         offset_ms=(60 - i) * 60_000)
            for i in range(60)
        ]
        buf_comp = _buffer_with_candles(tight)
        ag_comp  = _structure(buf_comp)
        out_comp = loop.run_until_complete(ag_comp.perceive(self.SYM, reason="comp"))

        assert out_exp.fired,   "Expansion must fire"
        assert out_trend.fired, "Strong trend must fire"
        assert not out_comp.fired, "Compression must not fire"

        assert out_exp.confidence >= out_trend.confidence, (
            f"Expansion ({out_exp.confidence}) should have >= confidence than "
            f"trend ({out_trend.confidence})"
        )

    def test_kant_veto_is_unconditional(self):
        """
        The categorical imperative: no validated cluster = no fire.
        Even VPIN=1.0 must not fire without cluster validation.
        """
        ob = _make_ob(bid_depth=1000.0, ask_depth=50.0)  # extreme buy imbalance
        sc = _make_empty_clusters()  # NO clusters
        agent = _micro(ob=ob, mark_price=50_000.0, vpin=1.0, stop_clusters=sc)
        out = asyncio.get_event_loop().run_until_complete(
            agent.perceive(self.SYM, reason="kant_veto")
        )
        assert not out.fired, (
            "Kant veto is unconditional: VPIN=1.0 without cluster = no fire"
        )

    def test_nietzsche_will_rises_with_evidence(self):
        """
        Across all fired states, confidence must increase with evidence quality.
        VPIN 0.40 < 0.60 < 0.80 < 1.00 in confidence ordering.
        """
        sc = _make_stop_cluster(49_900)
        ob = _make_ob(bid_depth=500.0, ask_depth=80.0)
        vpins = [0.40, 0.60, 0.80, 1.00]
        prev_conf = 0.0
        for v in vpins:
            agent = _micro(ob=ob, mark_price=50_000.0, vpin=v, stop_clusters=sc)
            out = asyncio.get_event_loop().run_until_complete(
                agent.perceive(self.SYM, reason=f"will_v{v}")
            )
            assert out.confidence >= prev_conf, (
                f"Will-to-truth must rise: vpin={v} gave {out.confidence} "
                f"which is < previous {prev_conf}"
            )
            prev_conf = out.confidence
