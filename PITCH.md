# ARIA: The Agentic Research-to-Execution Stack
## From SoSValue Intelligence to SoDEX Settlement in Seconds

---

## The Problem SoSoValue Identified — And ARIA Solves

The workshop framed the core problem perfectly: **information is abundant, but execution is fragmented.**

Today's trader faces four fractures:

1. **Fragmented data** — Funding rates on Bybit, liquidations on-chain, sector rotation in SSI, microstructure in the order book. No single lens fuses them.
2. **No clear signal from noise** — A dozen dashboards, zero conviction. Research produces opinions; markets demand decisions.
3. **Research and execution are disconnected** — You see the cascade forming. By the time you switch tabs, size, sign the transaction, and hit submit, the edge is gone.
4. **Users cannot act fast enough** — Human latency is 300ms. Market regimes flip in 30ms.

**ARIA closes this loop.** It is not a bot. It is a *philosophical operating system* that turns SoSValue's structured intelligence into signed SoDEX orders in under one second.

---

## What ARIA Is (The One-Minute Version)

ARIA is an autonomous perpetuals trading system that lives entirely inside the SoSValue ecosystem. It reads structured intelligence from **SoSValue Terminal** (SSI indices, ETF flows, sector rotation), detects institutional liquidations on **ValueChain** via `eth_getLogs`, fuses six tiers of signal evidence through a chain of epistemic authority, and executes on **SoDEX** via EIP-712 signed orders.

**The thesis:** Markets are not mathematical. They are psychological, structural, and historical. ARIA treats philosophy as compute architecture — Kant structures the world before Nietzsche sizes into it. Conviction distills the signal. A Bayesian prediction market demands independent agent agreement. Only then does capital leave the wallet.

**The result:** 3,000+ autonomously executed live trades. Real capital. Real fees. Real drawdowns. A live journal you can read right now.

---

## How ARIA Uses the Full SoSValue Stack

The workshop asked builders to demonstrate thoughtful use of Terminal, SSI, SoDEX, and ValueChain. Here is how ARIA uses all four as a *single closed-loop system*.

### 1. SoSValue Terminal — Structured Intelligence Input

ARIA consumes structured market intelligence from SoSValue Terminal as **Tier 1 macro signals**:

- **SSI Protocol indices:** MAG7SSI, DEFISSI, MEMESSI, USSI. These are not price feeds. They are *regime detectors*. When MAG7SSI rotates leadership from large-cap to alt-L1, ARIA's regime engine shifts coherence weighting across all 26 tracked assets.
- **ETF Flows:** ARIA tracks synthetic equity momentum via SoDEX's equity perpetuals (NVDA-USD, MSFT-USD, AMZN-USD, GOOGL-USD). When ETF flow intelligence signals risk-on rotation, ARIA's macro signal engine applies a portfolio-level boost to crypto longs — because tech equities lead crypto by ~4 hours.

This is the **research layer**. SoSValue Terminal gives ARIA the "what is happening" before the chart confirms it.

### 2. SSI Protocol — On-Chain Index Exposure

ARIA does not just *read* SSI. It *trades through* it.

- The **Sovereign Agent** holds spot SSI exposure as a hedge layer. When the perp engine is net short crypto, sovereign is long MAG7SSI spot.
- SSI staking status (`SOSO_STAKED=168`) feeds directly into ARIA's fee engine, unlocking a **5% taker fee discount** (0.0380% vs. 0.0400%). ARIA calculates this discount into every position's expected edge — it knows the exact fee drag before it sizes.

This is the **capital efficiency layer**. SSI is not a side product. It is part of ARIA's risk-neutral book.

### 3. ValueChain — The Edge Layer

This is where ARIA diverges from every other trading agent.

While competitors watch *price*, ARIA watches *blockchain events*.

