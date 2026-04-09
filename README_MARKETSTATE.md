# ARIA MarketState System

## Overview

ARIA (Advanced Real-time Intelligence Assistant) is a sophisticated market analysis system that processes market data through 6 distinct tiers to generate high-quality trading signals.

## Architecture

### Core Components

1. **MarketState**: Core data structure containing all analysis results
2. **SignalGenerator**: Main engine that coordinates all analysis tiers
3. **DataProcessor**: Processes raw market data into structured format
4. **MarketEngine**: High-level coordinator for the entire system

### Analysis Tiers

#### Tier 1: Macro Analysis (`MacroAnalyzer`)
- **Purpose**: Analyze overall market bias and macroeconomic factors
- **Inputs**: Economic data, news sentiment, institutional flow, geopolitical risk
- **Outputs**: Macro bias (bullish/bearish/neutral), confidence score

#### Tier 2: Regime Analysis (`RegimeAnalyzer`)
- **Purpose**: Identify market regime and asset rotation patterns
- **Inputs**: Asset correlations, volatility data, volume patterns
- **Outputs**: Regime type (risk_on/risk_off/rotational/confused), leading/lagging assets

#### Tier 3: Structure Analysis (`StructureAnalyzer`)
- **Purpose**: Analyze market structure and volatility patterns
- **Inputs**: Price data, volume data, ATR calculations
- **Outputs**: Market type (expansion/compression/trend/chop), ATR metrics

#### Tier 4: Microstructure Analysis (`MicrostructureAnalyzer`)
- **Purpose**: Analyze orderbook microstructure and short-term patterns
- **Inputs**: Orderbook data, trade flow, mark price
- **Outputs**: Sweep detection, imbalance, absorption, divergence signals

#### Tier 5: Funding Analysis (`FundingAnalyzer`)
- **Purpose**: Analyze funding rates and carry trade opportunities
- **Inputs**: Funding rates, mark/index prices, funding history
- **Outputs**: Funding classification, arbitrage opportunities

#### Tier 6: MAG Signal Analysis (`MAGAnalyzer`)
- **Purpose**: Generate algorithmic trading signals using multiple factors
- **Inputs**: All previous tier outputs, price action, volume profile
- **Outputs**: MAG signals, direction, strength, lag timers

## Signal Generation Process

1. **Data Collection**: Raw market data from WebSocket or API
2. **Data Processing**: Structure and clean data for analysis
3. **Tier Analysis**: Run each analysis tier independently
4. **Signal Synthesis**: Combine all tier outputs into final signal
5. **Quality Control**: Apply coherence scoring and validation
6. **Output**: Generate final MarketState with trading recommendation

## Signal Quality Metrics

- **Coherence Score**: 0-6 scale, higher = more coherent signal
- **Size Multiplier**: 0.0-1.5x, position sizing based on signal strength
- **Signal Strength**: 0.0-1.0, overall confidence in the signal
- **Validity Check**: Only signals with coherence >= 4 are considered valid

## Integration Points

### Data Sources
- **WebSocket**: Real-time orderbook, trades, candles
- **API**: Historical data, funding rates, market metrics
- **External**: News sentiment, economic data, institutional flow

### Output Destinations
- **Trading Engine**: Execute trades based on valid signals
- **Risk Management**: Position sizing and risk limits
- **Monitoring**: Real-time signal display and alerts
- **Analytics**: Performance tracking and optimization

## File Structure

```
ARIA/
core/
  market_state.py          # Core data structure
  macro_analyzer.py        # Tier 1 analysis
  regime_analyzer.py       # Tier 2 analysis
  structure_analyzer.py    # Tier 3 analysis
  microstructure_analyzer.py # Tier 4 analysis
  funding_analyzer.py      # Tier 5 analysis
  mag_analyzer.py          # Tier 6 analysis
  signal_generator.py      # Main signal engine
  data_processor.py        # Data processing
  market_engine.py         # System coordinator
data/
  websocket_manager.py     # Data collection
  orderbook_store.py       # Orderbook storage
  mark_price_store.py      # Mark price storage
  candle_buffer.py         # Candle data storage
  trade_flow_store.py      # Trade flow storage
display/
  terminal.py              # Real-time display
examples/
  market_state_demo.py     # Demonstration script
```

## Usage Examples

### Basic Signal Generation
```python
from core.signal_generator import SignalGenerator
from core.data_processor import DataProcessor

# Create components
signal_gen = SignalGenerator()
data_proc = DataProcessor()

# Process market data
processed_data = data_proc.process_market_data(
    symbol="BTC",
    orderbook_store=orderbook_store,
    mark_price_store=mark_price_store,
    candle_buffers=candle_buffers,
    trade_flow_store=trade_flow_store
)

# Generate signal
market_state = signal_gen.generate_market_state("BTC", processed_data)

# Check if signal is valid
if market_state.is_valid_signal():
    print(f"Signal: {market_state.trade_direction}")
    print(f"Coherence: {market_state.coherence_score}/6")
    print(f"Size: {market_state.size_multiplier}x")
```

### Running the Demo
```bash
cd /Users/dayodapper/CascadeProjects/ARIA
source .venv/bin/activate
pip install -r requirements_updated.txt
python examples/market_state_demo.py
```

## Configuration

The system is configured through the `Settings` class in `core/config.py`:

- **Mode**: paper/testnet/live
- **Assets**: List of symbols to analyze
- **Data Settings**: Buffer sizes, update intervals
- **Logging**: Log levels and output directory

## Performance Considerations

- **Memory Usage**: Data buffers are limited to prevent memory bloat
- **Processing Speed**: Async processing for real-time analysis
- **Signal Latency**: Optimized for sub-second signal generation
- **Scalability**: Can handle multiple symbols concurrently

## Extending the System

### Adding New Analysis Tiers
1. Create new analyzer class following existing pattern
2. Add to `SignalGenerator.generate_market_state()`
3. Update coherence scoring logic
4. Add to display components

### Adding New Data Sources
1. Extend `DataProcessor` to handle new data types
2. Update market data collection
3. Add to mock data generation for testing

### Custom Signal Logic
1. Modify coherence scoring in `SignalGenerator`
2. Add new signal validation rules
3. Update display formatting

## Testing

The system includes comprehensive testing through:
- Unit tests for each analyzer
- Integration tests for signal generation
- Mock data generation for consistent testing
- Performance benchmarking

## Monitoring and Maintenance

- **Health Checks**: WebSocket connection monitoring
- **Signal Quality**: Track coherence and validity rates
- **Performance Metrics**: Signal generation latency
- **Error Handling**: Comprehensive error logging and recovery

## Future Enhancements

- **Machine Learning**: ML-based signal enhancement
- **Multi-Asset Correlation**: Cross-asset analysis
- **Sentiment Analysis**: Real-time news/social sentiment
- **Backtesting**: Historical signal performance analysis
- **Portfolio Integration**: Multi-asset portfolio optimization
