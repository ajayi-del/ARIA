# ARIA Examples

This directory contains example scripts and demonstrations of the ARIA trading system.

## Files

### market_state_demo.py
A comprehensive demonstration of the MarketState analysis system. This script shows:
- How to create mock market data
- How to run all 6 tiers of analysis
- How to generate trading signals
- How to interpret the results

### Running the Demo

1. Make sure you're in the ARIA project directory:
```bash
cd /Users/dayodapper/CascadeProjects/ARIA
```

2. Activate the virtual environment:
```bash
source .venv/bin/activate
```

3. Install dependencies:
```bash
pip install -r requirements_updated.txt
```

4. Run the demo:
```bash
python examples/market_state_demo.py
```

## What the Demo Shows

The demo will walk you through:

1. **Mock Data Generation**: Creates realistic market data for BTC/ETH
2. **Data Processing**: Shows how raw data is processed for analysis
3. **6-Tier Analysis**: Runs all analysis tiers:
   - Tier 1: Macro bias (bullish/bearish/neutral)
   - Tier 2: Regime detection (risk_on/risk_off/rotational/confused)
   - Tier 3: Market structure (expansion/compression/trend/chop)
   - Tier 4: Microstructure (sweeps, imbalance, absorption, divergence)
   - Tier 5: Funding analysis (extreme/positive/neutral/negative)
   - Tier 6: MAG signals (algorithmic trading signals)

4. **Signal Generation**: Shows how all tiers combine to create final trading signals
5. **Performance Metrics**: Displays signal quality and performance statistics

## Expected Output

The demo will output:
- Detailed analysis for each tier
- Final signal recommendation (long/short/none)
- Coherence score (0-6)
- Size multiplier (0.0-1.5x)
- Signal strength (0.0-1.0)
- Performance statistics

## Integration with Main System

The demo uses the same components as the main ARIA system:
- `SignalGenerator`: Main analysis engine
- `DataProcessor`: Processes raw market data
- `MarketState`: Core data structure
- All tier-specific analyzers

This makes it a perfect testing ground for understanding how the system works before integrating with live data.
