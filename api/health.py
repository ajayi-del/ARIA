"""
Module: api/health.py
Purpose: Lightweight HTTP health and readiness probes for ARIA process monitoring.
Integration: Instantiate HealthServer in main.py after all components initialize.
             Call await health_server.start() and pass component references via
             health_server.register(name, probe_fn).

Feature Flags (config/infrastructure.yaml):
  health_endpoint.enabled: bool   # false → server never starts, no-op
  health_endpoint.host: str       # default 0.0.0.0
  health_endpoint.port: int       # default 9090

Endpoints:
  GET /health        → 200 {"status": "ok"}              (liveness — process alive)
  GET /health/ready  → 200|503 readiness with component detail
  GET /metrics       → Prometheus text format (if prometheus_client installed)

Failure Modes:
  - Port already in use → logs error, does NOT crash ARIA (non-fatal)
  - Any probe raises → component marked "error", still responds
  - prometheus_client missing → /metrics returns {"error": "metrics unavailable"}

Rollback: health_endpoint.enabled: false in infrastructure.yaml + restart
"""

import asyncio
import json
import time
import structlog
from typing import Callable, Awaitable, Dict, Optional

log = structlog.get_logger(__name__)


class HealthServer:
    """
    aiohttp-based health/ready/metrics server.

    All ARIA components that can be checked for health register a probe function:
        async def probe() -> dict:
            return {"status": "healthy|degraded|down", ...}

    The /health/ready response aggregates all probe results. If any component
    returns "down", the overall status is "unavailable" (HTTP 503).
    If any returns "degraded", the overall status is "degraded" (HTTP 200).
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9090,
        enabled: bool = True,
    ):
        self.host = host
        self.port = port
        self.enabled = enabled
        self._start_time = time.time()
        self._probes: Dict[str, Callable[[], Awaitable[dict]]] = {}
        self._server: Optional[object] = None

    def register(self, name: str, probe_fn: Callable[[], Awaitable[dict]]) -> None:
        """
        Register an async probe for a component.
        probe_fn must return a dict with at least {"status": "healthy|degraded|down"}.
        """
        self._probes[name] = probe_fn

    async def start(self) -> None:
        """Start the HTTP server as a background task. Non-fatal if port is in use."""
        if not self.enabled:
            log.info("health_server_disabled", note="health_endpoint.enabled=false")
            return
        try:
            from aiohttp import web
            app = web.Application()
            app.router.add_get("/health",       self._handle_liveness)
            app.router.add_get("/health/ready", self._handle_readiness)
            app.router.add_get("/metrics",      self._handle_metrics)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, self.host, self.port)
            await site.start()
            self._server = runner
            log.info("health_server_started",
                     host=self.host,
                     port=self.port,
                     endpoints=["/health", "/health/ready", "/metrics"])
        except OSError as e:
            # Port in use — log and continue. ARIA runs without health endpoint.
            log.error("health_server_port_in_use",
                      host=self.host,
                      port=self.port,
                      error=str(e),
                      note="health endpoint unavailable — change health_endpoint.port")
        except Exception as e:
            log.error("health_server_start_failed", error=str(e))

    async def stop(self) -> None:
        if self._server:
            try:
                await self._server.cleanup()
            except Exception:
                pass

    # ── HTTP Handlers ────────────────────────────────────────────────────────────

    async def _handle_liveness(self, request) -> object:
        from aiohttp import web
        return web.Response(
            content_type="application/json",
            text=json.dumps({"status": "ok", "uptime_seconds": round(time.time() - self._start_time, 0)}),
        )

    async def _handle_readiness(self, request) -> object:
        from aiohttp import web
        components = {}
        overall = "ready"

        for name, probe_fn in self._probes.items():
            try:
                result = await asyncio.wait_for(probe_fn(), timeout=2.0)
                components[name] = result
                status = result.get("status", "unknown")
                if status == "down":
                    overall = "unavailable"
                elif status == "degraded" and overall == "ready":
                    overall = "degraded"
            except asyncio.TimeoutError:
                components[name] = {"status": "down", "error": "probe_timeout"}
                overall = "unavailable"
            except Exception as e:
                components[name] = {"status": "error", "error": str(e)[:80]}
                if overall == "ready":
                    overall = "degraded"

        body = {
            "status": overall,
            "components": components,
            "uptime_seconds": round(time.time() - self._start_time, 0),
        }
        http_status = 503 if overall == "unavailable" else 200
        return web.Response(
            status=http_status,
            content_type="application/json",
            text=json.dumps(body, default=str),
        )

    async def _handle_metrics(self, request) -> object:
        from aiohttp import web
        try:
            from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
            content = generate_latest()
            return web.Response(body=content, content_type=CONTENT_TYPE_LATEST)
        except ImportError:
            return web.Response(
                status=503,
                content_type="application/json",
                text=json.dumps({"error": "prometheus_client not installed — pip install prometheus-client"}),
            )
        except Exception as e:
            return web.Response(
                status=500,
                content_type="application/json",
                text=json.dumps({"error": str(e)}),
            )


# ── Standard probe factories ───────────────────────────────────────────────────

def make_valuechain_probe(vc_monitor) -> Callable[[], Awaitable[dict]]:
    """
    Returns an async probe for ValueChainMonitor health.
    Exposed as GET /health/ready → components.valuechain_rpc
    """
    async def probe() -> dict:
        if vc_monitor is None:
            return {"status": "down", "error": "not_initialized"}
        status = vc_monitor.get_status()
        healthy = status.get("healthy", False)
        return {
            "status": "healthy" if healthy else "degraded",
            "endpoint": status.get("rpc_endpoint", "unknown"),
            "last_block": status.get("last_block", 0),
            "consecutive_failures": status.get("consecutive_failures", 0),
            "cascade_active": status.get("cascade_active", False),
            "cascade_zscore": status.get("cascade_zscore", 0.0),
        }
    return probe


def make_bybit_probe(bybit_feed) -> Callable[[], Awaitable[dict]]:
    """Returns an async probe for BybitFeed connection health."""
    async def probe() -> dict:
        if bybit_feed is None:
            return {"status": "down", "error": "not_initialized"}
        subscribed = len(getattr(bybit_feed, "_subscribed", set()))
        reconnect_attempts = getattr(bybit_feed, "_reconnect_attempts", 0)
        running = getattr(bybit_feed, "_running", False)
        return {
            "status": "healthy" if running and subscribed > 0 else "degraded",
            "running": running,
            "subscribed_symbols": subscribed,
            "reconnect_attempts": reconnect_attempts,
        }
    return probe


def make_cascade_probe(cascade_tracker) -> Callable[[], Awaitable[dict]]:
    """Returns an async probe for CascadeTracker phase state."""
    async def probe() -> dict:
        if cascade_tracker is None:
            return {"status": "down", "error": "not_initialized"}
        phase = getattr(cascade_tracker, "_phase", None)
        return {
            "status": "healthy",
            "phase": phase.value if phase else "unknown",
            "block_zscore": round(getattr(cascade_tracker, "_block_zscore", 0.0), 2),
        }
    return probe
