"""
Module: core/infra_config.py
Purpose: Load and validate config/infrastructure.yaml into typed dataclasses.
Integration: Import _infra at module level in valuechain_monitor, cascade_tracker,
             bybit_feed, main.py. Call load_infra_config() once at startup.

Feature Flags:
  Every component has an .enabled flag that defaults to True (safe: features on).
  Setting enabled=false reverts to original behavior without code changes.

Failure Modes:
  - File missing → all defaults used, warning logged (non-fatal)
  - YAML parse error → all defaults used, error logged (non-fatal)
  - Unknown key → silently ignored (forward-compatible)

Rollback: Set any component.enabled = false in infrastructure.yaml + restart.
"""

import os
import structlog
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

log = structlog.get_logger(__name__)

_INFRA_PATH = Path("config/infrastructure.yaml")


# ── Dataclass schema ──────────────────────────────────────────────────────────

@dataclass
class CircuitBreakerConfig:
    enabled: bool = True
    failure_threshold: int = 3
    success_threshold: int = 2
    open_timeout_s: float = 60.0


@dataclass
class RpcEndpointConfig:
    url: str
    priority: int = 1


@dataclass
class ValueChainRpcConfig:
    enabled: bool = True
    endpoints: List[RpcEndpointConfig] = field(default_factory=list)
    timeout_ms: float = 5000.0
    endpoint_backoff_s: float = 60.0
    circuit_breaker: CircuitBreakerConfig = field(default_factory=CircuitBreakerConfig)

    @property
    def timeout_s(self) -> float:
        return self.timeout_ms / 1000.0

    def ordered_urls(self) -> List[str]:
        """Return endpoint URLs sorted by priority ascending."""
        return [ep.url for ep in sorted(self.endpoints, key=lambda e: e.priority)]


@dataclass
class BybitReconnectConfig:
    base_delay_s: float = 1.0
    max_delay_s: float = 60.0
    backoff_multiplier: float = 2.0


@dataclass
class BybitCacheConfig:
    enabled: bool = True
    max_age_s: float = 300.0


@dataclass
class BybitWebsocketConfig:
    enabled: bool = True
    reconnect: BybitReconnectConfig = field(default_factory=BybitReconnectConfig)
    cache: BybitCacheConfig = field(default_factory=BybitCacheConfig)


@dataclass
class FreezeFlagConfig:
    hard_timeout_s: float = 120.0
    bypass_on_extreme_zscore: bool = True
    extreme_zscore_threshold: float = 4.0


@dataclass
class DwellTier:
    zscore_min: float
    dwell_s: float


@dataclass
class DynamicDwellConfig:
    enabled: bool = True
    tiers: List[DwellTier] = field(default_factory=lambda: [
        DwellTier(4.0, 15.0),
        DwellTier(3.0, 30.0),
        DwellTier(0.0, 60.0),
    ])

    def get_dwell(self, zscore: float) -> float:
        """Return dwell_s for the first tier whose zscore_min ≤ zscore."""
        for tier in sorted(self.tiers, key=lambda t: t.zscore_min, reverse=True):
            if zscore >= tier.zscore_min:
                return tier.dwell_s
        return 60.0


@dataclass
class StatePersistenceConfig:
    enabled: bool = True
    state_file: str = "logs/cascade_state.json"
    max_age_s: float = 300.0


@dataclass
class CascadeTrackerConfig:
    freeze: FreezeFlagConfig = field(default_factory=FreezeFlagConfig)
    dynamic_dwell: DynamicDwellConfig = field(default_factory=DynamicDwellConfig)
    state_persistence: StatePersistenceConfig = field(default_factory=StatePersistenceConfig)


@dataclass
class ValueChainStateConfig:
    enabled: bool = True
    state_file: str = "logs/valuechain_state.json"
    max_age_s: float = 300.0
    save_every_n_polls: int = 5


@dataclass
class HealthEndpointConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 9090
    require_auth: bool = False


@dataclass
class MetricsConfig:
    enabled: bool = True
    export_path: str = "/metrics"


@dataclass
class InfraConfig:
    valuechain_rpc: ValueChainRpcConfig = field(default_factory=ValueChainRpcConfig)
    bybit_websocket: BybitWebsocketConfig = field(default_factory=BybitWebsocketConfig)
    cascade_tracker: CascadeTrackerConfig = field(default_factory=CascadeTrackerConfig)
    valuechain_state: ValueChainStateConfig = field(default_factory=ValueChainStateConfig)
    health_endpoint: HealthEndpointConfig = field(default_factory=HealthEndpointConfig)
    metrics: MetricsConfig = field(default_factory=MetricsConfig)


# ── Loader ────────────────────────────────────────────────────────────────────

