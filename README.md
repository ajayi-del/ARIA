# ARIA
### Autonomous Research & Investment Agent

> An autonomous trading agent built natively
> on SoDEX / ValueChain.
> 8 assets. 6 signal tiers. Zero human
> intervention required.

---

## What ARIA Is

ARIA is an autonomous financial agent that
perceives live market data, reasons across
a 6-tier signal stack, decides using a
mathematically rigorous risk framework,
and acts by placing bracket orders on
SoDEX — the on-chain CLOB exchange built
on ValueChain.

It is not a bot. It is not a script.
It is a trading agent that operates
continuously, manages its own risk,
and learns from its own performance.

---

## Architecture

### Data Layer
- Dual WebSocket connection to SoDEX mainnet
- 8 live feeds: orderbook, mark price,
  trade flow, candles per asset
- Coalesced event bus — 50ms dispatch cadence
- Per-symbol warm-up state machine
  (50 candles required before signals activate)

### Intelligence Layer — 6 Tiers
| Tier | Signal | Timescale |
|------|--------|-----------|
| 1 | MAG7.ssi institutional inflow | 15 min |
| 2 | 8-asset relative strength regime | 15 min |
| 3 | ATR structure classification | 1 min |
| 4 | Sweep + VPIN + divergence (hard gate) | 1 sec |
| 5 | Funding rate classification | 1 hour |
| 6 | Ostium cross-venue OI lead signal | 15 min |

Tier 4 is a hard gate. No trade fires
without a confirmed sweep or divergence
signal regardless of all other tiers.

### Coherence Scoring
- Weighted 0–7.5 scale
- Predictive signals (Tier 4, 6) weighted higher
- Signal freshness decay: exp(-λ·t·σ·100)
- Minimum score 4.0 to trade

### Execution Engine
- EIP-712 signing with cached domain separator
- Bracket orders: entry + stop + TP1/TP2/TP3
- Golden stop after TP1 (50% of entry-to-TP1)
- Persistent HTTP connection to SoDEX REST API

### Risk Engine — 9 Gates
1. Calendar BLOCK (2h before high-impact events)
2. Portfolio correlation VaR
3. Max positions per symbol (2)
4. Pyramid rule (TP1 required)
5. Direction conflict
6. Coherence minimum
7. R:R minimum (2.0)
8. Dynamic stop safety (ATR-scaled buffer)
9. Daily loss limit (3%)

### Unified Multiplier Chain


effective_size = coherence_mult (0.0–1.5)
× freshness_mult  (0.3–1.0)
× calendar_mult   (0.0–1.0)
× allocation_mult (0.65–0.90)

Four mathematically independent risk
dimensions. All logged per decision.

### Memory Layer
- 35+ fields logged per trade decision
- Net P&L after funding cost deduction
- Realistic slippage model (square-root
  market impact)
- Coherence calibrator activates at 50 trades
- Weekly LLM review of signal performance

---

## Asset Universe

| Asset | Category | Max Leverage |
|-------|----------|-------------|
| BTC-USD | Large cap | 25x |
| ETH-USD | Large cap | 20x |
| SOL-USD | Alt L1 | 20x |
| XAUT-USD | Commodity | 25x |
| BNB-USD | CEX ecosystem | 20x |
| LINK-USD | DeFi infra | 20x |
| AVAX-USD | Alt L1 | 20x |
| USTECH100-USD | Index | 10x |

---

## Calendar Intelligence

ARIA is aware of macroeconomic events
and adjusts risk automatically:

| Event | XAUT impact | BTC impact | USTECH impact |
|-------|------------|-----------|--------------|
| FOMC | 100% | 80% | 90% |
| CPI | 100% | 60% | 80% |
| NFP | 80% | 50% | 70% |
| MAG7 Earnings | 10% | 40% | 100% |

Size reduction scales continuously:
- T-24h: 0.75x size
- T-12h: 0.50x size
- T-6h: 0.25x size
- T-2h: BLOCK (no trades)
- T+30min post-event: 0.25x (recovery)

---

## Parallel Strategies

### Strategy 1 — Directional Sweep Hunter
Trades confirmed microstructure events
(sweeps + reclaims) with coherence gate.
Capital: 80% of account (dynamic).

### Strategy 2 — Funding Arb
Delta-neutral: spot long + perp short
when carry score exceeds 2.5.
Collects funding passively.
Capital: 20% of account (dynamic).

Dynamic allocation shifts between
strategies based on current funding
regime across all 8 assets.

---

## ValueChain-Native Integrations

- **SOSO gas tracker** — monitors gas
  balance before every order
