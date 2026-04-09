#!/usr/bin/env python3
"""
ARIA MarketState Demo

This script demonstrates the complete MarketState analysis system.
It creates mock data and runs all 6 tiers of analysis to generate trading signals.
"""

import asyncio
import sys
import os
from datetime import datetime
from pathlib import Path

# Add the project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.market_state import MarketState
from core.signal_generator import SignalGenerator
from core.data_processor import DataProcessor
from core.market_engine import MarketEngine
from core.config import Settings


async def demo_market_state_analysis():
    """Demonstrate MarketState analysis with mock data"""
    
    print("=== ARIA MarketState Analysis Demo ===\n")
    
    # Create a mock config
    config = Settings()
    config.assets = ["BTC", "ETH"]
    config.mode = "paper"
    
    # Create the signal generator
    signal_generator = SignalGenerator()
    data_processor = DataProcessor()
    
    # Generate mock market data for BTC
    print("1. Generating mock market data for BTC...")
    mock_market_data = create_mock_market_data("BTC")
    
    # Process the data
    print("2. Processing market data...")
    processed_data = data_processor.process_market_data(
        "BTC",
        mock_market_data["orderbook_store"],
        mock_market_data["mark_price_store"],
        mock_market_data["candle_buffers"],
        mock_market_data["trade_flow_store"]
    )
    
    # Generate MarketState
    print("3. Running 6-tier analysis...")
    market_state = signal_generator.generate_market_state("BTC", processed_data)
    
    # Display results
    print("\n4. MarketState Analysis Results:")
    print("=" * 50)
    
    print(f"\nSymbol: {market_state.symbol}")
    print(f"Timestamp: {datetime.fromtimestamp(market_state.timestamp_ms / 1000)}")
    
    print(f"\n--- TIER 1: MACRO ---")
    print(f"Bias: {market_state.macro_bias.upper()}")
    print(f"Source: {market_state.macro_source}")
    print(f"Confidence: {market_state.macro_confidence:.2f}")
    
    print(f"\n--- TIER 2: REGIME ---")
    print(f"Regime: {market_state.regime.replace('_', ' ').title()}")
    print(f"Leading Asset: {market_state.leading_asset}")
    print(f"Lagging Asset: {market_state.lagging_asset}")
    
    print(f"\n--- TIER 3: STRUCTURE ---")
    print(f"Market Type: {market_state.market_type.title()}")
    print(f"ATR: {market_state.atr:.2f}")
    print(f"ATR vs Baseline: {market_state.atr_vs_baseline:.2f}x")
    
    print(f"\n--- TIER 4: MICROSTRUCTURE ---")
    print(f"Sweep: {market_state.sweep.replace('_', ' ').title()}")
    print(f"Reclaim: {'Yes' if market_state.reclaim else 'No'}")
    print(f"Imbalance: {market_state.imbalance:.2f}")
    print(f"Absorption: {'Yes' if market_state.absorption else 'No'}")
    print(f"Divergence: {market_state.divergence_signal.replace('_', ' ').title()}")
    print(f"Mark-Local Spread: {market_state.mark_local_spread_pct:.3f}%")
    
    print(f"\n--- TIER 5: FUNDING ---")
    print(f"Funding Class: {market_state.funding_class.replace('_', ' ').title()}")
    
    print(f"\n--- TIER 6: MAG SIGNAL ---")
    print(f"MAG Active: {'Yes' if market_state.mag_active else 'No'}")
    print(f"MAG Direction: {market_state.mag_direction.upper()}")
    print(f"Lag Remaining: {market_state.mag_lag_remaining_min} min")
    
    print(f"\n--- FINAL SIGNAL ---")
    print(f"Coherence Score: {market_state.coherence_score}/6")
    print(f"Size Multiplier: {market_state.size_multiplier:.1f}x")
    print(f"Trade Direction: {market_state.trade_direction.upper()}")
    
    if market_state.invalidation_reason:
        print(f"Invalidation Reason: {market_state.invalidation_reason}")
    
    # Signal strength
    signal_strength = market_state.get_signal_strength()
    print(f"Signal Strength: {signal_strength:.2f}")
    
    # Valid signal check
    is_valid = market_state.is_valid_signal()
    print(f"Valid Signal: {'YES' if is_valid else 'NO'}")
    
    # Get signal summary
    print(f"\n5. Signal Summary:")
    print("=" * 30)
    summary = signal_generator.get_signal_summary("BTC")
    
    print(f"Total Signals Generated: {summary['total_signals']}")
    print(f"Long Signals: {summary['long_signals']}")
    print(f"Short Signals: {summary['short_signals']}")
    print(f"No Signal: {summary['none_signals']}")
    print(f"Average Coherence: {summary['avg_coherence']:.2f}")
    print(f"Average Size Multiplier: {summary['avg_size_multiplier']:.2f}")
    
    # Performance metrics
    print(f"\n6. Performance Metrics:")
    print("=" * 30)
    performance = signal_generator.get_performance_metrics()
    
    print(f"Total Signals: {performance['total_signals_generated']}")
    print(f"Valid Signals: {performance['valid_signals']}")
    print(f"Signal Validity Rate: {performance['signal_validity_rate']:.1f}%")
    print(f"Average Coherence: {performance['avg_coherence_score']:.2f}")
    
    print(f"\n=== Demo Complete ===")


