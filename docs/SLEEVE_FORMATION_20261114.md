# Sleeve Formation Design — 2026-11-14 (Governor ultrathink)

Doctrine: the code is conquests; sleeves are soldiers. Each soldier gets a
rank, a weapon (account), rules of engagement (routing policy), and a
casualty budget (daily risk). Promotions are earned in the journal, never
granted by hope. The Governor is the general; the harness is the army.

## 0. The autopsy that drives everything (turbo2 CSV, n=124)

| Cohort | n | WR | net | PF | payoff | avg hold |
|---|---|---|---|---|---|---|
| LONGS | 50 | 40% | **+$47.55** | **1.48** | **2.23** | 329 min |
| SHORTS | 74 | 39% | **−$15.77** | **0.91** | **1.41** | 225 min |

- 38% WR needs payoff ≥ 1.63. Longs have 2.23 (edge). Shorts 1.41 (leak).
  Cutting shorts flips turbo2 +$31.78 → +$47.55: **+50% net for free.**
- Session PnL by entry hour (UTC): golden **23–02** (+$25.9/+$20.6/+$23.7),
  toxic **15–16** (−$28.9/−$7.4). US cash open bleeds; the night pays.
- Realized peak +$60.73 → end +$31.78: giveback $58.57. The army wins the
  battle then hands back the field — runner/bank doctrine is the fix.
- Fees $14.62 of $31.78 gross (46%) — bigger notional per signal dilutes
  fees; the $1,000 floor is correct.
- **EV math (the whole plan stands on this)**: long-side expectancy
  = 0.40×2.23R − 0.60×1R = **+0.29R per trade**. At R=$100 → +$29/trade;
  at R=$300 → +$87/trade. Everything below is arithmetic on +0.29R.

## 1. The edge between the two systems (why two venues, not one)

| Edge | Propr | Lucid | Formation consequence |
|---|---|---|---|
| Instruments | crypto perps + **stock perps (xyz:)** 24/7 | CME futures only (MNQ/MGC/MBT/MET/MES) | ALL stock flow = Propr-only. Lucid gets macro strikes: BTC/ETH/gold/indices |
| Session | 24/7 | **flat by 16:45 ET (21:45 UTC), no overnight** | The golden window 23–02 UTC is PHYSICALLY impossible on Lucid → Night Watch is Propr-only by rule, not preference |
| Eval consistency | Propr rules UNVERIFIED (Gate-0) | Flex eval 40% / **Pro eval NONE** | Sniper strategy (5–7 grand trades) is LEGAL on Lucid Pro, illegal on Flex |
| DD model | verify | EOD trailing, **locks at start+$100** | Day 1 on Lucid: bank a small green to lock the leash, THEN widen risk day 2+ |
| Pass target | 150k @ 9% (verify price/rules) | lower target per Governor | Lucid carries the BIGGER account attempt; Propr carries the stock book + cheap 5k farm |
| Cost | ~$30–40 per 5k eval (cheap tickets) | Flex 25k $39 w/ coupon; Pro pricing verify | Propr 5k = expendable sniper/archer farm; Lucid = the grand-strike account |

The signal engine is one brain feeding two armies. Coherence is the draft
board: coh ≥ 7.5 → Lucid cavalry; 6.0–7.5 → Propr line troops;
5.7–6.0 + shorts → the expendable farm.

## 2. The ranking system — five unit classes

```
                 ARIA signals (coh, dir, session, asset)
                            │
                   ┌────────▼────────┐
                   │  DRAFT BOARD     │  (router: first match wins)
                   └────────┬────────┘
   ┌────────┬────────┬─────┴─────┬────────────┬─────────────┐
   ▼        ▼        ▼           ▼            ▼             ▼
 GIANT   CAVALRY   KNIGHT     ARCHER       SNIPER      (park)
 Propr   Lucid     any FUNDED Propr 5k     Propr 5k
 150k    Pro 100k  account    shorts/probe 5–7 trades
 longs   coh≥7.5   payout-    expendable   grand size
 grind   macro     farming               pass-or-die
```

