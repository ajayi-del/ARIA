import time
import random
from typing import List, Dict, Any
from intelligence.market_state import MarketState
from core.config import Settings
from data.candle_buffer import Candle, CandleBuffer
from execution.schemas import TradeCandidate, Position

def make_test_candles(n: int, base_price: float = 70000.0, volatility: float = 200.0, atr_multiplier: float = 1.0) -> List[Candle]:
    """Generates n realistic candles."""
    candles = []
    current_price = base_price
    now_ms = int(time.time() * 1000)
    for i in range(n):
        o = current_price
        c = o + (random.random() - 0.5) * volatility * atr_multiplier
        h = max(o, c) + random.random() * (volatility / 2)
        l = min(o, c) - random.random() * (volatility / 2)
        v = random.random() * 1000
        candles.append(Candle(
            open_time=now_ms + i * 60000,
            open=o, high=h, low=l, close=c,
            volume=v, close_time=now_ms + i * 60000 + 59999
        ))
        current_price = c
    return candles

def make_neutral_market_state(symbol: str) -> MarketState:
    """All signals neutral/none."""
    return MarketState(
        symbol=symbol,
        timestamp_ms=int(time.time() * 1000),
        macro_bias="neutral",
        macro_source="none",
        macro_confidence=0.5,
        regime="confused",
        leading_asset="none",
        lagging_asset="none",
        market_type="chop",
        atr=100.0,
        atr_vs_baseline=1.0,
        sweep="none",
        reclaim=False,
        imbalance=0.0,
        vpin=0.2,
        absorption=False,
        divergence_signal="none",
        mark_local_spread_pct=0.01,
        funding_class="neutral",
        mag_active=False,
        mag_direction="none",
        mag_lag_remaining_min=0,
        weighted_score=0.0,
        raw_score=0,
        coherence_score=0,
        size_multiplier=1.0,
        trade_direction="none"
    )

def make_aligned_market_state(symbol: str, direction: str) -> MarketState:
    """All signals aligned for direction."""
    return MarketState(
        symbol=symbol,
        timestamp_ms=int(time.time() * 1000),
        macro_bias="bullish" if direction == "long" else "bearish",
        macro_source="institutional_flow",
        macro_confidence=0.8,
        regime="risk_on" if direction == "long" else "risk_off",
        leading_asset="BTC-USD",
        lagging_asset="XAUT-USD",
        market_type="expansion",
        atr=100.0,
        atr_vs_baseline=1.5,
        sweep="buy_side" if direction == "long" else "sell_side",
        reclaim=True,
        imbalance=0.8 if direction == "long" else -0.8,
        vpin=0.8,
        absorption=True,
        divergence_signal="bullish_reversion" if direction == "long" else "bearish_reversion",
        mark_local_spread_pct=0.05,
        funding_class="extreme_negative" if direction == "long" else "extreme_positive",
        mag_active=True,
        mag_direction="bullish" if direction == "long" else "bearish",
        mag_lag_remaining_min=10,
        weighted_score=6.5,
        raw_score=6,
        coherence_score=6,
        size_multiplier=1.5,
        trade_direction=direction
    )

def make_test_candidate(symbol: str) -> TradeCandidate:
    """Valid candidate passing all gates."""
    return TradeCandidate(
        symbol=symbol,
        side="long",
        entry_price=70000.0,
        stop_price=69000.0,
        tp1_price=71000.0,
        tp2_price=72000.0,
        tp3_price=73000.0,
        size=0.01,
        initial_margin=250.0,
        leverage=3,
        rr_ratio=2.5,
        coherence_score=5,
        size_multiplier=1.0,
        signal_reason="strong_coherence",
        invalidation="stop_hit",
        timestamp_ms=int(time.time() * 1000)
    )

def make_test_risk_engine():
    """RiskEngine initialized with empty positions."""
    from risk.risk_engine import RiskEngine
    from risk.margin_engine import MarginEngine
    from risk.position_manager import PositionManager
    config = test_config()
    margin = MarginEngine()
    pm = PositionManager()
    return RiskEngine(config, margin, pm, None, None)