def load_infra_config(path: Path = _INFRA_PATH) -> InfraConfig:
    """
    Load infrastructure.yaml from disk.

    Returns InfraConfig with all defaults if file is missing or unreadable.
    Never raises — infrastructure config failure must not prevent bot startup.
    """
    if not path.exists():
        log.info("infra_config_not_found",
                 path=str(path),
                 note="using all defaults — create config/infrastructure.yaml to customize")
        return InfraConfig()

    try:
        import yaml
    except ImportError:
        log.warning("pyyaml_not_installed",
                    note="pip install pyyaml to enable infrastructure.yaml loading — using defaults")
        return InfraConfig()

    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    except Exception as e:
        log.error("infra_config_parse_error",
                  path=str(path),
                  error=str(e),
                  note="using all defaults")
        return InfraConfig()

    cfg = InfraConfig()

    # valuechain_rpc
    if rpc := raw.get("valuechain_rpc"):
        cfg.valuechain_rpc.enabled = bool(rpc.get("enabled", True))
        cfg.valuechain_rpc.timeout_ms = float(rpc.get("timeout_ms", 5000))
        cfg.valuechain_rpc.endpoint_backoff_s = float(rpc.get("endpoint_backoff_s", 60.0))
        if eps := rpc.get("endpoints"):
            cfg.valuechain_rpc.endpoints = [
                RpcEndpointConfig(url=ep["url"], priority=int(ep.get("priority", 1)))
                for ep in eps if isinstance(ep, dict) and ep.get("url")
            ]
        if cb := rpc.get("circuit_breaker"):
            cfg.valuechain_rpc.circuit_breaker = CircuitBreakerConfig(
                enabled=bool(cb.get("enabled", True)),
                failure_threshold=int(cb.get("failure_threshold", 3)),
                success_threshold=int(cb.get("success_threshold", 2)),
                open_timeout_s=float(cb.get("open_timeout_s", 60.0)),
            )

    # bybit_websocket
    if bws := raw.get("bybit_websocket"):
        cfg.bybit_websocket.enabled = bool(bws.get("enabled", True))
        if rc := bws.get("reconnect"):
            cfg.bybit_websocket.reconnect = BybitReconnectConfig(
                base_delay_s=float(rc.get("base_delay_s", 1.0)),
                max_delay_s=float(rc.get("max_delay_s", 60.0)),
                backoff_multiplier=float(rc.get("backoff_multiplier", 2.0)),
            )
        if cache := bws.get("cache"):
            cfg.bybit_websocket.cache = BybitCacheConfig(
                enabled=bool(cache.get("enabled", True)),
                max_age_s=float(cache.get("max_age_s", 300.0)),
            )

    # cascade_tracker
    if ct := raw.get("cascade_tracker"):
        if fr := ct.get("freeze"):
            cfg.cascade_tracker.freeze = FreezeFlagConfig(
                hard_timeout_s=float(fr.get("hard_timeout_s", 120.0)),
                bypass_on_extreme_zscore=bool(fr.get("bypass_on_extreme_zscore", True)),
                extreme_zscore_threshold=float(fr.get("extreme_zscore_threshold", 4.0)),
            )
        if dd := ct.get("dynamic_dwell"):
            tiers = [
                DwellTier(zscore_min=float(t["zscore_min"]), dwell_s=float(t["dwell_s"]))
                for t in dd.get("tiers", []) if isinstance(t, dict)
            ] or DynamicDwellConfig().tiers
            cfg.cascade_tracker.dynamic_dwell = DynamicDwellConfig(
                enabled=bool(dd.get("enabled", True)),
                tiers=tiers,
            )
        if sp := ct.get("state_persistence"):
            cfg.cascade_tracker.state_persistence = StatePersistenceConfig(
                enabled=bool(sp.get("enabled", True)),
                state_file=str(sp.get("state_file", "logs/cascade_state.json")),
                max_age_s=float(sp.get("max_age_s", 300.0)),
            )

    # valuechain_state
    if vs := raw.get("valuechain_state"):
        cfg.valuechain_state = ValueChainStateConfig(
            enabled=bool(vs.get("enabled", True)),
            state_file=str(vs.get("state_file", "logs/valuechain_state.json")),
            max_age_s=float(vs.get("max_age_s", 300.0)),
            save_every_n_polls=int(vs.get("save_every_n_polls", 5)),
        )

    # health_endpoint
    if he := raw.get("health_endpoint"):
        cfg.health_endpoint = HealthEndpointConfig(
            enabled=bool(he.get("enabled", True)),
            host=str(he.get("host", "0.0.0.0")),
            port=int(he.get("port", 9090)),
            require_auth=bool(he.get("require_auth", False)),
        )

    # metrics
    if m := raw.get("metrics"):
        cfg.metrics = MetricsConfig(
            enabled=bool(m.get("enabled", True)),
            export_path=str(m.get("export_path", "/metrics")),
        )

    log.info("infra_config_loaded",
             path=str(path),
             valuechain_rpc_enabled=cfg.valuechain_rpc.enabled,
             cascade_freeze_bypass=cfg.cascade_tracker.freeze.bypass_on_extreme_zscore,
             dynamic_dwell_enabled=cfg.cascade_tracker.dynamic_dwell.enabled,
             state_persistence_enabled=cfg.cascade_tracker.state_persistence.enabled,
             health_endpoint_port=cfg.health_endpoint.port if cfg.health_endpoint.enabled else None)

    return cfg


# Module-level singleton — loaded once at import time so all modules share the same instance.
# Callers may call load_infra_config() explicitly to reload (e.g., on SIGHUP).
_infra: InfraConfig = InfraConfig()   # safe defaults until load_infra_config() is called


def get_infra() -> InfraConfig:
    """Return the module-level InfraConfig singleton."""
    return _infra


def init_infra(path: Path = _INFRA_PATH) -> InfraConfig:
    """Load infrastructure.yaml and replace the module-level singleton. Call once at startup."""
    global _infra
    _infra = load_infra_config(path)
    return _infra