- **ValueChain Monitor:** ARIA runs `eth_getLogs` on ValueChain mainnet (chain ID 286623), scanning for liquidation events in real blocks.
- **Cascade Detection:** Liquidation batches are not just counted. They are classified into cascade phase: `trigger` -> `building` -> `expansion` -> `exhaustion` -> `aftermath`. Each phase has a z-score and a directional bias.
- **Anticipation, Not Reaction:** When ValueChain detects a `trigger` phase with z-score > 2.0, ARIA's cascade tracker produces a momentum signal *before* the price move is visible in the SoDEX order book.

**Example from live logs:**
```json
{"liq_60s": 14, "zscore": 1.92, "phase": "trigger", ...}
{"symbol": "market_wide", "direction": "short", "size_factor": 0.6, ...}
```

This is **Tier 4 intelligence** in ARIA's six-tier signal stack. ValueChain gives ARIA institutional visibility that retail platforms cannot access.

### 4. SoDEX — Execution Layer

Signal without execution is astrology. ARIA bridges to SoDEX with zero friction:

- **EIP-712 Signed Orders:** Every order is cryptographically signed by the SoDEX signing key. No custodial API keys. No counterparty trust.
- **Auth-Aware Routing:** ARIA knows SoDEX's auth rules by heart — GET requests use the wallet address in the URL; POST/DELETE orders carry the X-API-Key. It never misfires.
- **Min Notional Gate:** ARIA enforces the $10 minimum notional *before* exchange submission, saving failed transactions and gas.
- **Fee Tier Intelligence:** ARIA's fee engine updates in real time from SoDEX REST (`tier: 0, perp_taker: 0.0380%`). It bakes fee drag into the conviction calculation. A trade is only executed if expected edge > fee + funding + slippage.

**From signal to execution:** ValueChain detects cascade -> Kant assesses `TREND` structure -> Nietzsche sizes at 1.25x with `market` order type -> SoDEX receives signed order. **Total latency: under 1 second.**

---

## The Architecture: Input -> Insight -> Action

The workshop judges on **complete flow**. Here is ARIA's, end-to-end.

### INPUT (6 Tiers of Signal Fusion)

| Tier | Source | What It Reads |
|------|--------|---------------|
| 1 | SoSValue SSI | Sector rotation (MAG7, DEFI, MEME, US indices) |
| 2 | Equity Momentum | ETF flow-driven synthetic equity signals |
| 3 | Microstructure | Sweep ratio, VPIN, stop clusters, order imbalance |
| 4 | **ValueChain** | On-chain liquidations via `eth_getLogs` |
| 5 | Funding Regime | Bybit-seeded funding bias across assets |
| 6 | SoDEX Liq Signal | Direct liquidation feed, cross-venue lag |

### INSIGHT (The Epistemic Chain)

1. **Kant (`assess()`)** — Market structure interpreter. Classifies regime into `{ACCUMULATION, NORMAL, TREND, DISTRIBUTION, CHAOS}`. Sets `coherence_min`, `size_cap`, `order_type`. Hysteresis: 3-period confirmation. Bayesian confidence blending from empirical win-rate per structure.

2. **Conviction Engine (`compute_conviction()`)** — Pure function, zero I/O. Aggregates coherence (40%), regime alignment (25%), order flow (20%), cascade state (15%) into `[0.0, 1.0]`. Kant confidence < 0.60 gates x0.80.

3. **Nietzsche (`compute()`)** — Will-to-size engine. Looks at drawdown band and streak state. The Will Table maps `(dd_band, streak_band) -> (WillState, base_mult)`. Conviction modulates +/-50%. Elite coherence (>=8.5) bypasses all psychology. Output: `adjusted_size`, `order_type`, `will_state`.

4. **Prediction Market (`CrossAgentBetEngine`)** — Bayesian joint probability. Two independent agents must agree on symbol + direction with `p_joint >= 0.70` before a `1.5x` combined-size bet is authorized. Echo chambers excluded.

### ACTION (SoDEX Execution)

- Chancellor approves cross-position exposure
- EIP-712 signed order submitted to SoDEX
- Position tracked with mark-price TP/SL
- Fee accrues to vault ledger
- Trade journal persists to disk

**This is the closed loop the workshop asked for.** Not research *or* execution. Research *through* execution.

