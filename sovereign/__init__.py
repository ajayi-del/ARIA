"""
sovereign/ — ARIA's long-term wealth accumulation agent.

Sovereign operates on 6-hour cycles, holding the four SoSoValue SSI index
tokens in proportions that adapt to market regime. It hedges directional
exposure with perp shorts during confirmed bear phases.

HARD RULES (never relaxed by any code path):
  1. NEVER short perps against USSI. USSI is already delta-neutral internally.
  2. MEME.ssi exits FIRST in every rotation down.
  3. MEME.ssi enters LAST in every rotation up.
  4. Sells execute before buys in every rebalance.
  5. Only SovereignAgent can issue spot orders for SSI tokens.
  6. Minimum 6h phase duration — no phase flip on noise.
  7. Maximum 2 phase transitions per 12h — churn prevention.
  8. Residual basis risk (32.42%) is logged every cycle, never hidden.
  9. All balances from env vars — never hardcoded.
"""
