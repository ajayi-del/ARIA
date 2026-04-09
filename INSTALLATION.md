# ARIA Installation Guide

## Prerequisites

- Python 3.8 or higher
- pip package manager
- Virtual environment (recommended)

## Installation Steps

### 1. Clone/Navigate to Project
```bash
cd /Users/dayodapper/CascadeProjects/ARIA
```

### 2. Create Virtual Environment
```bash
python -m venv .venv
```

### 3. Activate Virtual Environment
```bash
# On macOS/Linux:
source .venv/bin/activate

# On Windows:
.venv\Scripts\activate
```

### 4. Install Dependencies
```bash
pip install -r requirements_updated.txt
```

### 5. Set Up Environment Variables
Create a `.env` file in the project root:
```bash
cp .env.example .env
```

Edit `.env` with your configuration:
```env
# ARIA Configuration
MODE=paper
LOG_LEVEL=INFO
LOG_DIR=./logs

# WebSocket Endpoints (if using live data)
TESTNET_WS_SPOT=wss://testnet-gw.sodex.dev/ws/spot
TESTNET_WS_PERPS=wss://testnet-gw.sodex.dev/ws/perps
MAINNET_WS_SPOT=wss://mainnet-gw.sodex.dev/ws/spot
MAINNET_WS_PERPS=wss://mainnet-gw.sodex.dev/ws/perps

# Assets to Monitor
ASSETS=BTC,ETH,SOL,XAUT

# Data Settings
ORDERBOOK_MAX_AGE_MS=500
CANDLE_BUFFER_SIZE=200
LOOP_INTERVAL_MS=1000
```

### 6. Create Log Directory
```bash
mkdir -p logs
```

### 7. Verify Installation
```bash
python examples/market_state_demo.py
```

## Dependencies

### Core Dependencies
- `pydantic==2.6.4` - Data validation and settings
- `pydantic-settings==2.2.1` - Configuration management
- `structlog==24.1.0` - Structured logging
- `python-dotenv==1.0.1` - Environment variable loading

### WebSocket and Networking
- `websockets==12.0` - WebSocket client
- `httpx==0.27.0` - HTTP client
- `aiohttp>=3.8.0` - Async HTTP client

### Data Processing
- `numpy>=1.24.0` - Numerical computing
- `asyncio-mqtt>=0.13.0` - MQTT client (optional)

### Display and UI
- `rich==13.7.0` - Rich terminal output

## Troubleshooting

### Common Issues

#### 1. Import Errors
```bash
# If you get import errors, make sure you're in the project directory
cd /Users/dayodapper/CascadeProjects/ARIA
source .venv/bin/activate
```

#### 2. Missing Dependencies
```bash
# Reinstall all dependencies
pip install -r requirements_updated.txt --force-reinstall
```

#### 3. Virtual Environment Issues
```bash
# Delete and recreate virtual environment
rm -rf .venv
python -m venv .venv
source .venv/bin/activate
pip install -r requirements_updated.txt
```

#### 4. Permission Issues
```bash
# Make sure scripts are executable
chmod +x examples/market_state_demo.py
```

### Platform-Specific Issues

#### macOS
- If you get "command not found: python", try `python3`
- Make sure Xcode command line tools are installed: `xcode-select --install`

#### Linux
- Install Python development headers: `sudo apt-get install python3-dev`
- Install build tools: `sudo apt-get install build-essential`

#### Windows
- Use PowerShell instead of Command Prompt
- Make sure Python is in your PATH
- Use `python -m venv .venv` instead of `python -m venv .venv`

## Development Setup

### 1. Install Development Dependencies
```bash
pip install pytest pytest-asyncio black flake8 mypy
```

### 2. Run Tests
```bash
pytest tests/
```

### 3. Code Formatting
```bash
black .
flake8 .
mypy .
```

### 4. Run Demo
```bash
python examples/market_state_demo.py
```

## Configuration

### Environment Variables
All configuration is managed through environment variables or `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| MODE | paper | Operating mode (paper/testnet/live) |
| LOG_LEVEL | INFO | Logging level |
| LOG_DIR | ./logs | Log directory |
| ASSETS | BTC,ETH,SOL,XAUT | Assets to monitor |
| ORDERBOOK_MAX_AGE_MS | 500 | Maximum orderbook age in milliseconds |
| CANDLE_BUFFER_SIZE | 200 | Candle buffer size |
| LOOP_INTERVAL_MS | 1000 | Main loop interval in milliseconds |

### WebSocket Configuration
For live trading, configure WebSocket endpoints in your `.env` file:

```env
# Testnet
TESTNET_WS_SPOT=wss://testnet-gw.sodex.dev/ws/spot
TESTNET_WS_PERPS=wss://testnet-gw.sodex.dev/ws/perps

# Mainnet
MAINNET_WS_SPOT=wss://mainnet-gw.sodex.dev/ws/spot
MAINNET_WS_PERPS=wss://mainnet-gw.sodex.dev/ws/perps
```

## Running the System

### Paper Mode (Demo)
```bash
python examples/market_state_demo.py
```

### Full System
```bash
python main_updated.py
```

### With Custom Configuration
```bash
MODE=testnet python main_updated.py
```

## Monitoring

### Logs
Logs are written to `./logs/aria.log` by default.

### Health Checks
The system includes built-in health checks for:
- WebSocket connections
- Data quality
- Signal generation performance

### Performance Metrics
Monitor signal quality through:
- Coherence scores
- Signal validity rates
- Generation latency

## Next Steps

1. Run the demo to verify installation
2. Review the configuration options
3. Test with your own data sources
4. Customize the analysis parameters
5. Integrate with your trading system

## Support

For issues and questions:
1. Check the troubleshooting section
2. Review the demo script
3. Check the log files
4. Verify your configuration