---

## User Value: What Is Possible With ARIA?

The workshop asked for **clear user value** and **real use cases**. Here are three.

### Use Case 1: The Solo Quant
You have a thesis that alt-season is beginning. You do not have time to watch 26 assets, monitor funding, scan for liquidations, and manually size positions. ARIA does this continuously. It reads SSI sector rotation, detects ValueChain cascade triggers, sizes based on your current drawdown, and executes on SoDEX. You wake up to a `performance_cert.md` showing what happened while you slept.

### Use Case 2: The Risk-Averse Trader
You do not want to be aggressive during a drawdown. ARIA's Nietzsche engine automatically reduces size from 1.0x to 0.25x as drawdown deepens. At >10% drawdown, it enters `DORMANT` state — full halt, no new entries. Win streaks earn the right to be aggressive again. This is not a setting. It is a *philosophy* encoded as execution logic.

### Use Case 3: The Institutional Signal Provider
You run a liquidation monitoring service on ValueChain. You want your subscribers to *act* on your signals, not just read them. ARIA is the execution layer. It turns your cascade alerts into sized, signed, risk-managed SoDEX orders in under a second. Your research becomes their P&L.

---

## Real Project, Real Traction

The workshop emphasized **milestone-based evaluation** and **continuous improvement**. ARIA is not a weekend project.

- **3 months** of continuous development
- **3,000+** autonomously executed live trades on SoDEX mainnet
- **Live journal:** `logs/trade_history.json` with timestamped entries, agent attribution, and realized R-multiples
- **Live vault tracking:** NAV, HWM, fee ledger, management fee accrual
- **Agent calibration:** `SCOUT: 139/142`, `AFTERMATH: 62/134` — real win-rates computed from resolved predictions
- **Recovery mode active:** The system is currently in drawdown recovery, raising coherence floors and capping size at 0.50x. It knows when it is bleeding.

This is not a backtest. This is a live system with scars.

---

## Roadmap: What's Next

ARIA is built for **multi-wave improvement**, matching the workshop's grant structure.

- **Wave 1 (Complete):** SoDEX + ValueChain integration. Cascade anticipation. Fee tier intelligence. Live capital.
- **Wave 2 (In Progress):** Bybit bridge for cross-venue execution. When SoDEX liquidity is thin, ARIA routes to Bybit. Cross-venue lag detection ensures no stale fills.
- **Wave 3 (Planned):** Multi-venue intelligent routing. Best execution across SoDEX, Bybit, and additional venues. SSI spot exposure as perpetual hedge.

---

## How ARIA Checks Every Workshop Box

| Workshop Criterion | ARIA's Answer |
|--------------------|---------------|
| **Clear user value** | Autonomous execution of institutional-grade signals. No manual intervention. No emotional override. |
| **Real use case** | 3,000+ live mainnet trades. Real capital. Real fees. Real journal. |
| **Complete flow** | Input (SSI + ValueChain + microstructure) -> Insight (Kant + Nietzsche + Conviction + Prediction Market) -> Action (SoDEX EIP-712 execution). |
| **Signal-to-Execution Agent** | Core product. From cascade detection to signed order in <1s. |
| **Smart Trading Dashboard** | Live terminal UI with regime, agent states, and vault metrics. |
| **Opportunity Discovery Engine** | ValueChain liquidation monitor + SSI sector rotation + funding regime aggregator. |
| **Automated Strategy Bot** | 6-tier signal fusion with philosophical risk management. |

---

## The Closing Line

> **Most agents trade. ARIA thinks.**
>
> It thinks in Kantian structure, Nietzschean will, Bayesian consensus, and on-chain truth. It has executed 3,000 trades not because it is fast, but because it is *right enough, sized right, and shut down when wrong*. That is what a research-to-execution stack should do. That is what SoSoValue makes possible. That is ARIA.

---

**Live Proof:** https://app.akindo.io/wave-hacks/JBEQXgN4Zi2jA3wA

**Journal:** Available on request. Every trade timestamped, every agent attributed, every fee accounted for.