def create_mock_market_data(symbol: str) -> dict:
    """Create mock market data for demonstration"""
    from data.orderbook_store import OrderbookStore
    from data.mark_price_store import MarkPriceStore
    from data.candle_buffer import CandleBuffer, Candle
    from data.trade_flow_store import TradeFlowStore, Trade
    
    # Base price for the symbol
    base_price = 50000.0 if symbol == "BTC" else 3000.0
    
    # Mock orderbook store
    orderbook_store = OrderbookStore(symbol)
    
    # Add mock orderbook data
    for i in range(10):
        bid_price = base_price - (i * 10)
        ask_price = base_price + (i * 10)
        bid_size = 10.0 - (i * 0.5)
        ask_size = 10.0 - (i * 0.5)
        
        orderbook_store.update_bids([(bid_price, bid_size)])
        orderbook_store.update_asks([(ask_price, ask_size)])
    
    # Mock mark price store
    mark_price_store = MarkPriceStore(symbol)
    mark_price_store.update(base_price * 1.001, base_price, datetime.now().timestamp() * 1000)
    
    # Mock candle buffers
    candle_buffers = {
        "1m": CandleBuffer(symbol, "1m"),
        "15m": CandleBuffer(symbol, "15m")
    }
    
    # Add mock candles
    now = datetime.now().timestamp() * 1000
    for i in range(20):
        candle = Candle(
            open_time=now - (20 - i) * 60000,
            open=base_price * (1 + (i - 10) * 0.001),
            high=base_price * (1 + (i - 10) * 0.001 + 0.002),
            low=base_price * (1 + (i - 10) * 0.001 - 0.002),
            close=base_price * (1 + (i - 9) * 0.001),
            volume=100.0 + i * 5,
            close_time=now - (19 - i) * 60000
        )
        candle_buffers["1m"].add(candle)
    
    # Mock trade flow store
    trade_flow_store = TradeFlowStore(symbol)
    
    # Add mock trades
    for i in range(30):
        trade = Trade(
            timestamp_ms=now - (30 - i) * 2000,
            price=base_price * (1 + (i - 15) * 0.0001),
            size=1.0 + (i % 5) * 0.5,
            side="buy" if i % 2 == 0 else "sell",
            is_aggressor_buy=(i % 2 == 0)
        )
        trade_flow_store.add(trade)
    
    return {
        "orderbook_store": orderbook_store,
        "mark_price_store": mark_price_store,
        "candle_buffers": candle_buffers,
        "trade_flow_store": trade_flow_store
    }


if __name__ == "__main__":
    asyncio.run(demo_market_state_analysis())
