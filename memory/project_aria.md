---
name: ARIA Project Context
description: Autonomous trading agent on SoDEX/ValueChain L1 — architecture, exchange params, risk gate spec
type: project
---

ARIA is a 10-phase autonomous trading agent targeting SoDEX perps mainnet (Chain ID 286623).

**Exchange endpoints:**
- WS perps: wss://mainnet-gw.sodex.dev/ws/perps
- REST perps: https://mainnet-gw.sodex.dev/api/v1/perps

**Signing:** EIP-712, domain name="futures", prepend 0x01 to sig bytes. payloadHash = keccak256(compact JSON of full {type,params} wrapper). HTTP body = params only.

**SoDEX order type values (confirmed from SDK docs):** type=1=LIMIT, type=2=MARKET, timeInForce=1=GTC, timeInForce=3=IOC, modifier=1=NORMAL.

**Risk gate spec (current):**
0. Calendar BLOCK | 1. VaR | 2. Symbol count | 3. Pyramid | 4. Direction conflict
5. Coherence minimum 3.0 (changed from 4.0 on 2026-04-11)
6. SoDEX OB Liquidity: spread ≤ 20bps + entry-side depth ≥ $500 within 0.5% (replaced static R:R floor on 2026-04-11)
7. Stop safety | 8. Daily loss 3%

**Why:** Gate 5 lowered to 3.0 to allow half-size trades at marginal coherence. Gate 6 replaced with SoDEX-native L2 gate to prevent entering illiquid/manipulated conditions using live orderbook data.
