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
    # Eval tradfi/stocks probe floor (ATS-35): $5 probes die when round-trip
    # taker ($1.29 on a ~0.35% stop) exceeds 10% of risk. Floor probe risk
    # at $7 for xyz: under evaluation, and cost-aware-raise the fee/risk
    # allowance up to eval_tradfi_cost_hard_cap (size-invariant geometry).
    # Day budget ($40) still clamps. PROP_EVAL_TRADFI_MIN_RISK=0 → legacy $5.
    eval_tradfi_min_risk_enabled: bool = True
    eval_tradfi_min_risk_usd: float = 7.0
    eval_tradfi_cost_hard_cap: float = 0.30
    # Eval crypto probe floor (ATS-39): same wound on turbo capital_block
    # surplus after #24 coh fix — $5 probes still die fee%. Mirror ATS-35
    # for non-xyz: under evaluation. PROP_EVAL_CRYPTO_MIN_RISK=0 → legacy.
    eval_crypto_min_risk_enabled: bool = True
    eval_crypto_min_risk_usd: float = 7.0
    eval_crypto_cost_hard_cap: float = 0.30
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
            eval_tradfi_min_risk_usd=f("EVAL_TRADFI_MIN_RISK_USD", 7.0),
            eval_tradfi_cost_hard_cap=f("EVAL_TRADFI_COST_HARD_CAP", 0.30),
            eval_crypto_min_risk_enabled=b("EVAL_CRYPTO_MIN_RISK", True),
            eval_crypto_min_risk_usd=f("EVAL_CRYPTO_MIN_RISK_USD", 7.0),
            eval_crypto_cost_hard_cap=f("EVAL_CRYPTO_COST_HARD_CAP", 0.30),
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