- **GIANT (the siege engine)** — Propr 150k, longs-only, coh ≥ 6,
  2% risk, $1k+ min notional, golden-window preferred. Slow, unstoppable,
  the treasury when passed. One Giant at a time — never two sieges.
- **CAVALRY (decisive strikes)** — Lucid Pro (no eval consistency rule),
  coh ≥ 7.5 only, futures-mapped signals (BTC/ETH/gold/index), grand size
  inside DLL, flat by 16:40 ET. 1–2 strikes/day max. Day 1 = bank small
  green to lock the EOD trailing DD at start+$100.
- **KNIGHT (promoted, protected)** — any account that PASSES and goes
  funded. Knights trade conservative (0.5–1% risk, consistency governor
  at 35% best-day cap) because Knights pay salaries: $500 min payout,
  90% split, weekly harvest → the war chest that buys next week's armies.
- **ARCHER (harassment, expendable)** — Propr 5k eval-farm: shorts +
  5.7–6.0 probes at floor risk, $40/day budget. Their deaths are
  intelligence; an Archer that passes is promoted (book converts
  longs-only or resets — a funded 5k never keeps shorting).
- **SNIPER (one shot, one kill)** — Propr/Lucid 5k–25k evals attempting
  the pass in 5–7 trades at grand size. Pass math: need ~target/R wins in
  6 trades at 40% WR → P(pass per ticket) ≈ 15–25%. Lottery tickets —
  cheap, bought in batches, NEVER with rent money. Snipers obey the same
  exit ladder (1R/2.5R/5R); only the size is grand.

### Routing precedence (first match wins)
1. `coh≥7.5 and symbol∈{BTC,ETH,GOLD,idx}` → CAVALRY (if armed) else GIANT
2. `23≤hour_utc≤02 and coh≥6 and dir=long` → GIANT (golden window)
3. `dir=long and coh≥6` → GIANT (or Knight once funded)
4. `dir=short or 5.7≤coh<6` → ARCHER
5. SNIPER override: a designated sniper account takes coh≥6.5 longs at
   grand size until pass-or-die (5–7 trades), then retires either way
6. else → park (`formation_park` journal)

Same signal MAY fire GIANT (Propr perp) and CAVALRY (CME future) —
different instruments, different risk boxes = two trades, not netting.

## 3. Month 1 — the $350 split (between today and tomorrow)

Existing: turbo (crypto 5k) + turbo2 (tradfi 5k) = the 10k already in
accounts. They become the first two soldiers re-roled TODAY:

| Account | New role | Change |
|---|---|---|
| turbo2 (tradfi 5k) | **ARCHER→GIANT-seed** | longs-only (flip shorts OFF — the +50% net fix is config) |
| turbo (crypto 5k) | **ARCHER** | shorts + 5.7–6.0 probes (already at floor 5.7) |
| NEW Propr 150k eval | **GIANT** | ~$150–200 (VERIFY price at propr.xyz) |
| NEW Lucid Pro 50k/100k | **CAVALRY** | ~$100–190 — the bigger-account attempt (lower pass target); Gate-0 first |
| Reserve / watchdog server share | — | ~$30–40 |

Fits $350 only if Propr 150k ≈ $150 and Lucid ≈ $130: 150+130+40 = $320.
If pricing lands higher: drop Lucid to month 2 (GIANT + farm first) —
never buy the Cavalry before Gate-0 (automation permission) clears.

### The coin split (executes today/tomorrow)
- turbo2 prop.env: shorts OFF (`asset_class=tradfi` longs-only flag),
  keeps $1k floor / 2% risk / $250 day budget. Golden-window bias ON.
