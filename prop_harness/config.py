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

    max_positions: int = 3
    disable_flattens: bool = False

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

    @property
    def floor_line(self) -> float:
        return self.initial_balance - self.internal_floor_usd

    @property
    def disabled_file(self) -> Path:
        return self.state_root / "DISABLED"

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
            outbox_enabled=b("OUTBOX_ENABLED", False),
            outbox_path=os.environ.get("PROP_OUTBOX_PATH", ""),
            outbox_transport=os.environ.get("PROP_OUTBOX_TRANSPORT", "local"),
            outbox_host=os.environ.get("PROP_OUTBOX_HOST", "aria-prod-v2"),
            outbox_zone=os.environ.get("PROP_OUTBOX_ZONE", "europe-west3-c"),
            signal_ttl_s=f("SIGNAL_TTL_S", 300.0),
        )
