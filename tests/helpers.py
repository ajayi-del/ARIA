import time
import random
from typing import List, Dict, Any
from core.market_state import MarketState
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
            "outcome": "tp1_hit",
            "pnl_usd": 100.0 * avg_win_r,
            "initial_margin": 100.0,
            "closed_at_ms": int(time.time() * 1000)
        })
    for i in range(losses):
        entry_id = f"loss_{i}"
        journal.entries.append({
            "entry_id": entry_id,
            "symbol": "BTC-USD",
            "outcome": "stop_out",
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
