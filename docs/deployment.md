# ARIA Deployment — Latency Optimisation

## Current setup
- MacBook in Berlin → SoDEX mainnet (`mainnet-gw.sodex.dev`)
- Observed RTT: ~460ms (measured via clock sync)
- Observed fill latency: 610–880ms

## Network path analysis
Run to find where the RTT is coming from:
```bash
traceroute mainnet-gw.sodex.dev | head -15
```

## GCP deployment recommendation

SoDEX / ValueChain infrastructure location determines optimal GCP region:

| Region | Location | Est. RTT |
|--------|----------|----------|
| `europe-west3` (Frankfurt) | Frankfurt, DE | ~180ms |
| `asia-southeast1` (Singapore) | Singapore | ~5ms |
| `us-central1` (Iowa) | Iowa, US | ~250ms |
| `asia-northeast1` (Tokyo) | Tokyo, JP | ~15ms |

**If SoDEX is in Singapore/Asia**: Run ARIA on `asia-southeast1` GCP VM.  
Round trip drops from 460ms → ~10ms — saving ~225ms per order.

**If SoDEX is in Europe**: Run ARIA on `europe-west3` (sageclaw/Frankfurt).  
RTT drops from 460ms → ~180ms — saving ~140ms per order.

## Code-level latency budget (Berlin baseline)

| Stage | Budget | Actual (target) |
|-------|--------|-----------------|
| Signal detection | 50ms | ~20ms |
| Risk validation | 50ms | ~15ms |
| Nietzsche + sizing | 30ms | ~2ms |
| EIP-712 signing | 50ms | ~5ms |
| HTTP POST (entry) | 460ms | ~460ms |
| Fill wait (polling) | 200ms | ~150ms |
| **Total** | **~840ms** | **~652ms** |

## HTTP/2 status
ARIA uses `httpx[http2]>=0.27.0` with H/2 enabled.  
Verify: `python3 -c "import h2; print('h2', h2.__version__)"`

Keepalive: 25s interval, 20s keepalive ping — connection stays warm.