- turbo prop.env: stays crypto, floor 5.7, but re-roled ARCHER: shorts
  allowed, longs coh<6 allowed, daily budget $80 → $40.
- When GIANT boots: crypto longs coh≥6 migrate OFF turbo → 150k book;
  turbo keeps shorts/probes only. Symbol routing = config, no code.

## 4. The 6-month ladder (target ≈ $500k funded — the ladder binds, not the star)

Monthly deposit $300 + reinvested payouts. Weekly: new armies.

| Month | Buy | Nominal under command |
|---|---|---|
| 1 ($350) | Giant 150k + Cavalry 50–100k + re-role 2× 5k | ~$210–260k eval + 10k |
| 2 ($300) | 2× Archer/Sniper 5k weekly (~$30–40 ea) + 1× 50k | +$140–180k |
| 3 | First passes land (Giant needs ~40–75 trading days at +$87/trade EV on R=$300 — honest math: 9% of 150k = $13.5k) | first Knight 150k funded |
| 4–6 | Payouts ($500 min, 90%) buy 2–4 evals/week; sniper batches weekly | compounding |

Path to $500k funded ≈ 3× 150k passes + 2× 50–100k Lucid passes.
At +0.29R expectancy with discipline (the machine never tilts) this is
ambitious but arithmetically alive; without the shorts cut and the
golden-window specialization it was dead. Base-rate honesty: most
hand-held traders fail evals — our edge is measured PF 1.48 longs,
infinite patience, and $30 retry tickets.

## 5. Intelligent management policy (standing orders)

1. **Risk scales to each account's DD box** — never a flat $/day across
   the formation. Giant-eval $60/day; Archer $40/day; Cavalry = DLL soft
   cap; Knight 0.5–1% risk/trade.
2. **Consistency governor** on every funded account: after a day >35% of
   cycle PnL, size throttles until the ratio heals (Lucid funded 40%
   payout rule; Propr verify).
3. **Formation-level correlation cap**: BTC long on Propr + MBT long on
   Lucid = ONE risk. Shared loss_veto (program/loss_veto.json) already
   exists — extend to cross-venue exposure.
4. **Weekly war council** (digest-driven): grade every soldier
   (PF, payoff, rule compliance, giveback). Promote / re-role / execute
   (reset). Dead accounts recycled within 48h.
5. **Payout discipline**: Knights harvest weekly at $500 min; payouts go
   to the war chest, not the pocket, until $500k funded.
6. **Gate-0 before any Lucid purchase**: (a) algo/API trading permission
   under Lucid ToS via Tradovate/NinjaTrader — if banned, Cavalry becomes
   manual-advisory (harness emits the order, Governor clicks);
   (b) symbol map MBT/MET/MGC/MNQ coverage of our signals;
   (c) Pro-plan pricing + DLL option; (d) confirm EOD-trailing lock
   mechanics at start+$100.
7. **Propr verify-before-purchase**: 150k price, DD model, consistency
   rule, payout terms — search returned nothing; confirm on propr.xyz.

## 6. Ships now vs after purchase

NOW (zero cost): turbo2 longs-only flip; turbo re-role Archer budgets;
`formation_route` shadow logging on the existing two sleeves (coh, dir,
session → would-be unit) so the draft board has data before new money.

AFTER PURCHASE: Giant = clone turbo2 prop.env → 150k, longs-only.
Cavalry = new `io/lucid_client.py` (Tradovate API or manual-advisory),
EOD-trailing DD brain variant, flat-by-16:40-ET forced-exit timer.

## Sources
- [Lucid Trading plans & rules — phidiaspropfirm.com](https://phidiaspropfirm.com)
- [Lucid Flex/Pro/Direct comparison — copilink.com](https://copilink.com)
- [Lucid consistency rules — consistry.com](https://consistry.com)
- [Lucid payout rules — proea.app](https://proea.app)
- [Lucid pricing — lunefi.com](https://lunefi.com)
