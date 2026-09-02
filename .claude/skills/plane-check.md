---
name: plane-check
description: Cross-scale plane-mismatch diagnostics — metrics on the Yahoo-underlying plane vs rebased synthetic perp plane (SPCX ~5.4x, USTECH100 ~41x); the fund's most recurring defect class
allowed-tools: [Bash, Read, Grep]
when_to_use: Any metric/price comparison off by a round multiple; quarantine/sanity gates firing on tradfi symbols; before ANY new cross-source price comparison
arguments:
  - name: target
    type: string
    description: "Symbol or metric suspected of plane mismatch"
---

# Plane Check Skill

Five instances and counting: 08-21 SPCX rebase quarantine (f7733d6), 08-22
mark-entry scale guard (eafedde), 08-30 mark-scale sentinel (d03b3c7),
09-01 sentinel reference (6d1a7c3) + ATR plane (ee47951). Every one was a
metric computed on one price plane compared against another.

## 1. The plane map (memorize this before touching tradfi symbols)
- SoDEX stock/index perps are REBASED SYNTHETICS: SPCX ~141 (vs SPY ~767,
  rebase ~5.4x), USTECH100 ~29112 (vs QQQ ~716, rebase ~41x).
- For the 17 tradfi_feed-owned symbols, candle_buffers hold the YAHOO
  UNDERLYING's plane. Marks/entries/klines are the VENUE plane.
- Exceptions (own candles on the venue plane): `sodex_kline_assets`
  (SILVER/COPPER) and `aster_kline_assets` (XAUT/CL). Predicate:
  `_sentinel_venue_ref_symbol(symbol, sodex+aster kline lists)`.
- % vol is plane-INVARIANT (the perp tracks the underlying 1:1 in % terms)
  — that's why `venue_plane_atr` = atr × entry / underlying_close is exact.

## 2. The diagnostic (Hasbrouck: raw values, never the event name)
Get BOTH raw numbers from the log line (the metric AND the price it's
compared against). Compute the ratio. If ratio ≈ a symbol's rebase factor
(~5.4x SPCX, ~41x USTECH100) or its inverse — it's a plane mismatch, not a
market event and not a gate bug. A ratio near 1 is NOT proof of health:
the same Yahoo reference once read a REAL 5.66x split as in-band.

## 3. The fix doctrine — repair the plane, keep the gate
The atr_sanity gate was CORRECTLY preventing broken brackets (stops sized
off $0.36 ATR against a 29112 entry). Every plane fix maps the metric onto
the consumer's plane and splices BEFORE the gate, gate untouched and armed.
Surgical bounds: fire only on the pathological reading (the gate's own
detector) + the Yahoo-owned predicate; healthy ratios bit-for-bit;
wrong-plane/missing inputs fail closed to the legacy reject.

## 4. Yahoo darkness is designed, not a defect
Yahoo is dark off-hours/weekends — fail-open sentinel paths are silent by
design, and 09-01 post-20:00 UTC staleness on equities is the normal close.
Check feed ownership and the clock before claiming a plane defect; a dark
Yahoo is only a bug when something TRADES off its staleness.

## 5. Before adding ANY new cross-source comparison
Ask: which plane is each side on? Mark vs kline, ATR vs entry, sentinel
reference vs mark — every new comparison across feed sources needs the
plane answer in its commit message, or it becomes instance #6.

## Canon lens (baked into the steps — Dayo's working books)
- **Hasbrouck** (step 2): print both raw numbers and the ratio — a plane
  claim without the arithmetic is gossip.
- **von Neumann / Sun Tzu** (step 3): the gate refusing the battle is
  defense — repair the terrain (plane), never disarm the defender (gate).
- **Taleb** (step 3): fail closed — wrong-plane or missing inputs keep the
  legacy reject; the fix can only loosen a PROVEN pathological reading.
- **Kuhn** (step 2): the anomaly IS the finding — a ratio at exactly the
  rebase factor is a distribution statement about planes, not noise.
- **Simon** (step 5): the plane map lives here precisely so the next
  amnesiac session doesn't rediscover it via a phantom PnL print.
