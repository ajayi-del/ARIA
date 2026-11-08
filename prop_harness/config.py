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
    signal_ttl_s: float = 300.0
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
            loss_veto_enabled=b("LOSS_VETO_ENABLED", True),
            loss_veto_path=os.environ.get("PROP_LOSS_VETO_PATH", ""),
            loss_veto_min_n=i("LOSS_VETO_MIN_N", 5),
            protection_resize_enabled=b("PROTECTION_RESIZE_ENABLED", True),
            family_cap_enabled=b("FAMILY_CAP_ENABLED", True),
            cost_risk_max_frac=f("COST_RISK_MAX_FRAC", 0.10),
            taker_fee_rate=f("TAKER_FEE_RATE", 0.00045),
            outbox_enabled=b("OUTBOX_ENABLED", False),
            outbox_path=os.environ.get("PROP_OUTBOX_PATH", ""),
            outbox_transport=os.environ.get("PROP_OUTBOX_TRANSPORT", "local"),
            outbox_host=os.environ.get("PROP_OUTBOX_HOST", "aria-prod-v2"),
            outbox_zone=os.environ.get("PROP_OUTBOX_ZONE", "europe-west3-c"),
            signal_ttl_s=f("SIGNAL_TTL_S", 300.0),
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