- **ValueChain RPC** — reads on-chain
  liquidation cascade events directly
- **Ostium OI feed** — cross-venue lead
  signal for XAUT via DefiLlama API
- **SoSoValue SSI** — MAG7.ssi
  institutional inflow as Tier 1 signal
- **Binance public API** — CEX reference
  prices for third divergence layer

---

## Performance Targets

| Metric | Minimum | Target |
|--------|---------|--------|
| Win rate | 45% | 55% |
| Profit factor | 1.2 | 1.8 |
| SQN | 1.0 | 2.0 |
| Max drawdown | <15% | <8% |
| Avg R | 1.0R | 1.6R |

---

## Modes

```bash
# Switch modes safely
python scripts/set_mode.py paper     # synthetic data
python scripts/set_mode.py testnet   # real data, no money
python scripts/set_mode.py live      # requires CONFIRM
```

|Mode   |Data      |Execution  |Capital        |
|-------|----------|-----------|---------------|
|paper  |synthetic |simulated  |$10,000 virtual|
|testnet|real SoDEX|real orders|testnet USDC   |
|live   |real SoDEX|real orders|real USDC      |

## Running ARIA

```bash
# Setup
cd ~/CascadeProjects/ARIA
source .venv/bin/activate
pip install -r requirements.txt

# Run test suite
python tests/run_all.py

# Start
python main.py
```

Expected terminal output:

```text
ARIA — WARMING UP
BTC 23/50 | ETH 19/50 | SOL 31/50 ...

[after 50 candles per symbol]

ARIA v1.3 — READY
All 8 assets live. Signals active.
```

## Test Suite

```bash
python tests/run_all.py
```

```text
==================================================
  ARIA TEST SUITE RESULTS
==================================================
  ✓ PASS  Phase 1 — Data Layer
  ✓ PASS  Phase 2 — Intelligence
  ✓ PASS  Phase 3 — Execution
  ✓ PASS  Phase 4 — Memory
  ✓ PASS  Phase 4.5 — Funding Radar
  ✓ PASS  Phase 7 — Quant Layer
  ✓ PASS  Asset Expansion — 8 Assets
  ✓ PASS  Phase 9 — Calendar Engine
  ✓ PASS  Upgrades — Architecture
==================================================
  ALL PHASES PASSING
  ARIA is ready for testnet
==================================================
```

## Repository Structure

```text
ARIA/
├── core/           # Config, event bus,
│                   # system state machine
├── data/           # WebSocket manager,
│                   # orderbook/mark/candle/
│                   # trade flow stores
├── intelligence/   # 6-tier signal stack,
│                   # coherence scorer,
│                   # freshness decay,
│                   # stop clusters, VPIN,
│                   # relative strength,
│                   # market hours
├── execution/      # EIP-712 signer,
│                   # nonce manager,
│                   # SoDEX client,
│                   # paper client,
│                   # slippage model,
│                   # funding tracker,
│                   # order manager
├── risk/           # Margin engine,
│                   # position manager,
│                   # risk engine,
│                   # correlation engine
├── funding/        # Funding history,
│                   # funding radar,
│                   # arb strategy
├── risk_calendar/  # Event store,
│                   # multipliers,
│                   # calendar engine
├── memory/         # Trade journal,
│                   # performance tracker,
│                   # session summary,
│                   # calibrator,
│                   # weekly reviewer
├── monitoring/     # Alert system,
│                   # gas tracker
├── yield/          # SOSO yield manager
├── display/        # Rich terminal UI
├── scripts/        # Mode switcher,
│                   # signing test
├── tests/          # Full test suite
└── logs/           # Session logs,
                    # trade journal,
                    # calendar DB
```

## Built On
- **Exchange:** SoDEX (ValueChain L1)
- **Chain:** ValueChain — EVM compatible
- **Gas token:** $SOSO
- **Settlement:** USDC
- **Signing:** EIP-712
- **Language:** Python 3.11
- **Key libs:** asyncio, pydantic, websockets, httpx, eth-account, rich

## Roadmap
- Phase 1 — Data layer
- Phase 2 — Intelligence layer
- Phase 3 — Execution engine
- Phase 4 — Memory layer
- Phase 4.5 — Funding radar
- Phase 5 — Testnet deployment
- Phase 6 — Mainnet + vault scaffold
- Phase 7 — Quant signal upgrades
- Phase 8 — 8-asset universe
- Phase 9 — Calendar engine
- Phase 10 — Event architecture
- Phase 11 — 50 testnet trades
- Phase 12 — Mainnet $1,000
- Phase 13 — Vault product launch

**ARIA is a personal trading tool. Not financial advice. Trade at your own risk.**
