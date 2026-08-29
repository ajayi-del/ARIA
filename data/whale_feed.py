"""Whale feed — watched-address snapshot pollers (Deploy 5, 2026-08-29).

Two unsigned public data planes:
  SoDEX  — GET /api/v1/perps/accounts/{address}/positions (verified live
           2026-08-28/29): data.positions[], symbol/size(negative=short)/
           avgEntryPrice. Direct position snapshots, 60s cadence.
  Aster  — POST /bapi/futures/v1/public/campaign/trade/pro/leaderboard/rank
           {address, period, sort, symbol} → per-symbol pnl/volume.
           CAMPAIGN-SCOPED: the pro campaign going dark (observed
           2026-08-29 — empty data across all periods) is detected and
           logged whale_feed_campaign_dark; the leg abstains while dark
           (mover_radar doctrine: never trade a dark data plane).

Snapshots append to logs/whale_snapshots.jsonl (append-only, one-bad-line
doctrine — same reasoning as the shadow journal). Every method never
raises; the supervising loop owns backoff.
"""
import json
import os
import time

import certifi
import httpx
import structlog

logger = structlog.get_logger(__name__)

SODEX_BASE = "https://mainnet-gw.sodex.dev/api/v1/perps"
ASTER_RANK_URL = ("https://www.asterdex.com/bapi/futures/v1/public/"
                  "campaign/trade/pro/leaderboard/rank")


def sodex_positions_from_payload(payload: dict) -> dict:
    """{symbol: signed_size} from the verified contract (data.positions[];
    negative size = short — same parsing as the startup-sync adoption).
    Unparseable rows are skipped, never fatal."""
    out = {}
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    rows = (data.get("positions") or data.get("P") or []) if isinstance(data, dict) else []
    for p in rows:
        if not isinstance(p, dict):
            continue
        sym = p.get("symbol", "") or p.get("coin", "")
        try:
            size = float(p.get("size", 0) or p.get("qty", 0) or 0)
        except (TypeError, ValueError):
            continue
        if sym and size != 0.0:
            out[sym] = size
    return out


class WhaleFeed:
    """Polls both planes and journals snapshots. Never raises."""

    def __init__(self, registry: list, aster_symbols: list,
                 log_dir: str = "logs"):
        self._registry = [w for w in registry if w.get("address")]
        self._aster_symbols = list(aster_symbols)
        self._path = os.path.join(log_dir, "whale_snapshots.jsonl")
        self._client: httpx.AsyncClient | None = None
        self._campaign_dark = False

    async def _cli(self) -> httpx.AsyncClient:
        if self._client is None:
            import ssl
            ctx = ssl.create_default_context(cafile=certifi.where())
            self._client = httpx.AsyncClient(timeout=15.0, verify=ctx)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    def _journal(self, rec: dict) -> None:
        try:
            with open(self._path, "a", buffering=1) as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception as e:
            logger.warning("whale_feed_journal_error", error=str(e)[:120])

    @property
    def campaign_dark(self) -> bool:
        return self._campaign_dark

    async def poll_sodex(self) -> dict:
        """address -> {symbol: signed_size} for every sodex-venue whale."""
        out = {}
        cli = await self._cli()
        for w in self._registry:
            if w.get("venue") != "sodex":
                continue
            addr = w["address"]
            try:
                r = await cli.get(f"{SODEX_BASE}/accounts/{addr}/positions")
                if r.status_code != 200:
                    logger.warning("whale_feed_sodex_http", address=addr[:10],
                                   status=r.status_code)
                    continue
                payload = r.json()
                poss = sodex_positions_from_payload(payload)
                out[addr] = poss
                self._journal({"ts": time.time(), "venue": "sodex",
                               "address": addr, "label": w.get("label", ""),
                               "positions": poss})
            except Exception as e:
                logger.warning("whale_feed_sodex_error", address=addr[:10],
                               error=str(e)[:120])
        return out

    async def poll_aster(self) -> dict:
        """(address, symbol) -> {pnl, volume, rank}. Campaign-dark detection:
        ALL rows empty across the whole registry × symbol set = the campaign
        is off (verified live 2026-08-29) → abstain + log once per flip."""
        out = {}
        cli = await self._cli()
        any_data = False
        for w in self._registry:
            if w.get("venue") != "aster":
                continue
            addr = w["address"]
            for sym in self._aster_symbols:
                try:
                    r = await cli.post(ASTER_RANK_URL, json={
                        "address": addr, "period": "ALL",
                        "sort": "pnl", "symbol": sym})
                    if r.status_code != 200:
                        continue
                    d = r.json().get("data") or {}
                    if not d:
                        continue
                    any_data = True
                    rec = {"pnl": float(d.get("pnl", 0) or 0),
                           "volume": float(d.get("volume", 0) or 0),
                           "rank": d.get("rank")}
                    out[(addr, sym)] = rec
                    self._journal({"ts": time.time(), "venue": "aster",
                                   "address": addr, "symbol": sym,
                                   "label": w.get("label", ""), **rec})
                except Exception as e:
                    logger.warning("whale_feed_aster_error", address=addr[:10],
                                   symbol=sym, error=str(e)[:120])
        if not any_data and not self._campaign_dark:
            self._campaign_dark = True
            logger.info("whale_feed_campaign_dark",
                        note="aster pro leaderboard empty — leg abstaining")
        elif any_data and self._campaign_dark:
            self._campaign_dark = False
            logger.info("whale_feed_campaign_live")
        return out
