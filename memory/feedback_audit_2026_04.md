---
name: ARIA Technical Audit April 2026
description: Full 7-layer audit findings — bugs fixed, open issues, architecture notes
type: feedback
---

## Fixed in this session (2026-04-11)

1. **risk/risk_engine.py Gate 5 (coherence min):** default 4 → 3.0. Also updated `is_valid_signal()` in market_state.py to match.
2. **risk/risk_engine.py Gate 6 (R:R floor → SoDEX OB Liquidity):** Replaced static R:R floor with live bid-ask spread (≤ 20bps) + entry-side depth (≥ $500 within 0.5%) from SoDEX L2 book. `orderbook_stores` injected via new param.
3. **main.py:** Wired `orderbook_stores=orderbook_stores` into RiskEngine constructor.
4. **funding/arb_strategy.py line 188:** Fixed `float(None)` crash — `latest_price()` can return None. Now: `float(_latest or 0.0)`.
5. **execution/schemas.py:** Fixed inverted comment on `PerpsOrderItem.type` (1=limit, 2=market per SoDEX docs).

## Open Issues (not fixed — require more context)

- **Pyramid gate (tp1_hit):** `can_pyramid()` checks `tp1_hit` on position but no code path sets it from order fills. `mark_tp1_hit()` exists in PositionManager but is never called from order manager. Need fill-event handler wiring.
- **Direction conflict check (risk_engine.py L92):** Only checks `existing[0].side`, not all positions. Safe for now (max 2 per symbol, same-direction only), but semantically incomplete.
- **Liquidation formula:** Conservative but not matched against SoDEX docs. Standard formula gives liq ≈ entry*(1−1/lev+mmr); code gives slightly lower liq price. Non-critical but worth verifying.
- **OB spread not validated:** sodex_feed.py doesn't check bid < ask or high >= low on candles. Silent corruption risk.
- **arb_strategy.py entry_price == 0:** If trade_flow_store has no trades yet, entry_price=0 causes notional=0 → crash in monitoring loop. Should gate arb open on entry_price > 0.

## Architecture Observations

- EIP-712 signing is correct: payloadHash=keccak256(compact JSON of {type,params}), structHash with ABI32 nonce, typed_sig=0x01+sig_bytes.
- JSON field order enforced by insertion order in `_build_order_item()` dict. Python 3.7+ preserves insertion order. ✓
- Nonce manager is thread-safe, ms-resolution, monotonic. ✓
- Candle historical fetch correctly reverses SoDEX newest-first response. ✓
- Keepalive: WS ping every 30s (SoDEX drops at 60s silence). ✓