def make_journal_with_trades(wins: int, losses: int, avg_win_r: float = 2.0, avg_loss_r: float = -1.0):
    """Pre-populated journal for stats tests."""
    from memory.trade_journal import TradeJournal
    journal = TradeJournal(log_dir="/tmp/aria_test")
    
    # Mocking some trades
    for i in range(wins):
        entry_id = f"win_{i}"
        journal.entries.append({
            "entry_id": entry_id,
            "symbol": "BTC-USD",
            "outcome": "win",          # get_closed() filters on "win"/"loss"
            "pnl_usd": 100.0 * avg_win_r,
            "initial_margin": 100.0,
            "closed_at_ms": int(time.time() * 1000)
        })
    for i in range(losses):
        entry_id = f"loss_{i}"
        journal.entries.append({
            "entry_id": entry_id,
            "symbol": "BTC-USD",
            "outcome": "loss",         # get_closed() filters on "win"/"loss"
            "pnl_usd": 100.0 * avg_loss_r,
            "initial_margin": 100.0,
            "closed_at_ms": int(time.time() * 1000)
        })
    return journal

def mock_position(side: str = "long", entry_price: float = 70000.0, tp1_price: float = 71000.0) -> Position:
    """Position with sensible defaults."""
    return Position(
        symbol="BTC-USD",
        side=side,
        entry_price=entry_price,
        size=0.01,
        stop_price=entry_price - 1000 if side == "long" else entry_price + 1000,
        tp1_price=tp1_price,
        tp2_price=tp1_price + 1000 if side == "long" else tp1_price - 1000,
        tp3_price=tp1_price + 2000 if side == "long" else tp1_price - 2000,
        liq_price=60000.0 if side == "long" else 80000.0,
        initial_margin=250.0,
        leverage=3,
        opened_at_ms=int(time.time() * 1000)
    )

def make_candle_buffers(symbol: str) -> Dict[str, CandleBuffer]:
    """1m and 15m buffers with test data."""
    return {
        "1m": CandleBuffer(symbol=symbol, interval="1m"),
        "15m": CandleBuffer(symbol=symbol, interval="15m")
    }

def test_config() -> Settings:
    """Config in paper mode for tests."""
    config = Settings()
    config.mode = "paper"
    config.assets = ["BTC-USD", "ETH-USD", "SOL-USD", "XAUT-USD", "BNB-USD", "LINK-USD", "AVAX-USD"]
    return config


# ── Phase 12-13 Institutional Test Helpers ────────────────────────────────────

def make_test_personality_engine():
    """Create a fresh PersonalityEngine / MarketPersonalityEngine."""
    from intelligence.personality import PersonalityEngine
    return PersonalityEngine(config=test_config())


def make_test_context(
    symbol: str = "BTC-USD",
    coherence: float = 5.0,
    direction: str = "long",
    htf: str = "neutral",
    regime: str = "confused",
    regime_confidence: float = 0.7,
    cascade_phase: str = "idle",
    cascade_direction: str = "none",
    cascade_zscore: float = 0.0,
    cascade_notional: float = 0.0,
    aftermath_signals: int = 0,
    atr_vs_baseline: float = 1.0,
    calendar_regime: str = "CLEAR",
    hours_to_event=None,
    daily_pnl_pct: float = 0.0,
    session_win_rate: float = 0.5,
    basis_stress_count: int = 0,
    rpc_health_score: float = 1.0,
    freeze_active: bool = False,
    freeze_elapsed_s: float = 0.0,
    xaut_direction: str = "neutral",
    xaut_mult: float = 1.0,
    # SOVEREIGN fields
    stake_balance: float = 0.0,
    sovereign_budget: float = 0.0,
    component_signals: dict = None,
    best_divergence: tuple = ("", 0.0),
):
    """Build a PersonalityContext for testing with sensible defaults."""
    from intelligence.personality import PersonalityContext
    return PersonalityContext(
        symbol=symbol,
        direction=direction,
        coherence=coherence,
        htf=htf,
        cascade_phase=cascade_phase,
        cascade_direction=cascade_direction,
        cascade_zscore=cascade_zscore,
        cascade_notional=cascade_notional,
        aftermath_signals=aftermath_signals,
        regime=regime,
        regime_confidence=regime_confidence,
        atr_vs_baseline=atr_vs_baseline,
        calendar_regime=calendar_regime,
        hours_to_event=hours_to_event,
        daily_pnl_pct=daily_pnl_pct,
        session_win_rate=session_win_rate,
        basis_stress_count=basis_stress_count,
        rpc_health_score=rpc_health_score,
        freeze_active=freeze_active,
        freeze_elapsed_s=freeze_elapsed_s,
        xaut_direction=xaut_direction,
        xaut_mult=xaut_mult,
        stake_balance=stake_balance,
        sovereign_budget=sovereign_budget,
        component_signals=component_signals or {},
        best_divergence=best_divergence,
    )


