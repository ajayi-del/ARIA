"""Configuration for the prop harness. All vars use the PROP_ env prefix.

Secrets live in $PROP_STATE_ROOT/prop.env (state root is OUTSIDE the repo —
issue #16 lesson). Env loader: full-line comments only, blank values stay
truly empty (dotenv inline-comment landmine), real environment wins.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_STATE_ROOT = "~/prop_harness_state"
DEFAULT_API_URL = "https://api.propr.xyz/v1"
BETA_API_URL = "https://api.beta.propr.xyz/v1"


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class PropConfig:
    enabled: bool = False
    api_key: str = ""
    api_url: str = DEFAULT_API_URL
    account_id: str = ""
    state_root: Path = field(default_factory=lambda: Path(DEFAULT_STATE_ROOT).expanduser())

    quote: str = "USDC"
    initial_balance: float = 5000.0
    internal_floor_usd: float = 150.0
    hard_zero_headroom_usd: float = 30.0

    r_base_usd: float = 5.0
    r_max_usd: float = 7.5
    budget_floor_usd: float = 5.0
    budget_divisor: float = 10.0

    max_positions: int = 5
    disable_flattens: bool = False

    # Risk profile (Governor doctrine 2026-11-08): "standard" (legacy
    # bit-for-bit) | "evaluation" (the $25-account option doctrine —
    # $40/day budget, coherence>7 big bets at 70-100% of remaining
    # budget, 1R/2.5R/5R ladder, +0.5R breakeven arm, harvested-risk
    # reloads, 30%-of-equity margin cap over the 2x/4x/10x venue ladder).
    profile: str = "standard"
    eval_daily_budget_usd: float = 40.0
    eval_trend_budget_usd: float = 50.0
    eval_coh_big: float = 7.0
    # Per-class big-bet split (Governor 2026-11-15): stocks never print
    # 7.5 (7,355 stock taps, one >=7.5 ever, p95 3.82); the only green
    # stock band is 4.x. None/0 -> eval_coh_big everywhere (legacy).
    eval_coh_big_tradfi: float | None = None
    # Optional US-session (13-20 UTC) relaxation of the tradfi bar —
    # lower wins. Demo measurement arm for the Governor's US-open gate.
    eval_coh_big_tradfi_usopen: float | None = None
    eval_coh_max: float = 8.5
    eval_risk_frac: float = 0.70
    eval_margin_cap_frac: float = 0.30
    eval_be_arm_r: float = 0.5
    eval_max_reloads: int = 1
    # Sleeve-aware coh size bump (ATS-96): slight 1.25–1.5× risk above
    # measured journal thresholds. Stocks xyz: ≥3.5 → 1.40× (n=14 edge);
    # crypto ≥7.5 → 1.25× (n=5 edge). Day budget / headroom / r_max still
    # clamp. PROP_COH_SIZE_BUMP=0 → legacy risk_for_signal bit-for-bit.
    coh_size_bump_enabled: bool = True
    coh_size_bump_tradfi_threshold: float = 3.5
    coh_size_bump_tradfi_mult: float = 1.40
    coh_size_bump_crypto_threshold: float = 7.5
    coh_size_bump_crypto_mult: float = 1.25
    # Slot reserve / probe squat (schema Phase 4, ATS-21): keep ≥1 owner
    # slot for coh>7. Default OFF → on after GO (PROP_SLOT_RESERVE=1).
    # False = legacy max_positions-only bit-for-bit.
    slot_reserve_enabled: bool = False
    slot_reserve_slots: int = 1
    # Eval tradfi/stocks probe floor (ATS-35 → ATS-103): $5 probes die when
    # round-trip taker exceeds 10% of risk. Floor was $7; ATS-103 doubles
    # the stock bet unit to $14 (2×). Cost-aware fee/risk allowance still
    # raises to eval_tradfi_cost_hard_cap. Day budget clamps. Kill
    # PROP_EVAL_TRADFI_MIN_RISK=0 → legacy $5 (pre-ATS-35).
    eval_tradfi_min_risk_enabled: bool = True
    eval_tradfi_min_risk_usd: float = 14.0
    eval_tradfi_cost_hard_cap: float = 0.30
    # ATS-103: stocks/turbo2 risk = 0.5% of live sleeve equity per trade
    # (raise-only vs probe floor). Crypto NEVER takes this path.
    # PROP_EVAL_TRADFI_ACCOUNT_RISK=0 → probe-floor-only bit-for-bit.
    eval_tradfi_account_risk_enabled: bool = True
    eval_tradfi_account_risk_frac: float = 0.005
    # Governor floor (2026-11-14): tradfi eval entries carry a minimum
    # notional — sub-floor sizes are bumped up to it (risk scales with the
    # stop distance and is committed honestly). 0 = off (legacy).
    eval_tradfi_min_notional_usd: float = 0.0
    # Ghost-150k instrument (2026-11-14, Governor ultrathink): ALL-asset
    # min-notional floor so the demo sleeve places real $10k+ fills at the
    # proven edge points — measures avg return at large capital before the
    # account purchase. Same honest-risk contract as the tradfi floor
    # (risk recomputed + recommitted + re-checked vs cap). Composes:
    # effective floor = max(tradfi floor, ghost floor). 0 = off (legacy).
    ghost_min_notional_usd: float = 0.0
    # Formation tagging (SLEEVE_FORMATION_20261114): pure-telemetry draft
    # board — every tap_signal / registry row / close carries its would-be
    # formation unit (giant/cavalry/archer/park) + edge point. Never gates,
    # never sizes. PROP_FORMATION_TAGGING=0 → pre-module bit-for-bit.
    formation_tagging_enabled: bool = True
    # Rule-1 tag flips giant→cavalry only once the Lucid purchase clears
    # Gate-0 (doc §5.6). Default False = cavalry-class signals tag giant.
    formation_cavalry_armed: bool = False
    # Draft-order priority (2026-11-14 Governor "priority decisions"):
    # each tap batch executes cavalry-class → giant → archer → park so when
    # slots/budget bind, the decisive soldiers arm first. Ordering only —
    # never refuses. PROP_FORMATION_PRIORITY=0 → arrival order (legacy).
    formation_priority_enabled: bool = True
    # Archer slot doctrine LIVE switch: archer-class signals stand down when
    # only reserve slot(s) remain. Default False = shadow-journal only
    # (formation_slot_standby census); True = live refusal. Reserve size
    # reuses slot_reserve_slots.
    formation_archer_slot_live: bool = False
    # Eval crypto probe floor (ATS-39): same wound on turbo capital_block
    # surplus after #24 coh fix — $5 probes still die fee%. Mirror ATS-35
    # for non-xyz: under evaluation. PROP_EVAL_CRYPTO_MIN_RISK=0 → legacy.
    # ATS-103: crypto keeps its own floor — do NOT inherit stock 0.5%.
    eval_crypto_min_risk_enabled: bool = True
    eval_crypto_min_risk_usd: float = 7.0
    eval_crypto_cost_hard_cap: float = 0.30
    # ATS-103: crypto fire floor — refuse non-xyz entries below min coh so
    # mainly high-quality crypto executes (and clears coh_big → good size).
    # xyz:/tradfi never takes this gate. PROP_CRYPTO_MIN_COH_FIRE=0 → legacy.
    crypto_min_coh_fire_enabled: bool = True
    crypto_min_coh_fire: float = 7.5
    # Tradfi fire floor (Governor 2026-11-15): refuse xyz: entries below
    # the stock band — the sub-4 stock churn loop was the turbo2 bleed.
    # 0.0 -> off (legacy admit any coh). Crypto never binds.
    tradfi_min_coh_fire_enabled: bool = True
    tradfi_min_coh_fire: float = 0.0
    # H1 BE/trail invariant (ATS-45): be_armed ↔ stop_locks_entry + mark-
    # implied BE when HL proves ≥arm_r and uPnL BE is dark (GOLD class).
    # Default ON; PROP_BE_TRAIL_INVARIANT=0 → legacy one-way trail stamp only.
    be_trail_invariant_enabled: bool = True
    # H2 coh stamp invariant (impl 1c): refuse place if coh ∉ honest 0–10
    # or unit-stain (0,1). PROP_COH_STAMP_INVARIANT=0 → legacy place.
    coh_stamp_invariant_enabled: bool = True
    # H4 day-budget latch (SOL stack): after fast big-bet ≤−0.7R or spent
    # fraction above threshold, force probe-only until UTC budget roll.
    # PROP_DAY_BUDGET_LATCH=0 → legacy risk_for_signal bit-for-bit.
    day_budget_latch_enabled: bool = True
    day_budget_latch_loss_r: float = -0.7
    day_budget_latch_t_fast_s: float = 300.0
    day_budget_latch_spent_threshold: float = 0.70
    # Prop MFE sharpen — BTC peak→floor ratio (not hard TP / not ROE%).
    # peak_usd≥30 → floor max(20, frac×peak); banked uses denser frac.
    # PROP_MFE_SHARPEN=0 → H1 trail/BE path only. Min $20 = not dust.
    mfe_sharpen_enabled: bool = True
    mfe_sharpen_peak_usd: float = 30.0
    mfe_sharpen_floor_frac: float = 0.5
    mfe_sharpen_min_floor_usd: float = 20.0
    mfe_sharpen_banked_floor_frac: float = 0.75
    # Big-bet denser TP ladder (institutional scale-out). coh>7 or
    # risk≥min → EVAL_BIG_BET_LADDER; probes keep EVAL_LADDER.
    # PROP_BIG_BET_LADDER=0 → EVAL_LADDER for everyone.
    big_bet_ladder_enabled: bool = True
    big_bet_ladder_min_risk_usd: float = 20.0
    # Crypto-only slight TP tighten (0.8/2/4 probe, big mid/late cut).
    # xyz: tradfi never takes crypto ladders. PROP_CRYPTO_TP_TIGHTEN=0
    # → EVAL_LADDER / EVAL_BIG_BET_LADDER bit-for-bit on crypto too.
    crypto_tp_tighten_enabled: bool = True
    # Protect realized partial banks (Dayo 75% BTC @ ~$20–25): after
    # realized_booked≥$20, remainder stop → entry so the bank cannot
    # return. $15 was too soft. PROP_BANK_PROTECT=0 → legacy sync only.
    bank_protect_enabled: bool = True
    bank_protect_min_usd: float = 20.0
    # Pyramid/scalper runner harvest after bank (re-touch = harvest, not
    # re-risk). PROP_RUNNER_BANK_FLOOR=0 → bank_protect + trail only.
    runner_bank_floor_enabled: bool = True
    # Live-USD peak lock (Governor 2026-11-15 giveback repair): the
    # birth-R peak math is quantity-blind — tap_reload-grown runners
    # (LTC 10.9→152.17) showed peak_r 0.688 while the real peak was
    # ~$218; only breakeven armed, $72 of +$81 given back. This locks
    # rung-fractions of the VENUE uPnL peak instead. Default OFF =
    # pre-module bit-for-bit (no ratchet state, no tighten).
    live_peak_lock_enabled: bool = False
    live_peak_lock_min_floor_usd: float = 20.0
    # Loss-as-signal veto (Build C): the probe account publishes
    # loss_veto.json; every consuming account refuses vetoed cohorts.
    # Fail-OPEN on stale/missing artifact — a dead probe never freezes
    # the fleet.
    loss_veto_enabled: bool = True
    loss_veto_path: str = ""   # default: state_root/program/loss_veto.json
    loss_veto_min_n: int = 5
    # partial TP fills shrink the venue position but not the resting stop —
    # resize protection on reconcile (False = legacy bit-for-bit).
    protection_resize_enabled: bool = True
    # Rotation policy (Governor paste 2026-11-08): one representative per
    # (theme family, direction); round-trip taker cost must stay under
    # cost_risk_max_frac of planned risk (rejects tight-stop oversizing).
    family_cap_enabled: bool = True
    cost_risk_max_frac: float = 0.10
    # Crypto co-move / single-β gate (Dayo 2026-11-10): refuse opposite-
    # signed crypto adds vs the live book's dominant direction (net signed
    # risk, majority tiebreak). Alts co-move with majors — BTC/ETH/SOL-only
    # is insufficient. Default ON; PROP_CRYPTO_COMOVE_GATE=0 = legacy.
    crypto_comove_enabled: bool = True
    crypto_comove_min_legs: int = 1
    crypto_comove_min_dom_risk_usd: float = 0.0
    # Co-move underwater / MFE-decay flip (overflow audit §4). Shadow
    # journals crypto_comove_would_flip; live OFF by default. Prefer
    # flat-then-reverse over opposite hedges. PROP_CRYPTO_COMOVE_FLIP=0
    # disables shadow+live; PROP_CRYPTO_COMOVE_FLIP_LIVE=1 actuates.
    crypto_comove_flip_enabled: bool = True
    crypto_comove_flip_live: bool = False
    crypto_comove_flip_prefer_flat: bool = True
    crypto_comove_flip_underwater_s: float = 900.0
    crypto_comove_flip_giveback: float = 0.60
    # Post-win re-spend latch (nine-trade P0): after a large crypto win,
    # refuse β-proximate re-entries that re-spend the gain (SOL/NEAR class).
    # PROP_POST_WIN_RESPEND=0 → legacy.
    post_win_respend_enabled: bool = True
    post_win_respend_min_win_usd: float = 25.0
    post_win_respend_min_win_r: float = 0.70
    post_win_respend_min_risk_for_r: float = 20.0
    post_win_respend_ttl_s: float = 5400.0
    post_win_respend_max_usd: float = 15.0
    # Post-win same-direction cooloff (2026-11-15 cluster killer): after
    # ANY qualifying win (>= max($1.5, 0.20R)), same-(class, side) entries
    # are BLOCKED for the TTL — no respend budget. Direction scope, not
    # family: the fatal cluster crossed families (ETH/ONDO/ARB). Default
    # OFF (control sleeves bit-for-bit); arm per sleeve via prop.env.
    post_win_cooloff_enabled: bool = False
    post_win_cooloff_min_win_usd: float = 1.5
    post_win_cooloff_min_win_r: float = 0.20
    post_win_cooloff_ttl_s: float = 1800.0
    # M4' post-loss cooloff (2026-11-17, Governor order): ANY losing close
    # (pnl < 0) arms a (asset, side) latch — re-entering the same side of
    # the same asset inside the TTL is refused. xyz: prefix stripped so
    # GOLD/xyz:GOLD share one latch. Default ON;
    # PROP_POST_LOSS_COOLOFF=0 → legacy admit-all bit-for-bit.
    post_loss_cooloff_enabled: bool = True
    post_loss_cooloff_ttl_s: float = 1800.0
    # M8 rho-box clamp (2026-11-17, quant-confirmed on turbo2): eval-profile
    # risk is additionally clamped to rho × remaining headroom above the
    # kill floor so no single entry can strand the account below hard zero.
    # Below min-viable → DENY. rho <= 0 or PROP_RHO_BOX_ENABLED=0 → legacy
    # clamp chain bit-for-bit.
    rho_box_enabled: bool = True
    rho_box: float = 0.12
    rho_box_min_viable_usd: float = 7.0
    # Re-entry close-epoch latch (2026-11-17): after a STOP-CLASS/external
    # close, same (symbol, side) re-entry is refused for the TTL. Registry
    # closed_ts is the epoch; rows without closed_ts are legacy-valid.
    # PROP_REENTRY_LATCH=0 → legacy re-entry flow.
    reentry_latch_enabled: bool = True
    reentry_latch_ttl_s: float = 1800.0
    # Signal-tap hole repairs (2026-11-17): (a) seen[id] marked only AFTER
    # the execution attempt resolves (not burned on failure-to-execute);
    # (b) TTL-aware prune replaces the flat cap evicting in-TTL ids;
    # (c) boot-replay cursor rewind defaults OFF. PROP_TAP_SEEN_CONFIRM=0 /
    # PROP_TAP_TTL_PRUNE=0 / PROP_ARM_BOOT_REPLAY=1 → legacy behavior.
    tap_seen_confirm_enabled: bool = True
    tap_ttl_prune_enabled: bool = True
    arm_boot_replay: bool = False
    # Per-symbol fire floor (2026-11-17, quant mandate XAUT 4.0): K:V map,
    # e.g. PROP_SYMBOL_MIN_COH_FIRE="XAUT:4.0,GOLD:4.0". Precedence:
    # symbol-specific > class floor (tradfi/crypto) > 0. ""/"0" disables.
    symbol_min_coh_fire: str = "XAUT:4.0,GOLD:4.0"
    # Cross-sleeve conflict TELEMETRY (2026-11-17): shared book_view.json —
    # each sleeve publishes its open (asset, side) exposure per reconcile
    # tick; opposite-side holdings across sleeves log
    # company_conflict_open once per pair per 300s. NEVER blocks.
    # PROP_BOOK_VIEW=0 → no writes, no reads (legacy).
    book_view_enabled: bool = True
    book_view_path: str = ""   # default: state_root/program/book_view.json
    # Maker-first entries (2026-11-15 fee attack): two-phase — limit GTC
    # entry at entry_ref, fill-verify, then protection legs. Taker pays
    # 0.045%, resting maker 0.015% (tier proven on the Governor's tape).
    # Default OFF; PROP_MAKER_ENTRY=1 arms per sleeve. Fallback ON = an
    # unfilled limit retries once as the legacy full market bracket.
    maker_entry_enabled: bool = False
    maker_entry_timeout_s: float = 8.0
    maker_entry_taker_fallback: bool = True
    # Cost-to-expected-move gate (nine-trade P0/P1). Θ=0.25 shadow-from-
    # birth: live OFF so ATS-39 hard-cap 0.30 still clears ~0.35% ATR
    # probes; journals would-block for census before actuation.
    # PROP_COST_MOVE_GATE=0 → legacy; PROP_COST_MOVE_LIVE=1 → refuse at Θ.
    cost_move_gate_enabled: bool = True
    cost_move_gate_live: bool = False
    cost_move_theta: float = 0.25
    # Early stop-tighten telemetry (NEAR +2s class). Observability only.
    # PROP_STOP_RESIZE_TELEMETRY=0 → legacy protection_resized payload.
    stop_resize_telemetry_enabled: bool = True
    stop_resize_early_s: float = 30.0
    # Giveback P0 — act reliability under Propr 429 storms. When ON,
    # resize_protection (and future basket close) exp-backoffs per key
    # after rate limits so decided floors can settle. PROP_ACT_BACKOFF=0
    # → legacy retry-every-reconcile-tick (the storm).
    act_backoff_enabled: bool = True
    act_backoff_initial_s: float = 30.0
    act_backoff_max_s: float = 300.0
    act_backoff_latch_after: int = 3
    # Stop arbiter (2026-11-17 resize-storm kill): per-position per-tick
    # arbiter — every stage votes its floor, the tightest wins, AT MOST one
    # venue resize per position per tick fires, and only when the winning
    # floor moved past the dust epsilon vs the last issued floor. Size-sync
    # resizes (qty change) bypass dust. PROP_STOP_ARBITER=0 → legacy
    # uncoalesced per-stage resizes bit-for-bit.
    stop_arbiter_enabled: bool = True
    stop_arbiter_dust_frac: float = 0.0005
    # Dust epsilon per-R (audit 2026-11-17): the frac above is PRICE-
    # relative — a $20k-stop asset at 0.0005 swallows ~0.5R of the early
    # tighten band. When dust_r > 0 it REPLACES frac: epsilon =
    # dust_r × |entry − birth stop| (0.02 = 2% of 1R). 0.0 = legacy frac.
    stop_arbiter_dust_r: float = 0.02
    # Weak-stop BIRTH floor (2026-11-19): widen-ONLY at place_bracket.
    # Live hold<90s scrapes median birth_stop_pct≈0.35% (=3.9×RT) — fee-
    # tick death band. Crypto 0.75% (=8.3×RT) / tradfi 0.50% (=5.6×RT);
    # when ATR readable take max(pct×mark, atr_mult×ATR). Never tightens.
    # Fail-OPEN keep signal stop if inputs dark. PROP_STOP_FLOOR=0 → legacy.
    stop_floor_enabled: bool = True
    stop_floor_pct_crypto: float = 0.75
    stop_floor_pct_tradfi: float = 0.50
    stop_floor_atr_mult: float = 1.0
    # Weak-stop MANAGEMENT gate (2026-11-19): BTC trail/BE at 7s +
    # runner_harvest at 22s = synthetic tight stop. Raise only after
    # min_age_s (300s = bottom of 300–900 crypto band) AND stage R★
    # (0.5R BE/bank/trail; 1.0R harvest/live_peak/mfe_sharpen).
    # Fail-OPEN on missing opened_ts/mfe. PROP_EARLY_TIGHTEN_GATE=0 → legacy.
    early_tighten_gate_enabled: bool = True
    early_tighten_min_age_s: float = 300.0
    early_tighten_be_min_mfe_r: float = 0.5
    early_tighten_harvest_min_mfe_r: float = 1.0
    # Algedonic latch (2026-11-17): equity_poll writes algedonic.json on
    # LOCKED/BREACHED; without this read-back the drawdown recomputes fresh
    # after a restart and a locked sleeve self-heals past the Governor's
    # floor. Latch WINS the recompute; only the Governor clears (delete the
    # latch file). Fail-open on missing/corrupt. PROP_ALGEDONIC_LATCH=0 →
    # legacy recompute-only bit-for-bit.
    algedonic_latch_enabled: bool = True
    # Signed realized leg (2026-11-17): release() carried realized=0.0
    # structurally — the UTC-day budget never saw signed P&L. ON passes the
    # real signed close pnl (net of intra-day shrink releases).
    # PROP_BUDGET_REALIZED=0 → legacy constant-0.0 bit-for-bit.
    budget_realized_enabled: bool = True
    # MAE tracker (2026-11-17): symmetric lower-only trough beside the MFE
    # peak so the close journal carries mfe_peak_r + mae_trough_r (M3
    # shield-q measurement). PROP_MAE_TRACKER=0 → MFE-only legacy rows.
    mae_tracker_enabled: bool = True
    # Loss-veto grain (2026-11-17): cohort key moves (family, side) →
    # (asset, side) so one toxic name vetoes itself, not its whole family.
    # Versioned schema: publisher stamps schema=2, consumer fails closed to
    # EMPTY on old/absent schema. DEFAULT OFF (legacy family grain) — the
    # Governor arms per sleeve via PROP_LOSS_VETO_ASSET_GRAIN=1; OFF is the
    # legacy (family, side) flow bit-for-bit.
    loss_veto_asset_grain_enabled: bool = False
    # Edge boost (2026-11-17): the profit mirror of loss_veto. The SAME
    # artifact (loss_veto.json — cohort stats carry positive rows too) arms
    # a bounded size multiplier [1.0, cap] on cohorts with earned positive
    # shrunk expectancy. Reads whatever artifact loss_veto reads, at
    # whatever grain is armed; fail-OPEN to 1.0 on stale/missing/thin.
    # PROP_EDGE_BOOST=0 → legacy sizing bit-for-bit.
    edge_boost_enabled: bool = True
    edge_boost_min_n: int = 5
    edge_boost_min_expectancy: float = 0.5
    edge_boost_max_mult: float = 1.5
    # C12 amendments (2026-11-17): R-based gate (size-invariant — a 5-for-5
    # lucky streak at $5 risk can never hit the cap) with an evidence ramp
    # (full mult only at n ≥ full_n) and a veto→boost quarantine (a cohort
    # the artifact vetoed within quarantine_s may not be boosted).
    # PROP_EDGE_BOOST_R_BASED=0 → legacy $-threshold bit-for-bit.
    edge_boost_r_based_enabled: bool = True
    edge_boost_min_r: float = 0.10
    edge_boost_full_n: int = 20
    edge_boost_veto_quarantine_s: float = 21600.0
    # Boost/veto symmetry (audit): boost arms only when the veto consumer
    # is armed too — a seat that does not respect the loss side must not
    # size up on the profit side. PROP_EDGE_BOOST_REQUIRES_VETO=0 → legacy.
    edge_boost_requires_veto: bool = True
    # C10 account partition: one account = one evidence base. The publisher
    # stamps account_id on close rows and the cohort builder filters to it
    # (rows without account_id are excluded under an active filter).
    # PROP_COHORT_ACCOUNT_FILTER=0 → legacy unpartitioned build.
    cohort_account_filter_enabled: bool = True
    # C10 warm-start epoch: closes before this epoch never enter the
    # artifact (account switch / doctrine flip rebase without deleting
    # history). 0.0 = legacy.
    cohort_epoch_ts: float = 0.0
    # C4 venue-lag restamp: a close stamped within this window accepts a
    # fresher realized read (first-write-wins froze stale stamps).
    # 0.0 = legacy first-write-wins.
    close_restamp_window_s: float = 600.0
    # Rebound tracker (2026-11-17): underwater→breakeven cycle count per
    # bracket (the "coins go under water and come back several times"
    # signal). Close journal carries rebound_count; a rebound-qualified
    # bracket currently underwater may not be sharpened past entry into
    # the dip (decline-only, never loosens). Kill PROP_REBOUND_TRACKER=0 /
    # PROP_REBOUND_SHARPEN_GUARD=0 → legacy bit-for-bit.
    rebound_tracker_enabled: bool = True
    rebound_sharpen_guard_enabled: bool = True
    rebound_arm_r: float = 0.5
    rebound_recover_r: float = 0.0
    rebound_min_count: int = 2
    # Suppression-event throttle (audit 2026-11-17): the sharpen-guard
    # decline fires every tick while a qualified bracket is underwater —
    # one event per bracket per TTL is enough for the cohort planes.
    rebound_suppress_log_ttl_s: float = 300.0
    # measured 2026-11-07 on the demo fills: 0.045%/side taker. TP prices are
    # solved net of the round trip; 0.0 = legacy pure-geometry ladder.
    taker_fee_rate: float = 0.00045

    # Overflow topology (Governor 2026-11-08): an overflow-only account
    # takes a fresh signal ONLY when every peer covering that signal's
    # class is at max positions (peer = another account's published
    # prop_positions.json). Fail-closed on stale/missing peer snapshots.
    overflow_only: bool = False
    overflow_peers: str = ""   # comma-separated prop_positions.json paths
    overflow_stale_s: float = 90.0

    # KingdomView Phase 1 (harness-wiring Law 2): shared kingdom memory.
    # Writer merges ArmyStates + heartbeats + loss_veto + thesis slots →
    # ~/kingdom/prop_kingdom_view.json. Overflow reads view OR legacy
    # peers. PROP_KINGDOM_VIEW=0 → peer-file envy bit-for-bit.
    kingdom_view_enabled: bool = True
    kingdom_view_path: str = ""          # default ~/kingdom/prop_kingdom_view.json
    kingdom_view_peers: str = ""         # state roots or prop_positions paths
    kingdom_view_write: bool = True      # fail-open publish on heartbeat

    # HL reference mark plane (spec §3, GO 2026-11-08): keyless public-info
    # poller, REFERENCE-ONLY (triggers/telemetry, never risk math), staleness
    # abstain, fail-open = pre-plane behavior bit-for-bit.
    hl_marks_enabled: bool = True
    hl_marks_poll_s: float = 10.0
    hl_marks_stale_s: float = 60.0

    equity_poll_s: float = 30.0
    reconcile_s: float = 15.0
    budget_roll_s: float = 60.0
    heartbeat_s: float = 60.0
    signal_tap_s: float = 2.0

    outbox_enabled: bool = False
    outbox_path: str = ""
    outbox_transport: str = "local"  # "local" file | "ssh" pull from the ARIA VM
    outbox_host: str = "aria-prod-v2"
    outbox_zone: str = "europe-west3-c"
    # §7 S3: actionable aria_capital_block rows. PROP_CAPITAL_SURPLUS=0 →
    # standby (census only); risk_block always dropped regardless.
    capital_surplus_enabled: bool = True
    signal_ttl_s: float = 300.0
    # Graph 7 / Monday RTH: xyz: rows keep a longer TTL so turbo2 equity
    # alpha does not die as tap_expired under the shared 300s cursor.
    # <=0 = legacy (crypto TTL for all classes).
    tradfi_signal_ttl_s: float = 3600.0
    # Hot-cursor at boot for the tradfi owner: rewind offset→0 once so
    # in-TTL equity rows are rescanned. Dedup + TTL still bind. False =
    # legacy cold cursor (EOF stays EOF).
    tap_boot_replay: bool = True
    # sleeve split (Governor 2026-11-07): "crypto" | "tradfi" | "all".
    # xyz:-mapped assets are tradfi; "all" = pre-split bit-for-bit.
    asset_class: str = "all"
    # family partition (Governor 2026-11-08 two-turbo split): comma list of
    # theme families this account trades (prop_harness/families.py). Each
    # family lives on exactly ONE turbo account so the per-account family
    # cap doubles effective same-direction capacity. Empty = no partition.
    families: str = ""
    # Turbo crypto toxic-cohort denylist (ATS-102). Tap-only when
    # asset_class=="crypto". PROP_CRYPTO_DENY=0 → legacy pass-through.
    # PROP_CRYPTO_DENY_SYMBOLS empty → DEFAULT_CRYPTO_DENY_SYMBOLS.
    crypto_deny_enabled: bool = True
    crypto_deny_symbols: str = ""
    # L8/L10 latch persistence (CEO audit S4, 2026-11-17): persist the
    # ActBackoff 429 latch into the sleeve state dir; PROP_ACT_BACKOFF_PERSIST=0
    # → legacy memory-only bit-for-bit.
    act_backoff_persist_enabled: bool = True
    # Reader-side TTL prune for post_win_respend / post_win_cooloff latches:
    # expired entries are dropped on read so stale respend_committed values
    # never cap fresh budgets. PROP_STATE_TTL_PRUNE=0 → legacy raw reads.
    state_ttl_prune_enabled: bool = True
    # L7 clock-skew freshness guard (signal_tap): a signal is stale only when
    # BOTH the wall clock and the emit-ts envelope agree. PROP_TAP_SKEW_GUARD=0
    # → legacy wall-clock-only.
    tap_skew_guard_enabled: bool = True
    # HL first-party observer plane (task #37, 2026-11-17): keyless public
    # /info oracle — allMids marks + clearinghouseState per-seat equity/uPnL
    # + metaAndAssetCtxs funding/OI. OBSERVER-CLASS: cross-checks the Propr
    # API mirrors and warms the HL adapter; never sizes, never gates, never
    # orders. Default ON but INERT until the harness wires the tick cadence.
    # PROP_HL_PLANE=0 → dead plane (nothing reads it). Empty wallets =
    # marks-only mode. PROP_HL_PLANE_WALLETS is a csv of seat addresses.
    hl_plane_enabled: bool = True
    hl_plane_wallets: str = ""
    hl_plane_poll_s: float = 30.0
    hl_plane_diverge_bps: float = 50.0
    # Per-poll jump guard (A9, 2026-11-17): a mark jumping > this fraction
    # vs the symbol's last accepted print is quarantined (last good print
    # survives) so a venue cross-scale print can never inflate a raise-only
    # mfe_peak_r ratchet. 0.0 disables (legacy). Forward-wired: consumed at
    # the HLDataPlane construction site when the tick cadence lands.
    hl_plane_jump_frac: float = 0.15

    @property
    def floor_line(self) -> float:
        return self.initial_balance - self.internal_floor_usd

    @property
    def disabled_file(self) -> Path:
        return self.state_root / "DISABLED"

    @property
    def loss_veto_artifact(self) -> Path:
        """READ path: the shared artifact when PROP_LOSS_VETO_PATH is set
        (consumer seat), else this seat's own."""
        if self.loss_veto_path:
            return Path(self.loss_veto_path).expanduser()
        return self.state_root / "program" / "loss_veto.json"

    @property
    def loss_veto_write_artifact(self) -> Path:
        """C9 (2026-11-17): the WRITE path is ALWAYS this seat's own state
        root. A consumer seat with PROP_LOSS_VETO_PATH set used to write
        the SHARED path too — polluting the probe's evidence base with the
        consumer's closes. Publishers that should feed the shared artifact
        get it via prop.env pointing their own root (single writer per
        surface, A2)."""
        return self.state_root / "program" / "loss_veto.json"

    @property
    def book_view_artifact(self) -> Path:
        if self.book_view_path:
            return Path(self.book_view_path).expanduser()
        return self.state_root / "program" / "book_view.json"

    @property
    def kingdom_view_artifact(self) -> Path:
        if self.kingdom_view_path:
            return Path(self.kingdom_view_path).expanduser()
        from .brains.kingdom_view import DEFAULT_VIEW_PATH
        return Path(DEFAULT_VIEW_PATH).expanduser()

    @property
    def journal_path(self) -> Path:
        return self.state_root / "journal" / "events.jsonl"

    @classmethod
    def from_env(cls) -> "PropConfig":
        state_root = Path(os.environ.get("PROP_STATE_ROOT", DEFAULT_STATE_ROOT)).expanduser()
        load_env_file(state_root / "prop.env")
        # state_root may itself be set by the env file just loaded
        state_root = Path(os.environ.get("PROP_STATE_ROOT", str(state_root))).expanduser()

        def f(name: str, default: float) -> float:
            raw = os.environ.get(f"PROP_{name}", "")
            return float(raw) if raw else default

        def i(name: str, default: int) -> int:
            raw = os.environ.get(f"PROP_{name}", "")
            return int(raw) if raw else default

        def b(name: str, default: bool) -> bool:
            raw = os.environ.get(f"PROP_{name}", "")
            return raw.lower() in ("1", "true", "yes") if raw else default

        return cls(
            enabled=b("ENABLED", False),
            api_key=os.environ.get("PROP_API_KEY") or os.environ.get("PROPR_API_KEY", ""),
            api_url=os.environ.get("PROP_API_URL", DEFAULT_API_URL),
            account_id=os.environ.get("PROP_ACCOUNT_ID", ""),
            state_root=state_root,
            initial_balance=f("INITIAL_BALANCE", 5000.0),
            internal_floor_usd=f("INTERNAL_FLOOR_USD", 150.0),
            hard_zero_headroom_usd=f("HARD_ZERO_HEADROOM_USD", 30.0),
            r_base_usd=f("R_BASE_USD", 5.0),
            r_max_usd=f("R_MAX_USD", 7.5),
            budget_floor_usd=f("BUDGET_FLOOR_USD", 5.0),
            budget_divisor=f("BUDGET_DIVISOR", 10.0),
            max_positions=i("MAX_POSITIONS", 3),
            disable_flattens=b("DISABLE_FLATTENS", False),
            profile=os.environ.get("PROP_PROFILE", "standard"),
            eval_daily_budget_usd=f("EVAL_DAILY_BUDGET_USD", 40.0),
            eval_trend_budget_usd=f("EVAL_TREND_BUDGET_USD", 50.0),
            eval_coh_big=f("EVAL_COH_BIG", 7.0),
            eval_coh_big_tradfi=(f("EVAL_COH_BIG_TRADFI", 0.0) or None),
            eval_coh_big_tradfi_usopen=(
                f("EVAL_COH_BIG_TRADFI_USOPEN", 0.0) or None),
            eval_coh_max=f("EVAL_COH_MAX", 8.5),
            eval_risk_frac=f("EVAL_RISK_FRAC", 0.70),
            eval_margin_cap_frac=f("EVAL_MARGIN_CAP_FRAC", 0.30),
            eval_be_arm_r=f("EVAL_BE_ARM_R", 0.5),
            eval_max_reloads=i("EVAL_MAX_RELOADS", 1),
            coh_size_bump_enabled=b("COH_SIZE_BUMP", True),
            coh_size_bump_tradfi_threshold=f(
                "COH_SIZE_BUMP_TRADFI_THR", 3.5),
            coh_size_bump_tradfi_mult=f(
                "COH_SIZE_BUMP_TRADFI_MULT", 1.40),
            coh_size_bump_crypto_threshold=f(
                "COH_SIZE_BUMP_CRYPTO_THR", 7.5),
            coh_size_bump_crypto_mult=f(
                "COH_SIZE_BUMP_CRYPTO_MULT", 1.25),
            slot_reserve_enabled=b("SLOT_RESERVE", False),
            slot_reserve_slots=i("SLOT_RESERVE_SLOTS", 1),
            eval_tradfi_min_risk_enabled=b("EVAL_TRADFI_MIN_RISK", True),
            eval_tradfi_min_risk_usd=f("EVAL_TRADFI_MIN_RISK_USD", 14.0),
            eval_tradfi_cost_hard_cap=f("EVAL_TRADFI_COST_HARD_CAP", 0.30),
            eval_tradfi_account_risk_enabled=b(
                "EVAL_TRADFI_ACCOUNT_RISK", True),
            eval_tradfi_account_risk_frac=f(
                "EVAL_TRADFI_ACCOUNT_RISK_FRAC", 0.005),
            eval_tradfi_min_notional_usd=f(
                "EVAL_TRADFI_MIN_NOTIONAL_USD", 0.0),
            ghost_min_notional_usd=f("GHOST_MIN_NOTIONAL_USD", 0.0),
            formation_tagging_enabled=b("FORMATION_TAGGING", True),
            formation_cavalry_armed=b("FORMATION_CAVALRY_ARMED", False),
            formation_priority_enabled=b("FORMATION_PRIORITY", True),
            formation_archer_slot_live=b("FORMATION_ARCHER_SLOT_LIVE", False),
            eval_crypto_min_risk_enabled=b("EVAL_CRYPTO_MIN_RISK", True),
            eval_crypto_min_risk_usd=f("EVAL_CRYPTO_MIN_RISK_USD", 7.0),
            eval_crypto_cost_hard_cap=f("EVAL_CRYPTO_COST_HARD_CAP", 0.30),
            # Kill: PROP_CRYPTO_MIN_COH_FIRE=0 (legacy admit any coh).
            crypto_min_coh_fire_enabled=(
                False if os.environ.get("PROP_CRYPTO_MIN_COH_FIRE", "")
                .strip() in ("0", "0.0") else True
            ) if "PROP_CRYPTO_MIN_COH_FIRE" in os.environ else True,
            crypto_min_coh_fire=f("CRYPTO_MIN_COH_FIRE", 7.5),
            # Kill: PROP_TRADFI_MIN_COH_FIRE=0 or unset (legacy).
            tradfi_min_coh_fire_enabled=(
                False if os.environ.get("PROP_TRADFI_MIN_COH_FIRE", "")
                .strip() in ("0", "0.0") else True
            ) if "PROP_TRADFI_MIN_COH_FIRE" in os.environ else True,
            tradfi_min_coh_fire=f("TRADFI_MIN_COH_FIRE", 0.0),
            be_trail_invariant_enabled=b("BE_TRAIL_INVARIANT", True),
            coh_stamp_invariant_enabled=b("COH_STAMP_INVARIANT", True),
            day_budget_latch_enabled=b("DAY_BUDGET_LATCH", True),
            day_budget_latch_loss_r=f("DAY_BUDGET_LATCH_LOSS_R", -0.7),
            day_budget_latch_t_fast_s=f("DAY_BUDGET_LATCH_T_FAST_S", 300.0),
            day_budget_latch_spent_threshold=f(
                "DAY_BUDGET_LATCH_SPENT", 0.70),
            mfe_sharpen_enabled=b("MFE_SHARPEN", True),
            mfe_sharpen_peak_usd=f("MFE_SHARPEN_PEAK_USD", 30.0),
            mfe_sharpen_floor_frac=f("MFE_SHARPEN_FLOOR_FRAC", 0.5),
            mfe_sharpen_min_floor_usd=f("MFE_SHARPEN_MIN_FLOOR_USD", 20.0),
            mfe_sharpen_banked_floor_frac=f(
                "MFE_SHARPEN_BANKED_FLOOR_FRAC", 0.75),
            big_bet_ladder_enabled=b("BIG_BET_LADDER", True),
            big_bet_ladder_min_risk_usd=f("BIG_BET_LADDER_MIN_RISK", 20.0),
            crypto_tp_tighten_enabled=b("CRYPTO_TP_TIGHTEN", True),
            bank_protect_enabled=b("BANK_PROTECT", True),
            bank_protect_min_usd=f("BANK_PROTECT_MIN_USD", 20.0),
            runner_bank_floor_enabled=b("RUNNER_BANK_FLOOR", True),
            live_peak_lock_enabled=b("LIVE_PEAK_LOCK", False),
            live_peak_lock_min_floor_usd=f(
                "LIVE_PEAK_LOCK_MIN_FLOOR_USD", 20.0),
            loss_veto_enabled=b("LOSS_VETO_ENABLED", True),
            loss_veto_path=os.environ.get("PROP_LOSS_VETO_PATH", ""),
            loss_veto_min_n=i("LOSS_VETO_MIN_N", 5),
            protection_resize_enabled=b("PROTECTION_RESIZE_ENABLED", True),
            family_cap_enabled=b("FAMILY_CAP_ENABLED", True),
            cost_risk_max_frac=f("COST_RISK_MAX_FRAC", 0.10),
            crypto_comove_enabled=b("CRYPTO_COMOVE_GATE", True),
            crypto_comove_min_legs=i("CRYPTO_COMOVE_MIN_LEGS", 1),
            crypto_comove_min_dom_risk_usd=f(
                "CRYPTO_COMOVE_MIN_DOM_RISK", 0.0),
            crypto_comove_flip_enabled=b("CRYPTO_COMOVE_FLIP", True),
            crypto_comove_flip_live=b("CRYPTO_COMOVE_FLIP_LIVE", False),
            crypto_comove_flip_prefer_flat=b(
                "CRYPTO_COMOVE_FLIP_PREFER_FLAT", True),
            crypto_comove_flip_underwater_s=f(
                "CRYPTO_COMOVE_FLIP_UNDERWATER_S", 900.0),
            crypto_comove_flip_giveback=f(
                "CRYPTO_COMOVE_FLIP_GIVEBACK", 0.60),
            post_win_respend_enabled=b("POST_WIN_RESPEND", True),
            post_win_respend_min_win_usd=f("POST_WIN_RESPEND_MIN_USD", 25.0),
            post_win_respend_min_win_r=f("POST_WIN_RESPEND_MIN_R", 0.70),
            post_win_respend_min_risk_for_r=f(
                "POST_WIN_RESPEND_MIN_RISK", 20.0),
            post_win_respend_ttl_s=f("POST_WIN_RESPEND_TTL_S", 5400.0),
            post_win_respend_max_usd=f("POST_WIN_RESPEND_MAX_USD", 15.0),
            post_win_cooloff_enabled=b("POST_WIN_COOLOFF", False),
            post_win_cooloff_min_win_usd=f("POST_WIN_COOLOFF_MIN_USD", 1.5),
            post_win_cooloff_min_win_r=f("POST_WIN_COOLOFF_MIN_R", 0.20),
            post_win_cooloff_ttl_s=f("POST_WIN_COOLOFF_TTL_S", 1800.0),
            post_loss_cooloff_enabled=b("POST_LOSS_COOLOFF", True),
            post_loss_cooloff_ttl_s=f("POST_LOSS_COOLOFF_TTL_S", 1800.0),
            rho_box_enabled=b("RHO_BOX_ENABLED", True),
            rho_box=f("RHO_BOX", 0.12),
            rho_box_min_viable_usd=f("RHO_BOX_MIN_VIABLE_USD", 7.0),
            reentry_latch_enabled=b("REENTRY_LATCH", True),
            reentry_latch_ttl_s=f("REENTRY_LATCH_TTL_S", 1800.0),
            tap_seen_confirm_enabled=b("TAP_SEEN_CONFIRM", True),
            tap_ttl_prune_enabled=b("TAP_TTL_PRUNE", True),
            arm_boot_replay=b("ARM_BOOT_REPLAY", False),
            symbol_min_coh_fire=os.environ.get(
                "PROP_SYMBOL_MIN_COH_FIRE", "XAUT:4.0,GOLD:4.0"),
            book_view_enabled=b("BOOK_VIEW", True),
            book_view_path=os.environ.get("PROP_BOOK_VIEW_PATH", ""),
            maker_entry_enabled=b("MAKER_ENTRY", False),
            maker_entry_timeout_s=f("MAKER_ENTRY_TIMEOUT_S", 8.0),
            maker_entry_taker_fallback=b("MAKER_ENTRY_TAKER_FALLBACK", True),
            cost_move_gate_enabled=b("COST_MOVE_GATE", True),
            cost_move_gate_live=b("COST_MOVE_LIVE", False),
            cost_move_theta=f("COST_MOVE_THETA", 0.25),
            stop_resize_telemetry_enabled=b("STOP_RESIZE_TELEMETRY", True),
            stop_resize_early_s=f("STOP_RESIZE_EARLY_S", 30.0),
            act_backoff_enabled=b("ACT_BACKOFF", True),
            act_backoff_initial_s=f("ACT_BACKOFF_INITIAL_S", 30.0),
            act_backoff_max_s=f("ACT_BACKOFF_MAX_S", 300.0),
            act_backoff_latch_after=i("ACT_BACKOFF_LATCH_AFTER", 3),
            stop_arbiter_enabled=b("STOP_ARBITER", True),
            stop_arbiter_dust_frac=f("STOP_ARBITER_DUST_FRAC", 0.0005),
            stop_arbiter_dust_r=f("STOP_ARBITER_DUST_R", 0.02),
            stop_floor_enabled=b("STOP_FLOOR", True),
            stop_floor_pct_crypto=f("STOP_FLOOR_PCT_CRYPTO", 0.75),
            stop_floor_pct_tradfi=f("STOP_FLOOR_PCT_TRADFI", 0.50),
            stop_floor_atr_mult=f("STOP_FLOOR_ATR_MULT", 1.0),
            early_tighten_gate_enabled=b("EARLY_TIGHTEN_GATE", True),
            early_tighten_min_age_s=f("EARLY_TIGHTEN_MIN_AGE_S", 300.0),
            early_tighten_be_min_mfe_r=f("EARLY_TIGHTEN_BE_MIN_MFE_R", 0.5),
            early_tighten_harvest_min_mfe_r=f(
                "EARLY_TIGHTEN_HARVEST_MIN_MFE_R", 1.0),
            algedonic_latch_enabled=b("ALGEDONIC_LATCH", True),
            budget_realized_enabled=b("BUDGET_REALIZED", True),
            mae_tracker_enabled=b("MAE_TRACKER", True),
            loss_veto_asset_grain_enabled=b("LOSS_VETO_ASSET_GRAIN", False),
            edge_boost_enabled=b("EDGE_BOOST", True),
            edge_boost_min_n=i("EDGE_BOOST_MIN_N", 5),
            edge_boost_min_expectancy=f("EDGE_BOOST_MIN_EXPECTANCY", 0.5),
            edge_boost_max_mult=f("EDGE_BOOST_MAX_MULT", 1.5),
            edge_boost_r_based_enabled=b("EDGE_BOOST_R_BASED", True),
            edge_boost_min_r=f("EDGE_BOOST_MIN_R", 0.10),
            edge_boost_full_n=i("EDGE_BOOST_FULL_N", 20),
            edge_boost_veto_quarantine_s=f(
                "EDGE_BOOST_VETO_QUARANTINE_S", 21600.0),
            edge_boost_requires_veto=b("EDGE_BOOST_REQUIRES_VETO", True),
            cohort_account_filter_enabled=b("COHORT_ACCOUNT_FILTER", True),
            cohort_epoch_ts=f("COHORT_EPOCH_TS", 0.0),
            close_restamp_window_s=f("CLOSE_RESTAMP_WINDOW_S", 600.0),
            rebound_tracker_enabled=b("REBOUND_TRACKER", True),
            rebound_sharpen_guard_enabled=b("REBOUND_SHARPEN_GUARD", True),
            rebound_arm_r=f("REBOUND_ARM_R", 0.5),
            rebound_recover_r=f("REBOUND_RECOVER_R", 0.0),
            rebound_min_count=i("REBOUND_MIN_COUNT", 2),
            rebound_suppress_log_ttl_s=f("REBOUND_SUPPRESS_LOG_TTL_S", 300.0),
            taker_fee_rate=f("TAKER_FEE_RATE", 0.00045),
            outbox_enabled=b("OUTBOX_ENABLED", False),
            outbox_path=os.environ.get("PROP_OUTBOX_PATH", ""),
            outbox_transport=os.environ.get("PROP_OUTBOX_TRANSPORT", "local"),
            outbox_host=os.environ.get("PROP_OUTBOX_HOST", "aria-prod-v2"),
            outbox_zone=os.environ.get("PROP_OUTBOX_ZONE", "europe-west3-c"),
            # ATS-59: overflow sleeves (demo) must not scoop capital_block
            # surplus from funded turbos. Explicit PROP_CAPITAL_SURPLUS wins;
            # when unset, overflow_only defaults surplus OFF (standby / observe).
            capital_surplus_enabled=(
                b("CAPITAL_SURPLUS", True)
                if "PROP_CAPITAL_SURPLUS" in os.environ
                else (not b("OVERFLOW_ONLY", False))
            ),
            signal_ttl_s=f("SIGNAL_TTL_S", 300.0),
            tradfi_signal_ttl_s=f("TRADFI_SIGNAL_TTL_S", 3600.0),
            tap_boot_replay=b("TAP_BOOT_REPLAY", True),
            asset_class=os.environ.get("PROP_ASSET_CLASS", "all"),
            families=os.environ.get("PROP_FAMILIES", ""),
            crypto_deny_enabled=b("CRYPTO_DENY", True),
            crypto_deny_symbols=os.environ.get("PROP_CRYPTO_DENY_SYMBOLS", ""),
            act_backoff_persist_enabled=b("ACT_BACKOFF_PERSIST", True),
            state_ttl_prune_enabled=b("STATE_TTL_PRUNE", True),
            tap_skew_guard_enabled=b("TAP_SKEW_GUARD", True),
            overflow_only=b("OVERFLOW_ONLY", False),
            overflow_peers=os.environ.get("PROP_OVERFLOW_PEERS", ""),
            overflow_stale_s=f("OVERFLOW_STALE_S", 90.0),
            kingdom_view_enabled=b("KINGDOM_VIEW", True),
            kingdom_view_path=os.environ.get("PROP_KINGDOM_VIEW_PATH", ""),
            kingdom_view_peers=os.environ.get("PROP_KINGDOM_VIEW_PEERS", ""),
            kingdom_view_write=b("KINGDOM_VIEW_WRITE", True),
            hl_marks_enabled=b("HL_MARKS_ENABLED", True),
            hl_marks_poll_s=f("HL_MARKS_POLL_S", 10.0),
            hl_marks_stale_s=f("HL_MARKS_STALE_S", 60.0),
            hl_plane_enabled=b("HL_PLANE", True),
            hl_plane_wallets=os.environ.get("PROP_HL_PLANE_WALLETS", ""),
            hl_plane_poll_s=f("HL_PLANE_POLL_S", 30.0),
            hl_plane_diverge_bps=f("HL_PLANE_DIVERGE_BPS", 50.0),
            hl_plane_jump_frac=f("HL_PLANE_JUMP_FRAC", 0.15),
        )
