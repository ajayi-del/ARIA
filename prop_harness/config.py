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
    # day budget via PROP_EVAL_DAILY_BUDGET_USD — turbo crypto $80 as of
    # 2026-11-11; default $40; coherence>7 big bets at 70-100% of
    # remaining budget, 1R/2.5R/5R ladder, +0.5R breakeven arm,
    # harvested-risk reloads, 30%-of-equity margin cap over the
    # 2x/4x/10x venue ladder). Sleeve-local env — do not raise the
    # default here (turbo2 stocks keeps its own day/trend floors).
    profile: str = "standard"
    eval_daily_budget_usd: float = 40.0
    eval_trend_budget_usd: float = 50.0
    eval_coh_big: float = 7.0
    eval_coh_max: float = 8.5
    eval_risk_frac: float = 0.70
    eval_margin_cap_frac: float = 0.30
    eval_be_arm_r: float = 0.5
    eval_max_reloads: int = 1
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
    # 2026-11-11 conquest: peak_usd≥$10 (was $30≈4.3R on $7 probes —
    # complementary to basket TP for single-leg mid-green). Floor
    # max($5, frac×peak); banked uses denser frac. PROP_MFE_SHARPEN=0
    # → H1 trail/BE path only. Bank-protect min stays institutional $20.
    mfe_sharpen_enabled: bool = True
    mfe_sharpen_peak_usd: float = 10.0
    mfe_sharpen_floor_frac: float = 0.5
    mfe_sharpen_min_floor_usd: float = 5.0
    mfe_sharpen_banked_floor_frac: float = 0.75
    # Basket TP conquest gate (ARIA treasury port) — EVERY evaluation
    # sleeve (turbo crypto, turbo2 stocks, future). N≥2, book uPnL ≥ $10
    # AND ≥25% of Σrisk → tax-bank harvest; trail giveback after a cleared
    # peak. Kill OFF = per-leg BE/trail/sharpen only. PROP_BASKET_TP=0
    # → legacy. Tax-bank / residual-pyramid have their own kills.
    basket_tp_enabled: bool = True
    basket_tp_min_n: int = 2
    basket_tp_min_book_usd: float = 10.0
    basket_tp_arm_frac: float = 0.25
    basket_tp_harvest_ratio: float = 0.60
    basket_tp_giveback_frac: float = 0.60
    basket_tp_cooloff_s: float = 90.0
    # Tax-bank: harvest ≥ max(ratio×book, highest single-leg peak $).
    # PROP_BASKET_TP_TAX_BANK=0 → ratio×book only.
    basket_tp_tax_bank_enabled: bool = True
    # Residual-pyramid: leave residual_frac qty when coh≥min AND thesis
    # intact (leg still green). Dark coh/thesis → fail-closed full close.
    # PROP_BASKET_TP_RESIDUAL=0 → always flatten tax targets.
    basket_tp_residual_pyramid_enabled: bool = True
    basket_tp_residual_frac: float = 0.25
    basket_tp_residual_min_coh: float = 4.0
    # Adaptive N∈[2,7] arm scaling (excellence graph). OFF → flat
    # min_book/arm_frac. PROP_BASKET_TP_ADAPTIVE=0 → flat thresholds.
    basket_tp_adaptive_thresholds: bool = True
    # Capital scale: $ floors × (equity/5000) or (profit_target/500).
    # Authoritative: verified equity FIRST, then profit_target, else 1.0.
    # PROP_BASKET_TP_CAPITAL_SCALE=0 → absolute $ floors.
    basket_tp_capital_scale_enabled: bool = True
    # Side-split tax: long and short books taxed independently so a
    # trending side harvests even when the peer side is red.
    # PROP_BASKET_TP_SIDE_SPLIT=0 → legacy combined book.
    basket_tp_side_split_enabled: bool = True
    # Eval profit hurdle used when equity is dark (scale fallback).
    # Default $500 = 10% of REF $5k; set PROP_EVAL_PROFIT_TARGET_USD=2500
    # for the larger account.
    eval_profit_target_usd: float = 500.0
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

    @property
    def floor_line(self) -> float:
        return self.initial_balance - self.internal_floor_usd

    @property
    def disabled_file(self) -> Path:
        return self.state_root / "DISABLED"

    @property
    def loss_veto_artifact(self) -> Path:
        if self.loss_veto_path:
            return Path(self.loss_veto_path).expanduser()
        return self.state_root / "program" / "loss_veto.json"

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
            eval_coh_max=f("EVAL_COH_MAX", 8.5),
            eval_risk_frac=f("EVAL_RISK_FRAC", 0.70),
            eval_margin_cap_frac=f("EVAL_MARGIN_CAP_FRAC", 0.30),
            eval_be_arm_r=f("EVAL_BE_ARM_R", 0.5),
            eval_max_reloads=i("EVAL_MAX_RELOADS", 1),
            slot_reserve_enabled=b("SLOT_RESERVE", False),
            slot_reserve_slots=i("SLOT_RESERVE_SLOTS", 1),
            eval_tradfi_min_risk_enabled=b("EVAL_TRADFI_MIN_RISK", True),
            eval_tradfi_min_risk_usd=f("EVAL_TRADFI_MIN_RISK_USD", 14.0),
            eval_tradfi_cost_hard_cap=f("EVAL_TRADFI_COST_HARD_CAP", 0.30),
            eval_tradfi_account_risk_enabled=b(
                "EVAL_TRADFI_ACCOUNT_RISK", True),
            eval_tradfi_account_risk_frac=f(
                "EVAL_TRADFI_ACCOUNT_RISK_FRAC", 0.005),
            eval_crypto_min_risk_enabled=b("EVAL_CRYPTO_MIN_RISK", True),
            eval_crypto_min_risk_usd=f("EVAL_CRYPTO_MIN_RISK_USD", 7.0),
            eval_crypto_cost_hard_cap=f("EVAL_CRYPTO_COST_HARD_CAP", 0.30),
            # Kill: PROP_CRYPTO_MIN_COH_FIRE=0 (legacy admit any coh).
            crypto_min_coh_fire_enabled=(
                False if os.environ.get("PROP_CRYPTO_MIN_COH_FIRE", "")
                .strip() in ("0", "0.0") else True
            ) if "PROP_CRYPTO_MIN_COH_FIRE" in os.environ else True,
            crypto_min_coh_fire=f("CRYPTO_MIN_COH_FIRE", 7.5),
            be_trail_invariant_enabled=b("BE_TRAIL_INVARIANT", True),
            coh_stamp_invariant_enabled=b("COH_STAMP_INVARIANT", True),
            day_budget_latch_enabled=b("DAY_BUDGET_LATCH", True),
            day_budget_latch_loss_r=f("DAY_BUDGET_LATCH_LOSS_R", -0.7),
            day_budget_latch_t_fast_s=f("DAY_BUDGET_LATCH_T_FAST_S", 300.0),
            day_budget_latch_spent_threshold=f(
                "DAY_BUDGET_LATCH_SPENT", 0.70),
            mfe_sharpen_enabled=b("MFE_SHARPEN", True),
            mfe_sharpen_peak_usd=f("MFE_SHARPEN_PEAK_USD", 10.0),
            mfe_sharpen_floor_frac=f("MFE_SHARPEN_FLOOR_FRAC", 0.5),
            mfe_sharpen_min_floor_usd=f("MFE_SHARPEN_MIN_FLOOR_USD", 5.0),
            mfe_sharpen_banked_floor_frac=f(
                "MFE_SHARPEN_BANKED_FLOOR_FRAC", 0.75),
            basket_tp_enabled=b("BASKET_TP", True),
            basket_tp_min_n=i("BASKET_TP_MIN_N", 2),
            basket_tp_min_book_usd=f("BASKET_TP_MIN_BOOK_USD", 10.0),
            basket_tp_arm_frac=f("BASKET_TP_ARM_FRAC", 0.25),
            basket_tp_harvest_ratio=f("BASKET_TP_HARVEST_RATIO", 0.60),
            basket_tp_giveback_frac=f("BASKET_TP_GIVEBACK", 0.60),
            basket_tp_cooloff_s=f("BASKET_TP_COOLOFF_S", 90.0),
            basket_tp_tax_bank_enabled=b("BASKET_TP_TAX_BANK", True),
            basket_tp_residual_pyramid_enabled=b("BASKET_TP_RESIDUAL", True),
            basket_tp_residual_frac=f("BASKET_TP_RESIDUAL_FRAC", 0.25),
            basket_tp_residual_min_coh=f("BASKET_TP_RESIDUAL_MIN_COH", 4.0),
            basket_tp_adaptive_thresholds=b("BASKET_TP_ADAPTIVE", True),
            basket_tp_capital_scale_enabled=b(
                "BASKET_TP_CAPITAL_SCALE", True),
            basket_tp_side_split_enabled=b(
                "BASKET_TP_SIDE_SPLIT", True),
            eval_profit_target_usd=f("EVAL_PROFIT_TARGET_USD", 500.0),
            big_bet_ladder_enabled=b("BIG_BET_LADDER", True),
            big_bet_ladder_min_risk_usd=f("BIG_BET_LADDER_MIN_RISK", 20.0),
            crypto_tp_tighten_enabled=b("CRYPTO_TP_TIGHTEN", True),
            bank_protect_enabled=b("BANK_PROTECT", True),
            bank_protect_min_usd=f("BANK_PROTECT_MIN_USD", 20.0),
            runner_bank_floor_enabled=b("RUNNER_BANK_FLOOR", True),
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
            cost_move_gate_enabled=b("COST_MOVE_GATE", True),
            cost_move_gate_live=b("COST_MOVE_LIVE", False),
            cost_move_theta=f("COST_MOVE_THETA", 0.25),
            stop_resize_telemetry_enabled=b("STOP_RESIZE_TELEMETRY", True),
            stop_resize_early_s=f("STOP_RESIZE_EARLY_S", 30.0),
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
        )