def make_test_context_cache():
    """Create a default-initialised PersonalityContextCache."""
    from intelligence.personality import PersonalityContextCache
    return PersonalityContextCache()


def make_warmed_context_cache():
    """Create a PersonalityContextCache with all fields pre-loaded."""
    from intelligence.personality import PersonalityContextCache
    cache = PersonalityContextCache()
    cache.update_cascade("idle", "none", 0.0, 0.0, 0)
    cache.update_regime("confused", 0.5, "neutral", 1.0)
    cache.update_atr("BTC-USD", 0.99)
    cache.update_atr("ETH-USD", 0.95)
    cache.update_calendar({})
    cache.update_basis_stress(0)
    cache.update_performance(0.0, 0.5)
    cache.update_rpc_health(0, True)
    cache.update_sovereign(0.0, 0.0, {})
    return cache


def make_test_personality_state(
    personality_name: str = "FLOW",
    size_mult: float = 1.0,
    confidence: float = 0.62,
):
    """Create a PersonalityParams for a specific personality."""
    from intelligence.personality import _INTERNAL_PARAMS, Personality
    p = Personality(personality_name)
    return _INTERNAL_PARAMS[p]


def make_test_context_object():
    """Create a PersonalityContext for ML feature extraction tests."""
    return make_test_context(
        symbol="BTC-USD",
        coherence=5.0,
        direction="long",
        htf="bullish",
        regime="risk_on",
        cascade_phase="idle",
        rpc_health_score=1.0,
        daily_pnl_pct=0.0,
        session_win_rate=0.5,
        basis_stress_count=0,
    )


def make_market_state_no_micro(symbol: str):
    """Return a MarketState with all microstructure signals zeroed."""
    # MarketState is a frozen Pydantic model — use model_copy(update=...) to
    # produce a new instance with zero Tier-4 fields.
    state = make_neutral_market_state(symbol)
    return state.model_copy(update={
        "sweep":    "none",
        "vpin":     0.0,
        "imbalance": 0.0,
        "absorption": False,
        "reclaim":  False,
    })


def make_test_budget_manager(balance: float = 1000.0):
    """Return an initialised BudgetManager."""
    from core.budget_manager import BudgetManager
    bm = BudgetManager(test_config(), balance)
    bm.initialise()
    return bm


def make_sovereign_context_cache(stake_usd: float = 200.0, budget_usd: float = 8.0, z_scores: dict = None):
    """Create a PersonalityContextCache pre-loaded with SOVEREIGN fields."""
    from intelligence.personality import PersonalityContextCache
    cache = make_warmed_context_cache()
    cache.update_sovereign(
        stake_balance=stake_usd,
        sovereign_budget=budget_usd,
        component_signals=z_scores or {"TSLA-USD": -2.1, "GOOGL-USD": -1.7},
    )
    return cache


def make_test_prediction(
    pred_id: str,
    confidence: float = 0.65,
    agent: str = "perp",
    symbol: str = "BTC-USD",
    direction: str = "long",
    personality: str = "flow",
    ml_probability: float = 0.55,
    coherence: float = 5.0,
    entry_price: float = 75000.0,
    predicted_exit: float = 76500.0,
):
    """Create a PredictionRecord for testing."""
    import time as _t
    from intelligence.prediction_market import PredictionRecord
    return PredictionRecord(
        id=pred_id,
        agent=agent,
        personality=personality,
        symbol=symbol,
        direction=direction,
        confidence=confidence,
        ml_probability=ml_probability,
        coherence=coherence,
        entry_price=entry_price,
        predicted_exit=predicted_exit,
        timestamp_ms=int(_t.time() * 1000),
    )
