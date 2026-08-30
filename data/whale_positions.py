"""Whale Position Plane (WPP) — address-scoped position resolution.

Deploy 2026-08-30 (operator directive "build test and ship" + spec audit):
the DaVinci bypass for the dark Aster campaign leg — you don't need a
"whale positions API", you need Discovery → Resolution → Change Detection.

Two unsigned address-scoped planes (both live-probed 2026-08-30):
  Aster RPC  — POST https://tapi.asterdex.com/info
               method aster_getBalance(address, "latest"). Verified serving
               REAL account data against two UI-known whales (0xE1d71a…:
               BTCUSDT 44.835 @ $3.5M notional; 0x4ea29D…: ETHUSDT 4,254
               @ $10.5M). Unknown addresses return a distinguishable
               -32603/"account does not exist" error. Schema:
               result.positions[].positions[]{symbol, positionAmount(signed
               str), positionSide, notionalValue, unrealizedProfit,
               cumRealized} + result.perpAssets[]{asset, walletBalance}.
               Entry/mark are DERIVED (mark = notional/|size|, entry =
               mark − upnl/|size| long, + short) → confidence 0.9.
               margin/leverage NOT provided → None (never derived —
               cross/isolated semantics unknown).
  Hyperliquid — POST https://api.hyperliquid.xyz/info
               {"type":"clearinghouseState","user":addr}. Transport live-
               probed (empty assetPositions for the Aster whales — same
               EVM address space, so the whole registry is polled on both
               venues). assetPositions[].position{coin, szi(signed),
               entryPx, positionValue, unrealizedPnl, liquidationPx,
               marginUsed, leverage{value}} + marginSummary.accountValue —
               all native → confidence 1.0.

Audit amendments baked in (2026-08-30 reviewer pass):
  - margin_used_usd is NATIVE-ONLY (None when the venue doesn't publish
    it) — never notional/leverage arithmetic.
  - opened_at_confidence: HIGH = we observed the open (0 → nonzero in our
    own delta stream); LOW = position predates our watch (first-snapshot
    bags are NOT fresh opens — Hasbrouck aged-bag doctrine).
  - The whale "score" is a FEATURE VECTOR, not a confidence number —
    hand-picked weights are a hypothesis; the calibrated predictive layer
    (E[R | event, context]) is learned from the shadow journal once n
    accrues (Aronson). No fake precision before data.
  - Flow events carry the WhaleMirror contract exactly ({venue, address,
    symbol, direction, kind, size, prev_size, quality, ts}) so the
    existing consensus/probe machinery consumes them unchanged — the
    Aster leg upgrades from INFERRED (dark) to DIRECT without touching
    whale_mirror's doctrines.
  - QUANTITY delta, not notional delta (external audit P0, 2026-08-30):
    a snapshot is not a trade event. Behavior classifies on Δqty; the
    notional delta decomposes into estimated_trade_notional (|Δqty| ×
    event price — behavior) and mtm_change_usd (prev qty × Δprice —
    revaluation). A constant-quantity hold through a 10% pump emits
    NOTHING — before this fix it emitted ADDED, which feeds the LIVE
    consensus size boost on zero information.

Journal: logs/whale_positions.jsonl (append-only, one-bad-line doctrine).
Every method never raises; the supervising loop owns backoff.
"""
import json
import os
import time

import certifi
import httpx
import structlog

logger = structlog.get_logger(__name__)

ASTER_RPC_URL = "https://tapi.asterdex.com/info"
HL_INFO_URL = "https://api.hyperliquid.xyz/info"

# Flow kinds — identical vocabulary to intelligence/whale_mirror.py
OPENED = "opened"
ADDED = "added"
TRIMMED = "trimmed"
CLOSED = "closed"
FLIPPED = "flipped"

# Whale tiers by account value (margin-side capital, not notional —
# notional is leverage-inflated). Feature only, never a gate.
_TIERS = ((20e6, "LEVIATHAN"), (5e6, "MEGA_WHALE"), (1e6, "LARGE_WHALE"),
          (5e5, "WHALE"), (1e5, "LARGE"))


def whale_tier(account_value_usd: float | None) -> str:
    if account_value_usd is None or account_value_usd <= 0:
        return "UNKNOWN"
    for floor, name in _TIERS:
        if account_value_usd >= floor:
            return name
    return "RETAIL"


def normalize_symbol(raw: str) -> str:
    """BTCUSDT / BTC → BTC-USD (ARIA convention)."""
    s = raw.upper().strip()
    if s.endswith("USDT"):
        s = s[:-4]
    if not s.endswith("-USD"):
        s = f"{s}-USD"
    return s


def _f(v) -> float | None:
    try:
        x = float(v)
        return x if x == x else None  # NaN guard
    except (TypeError, ValueError):
        return None


# ── Adapters ──────────────────────────────────────────────────────────────

def parse_aster_balance(payload: dict, address: str, now: float) -> list:
    """aster_getBalance result → position dicts. Verified live schema
    2026-08-30. Unparseable rows are skipped, never fatal."""
    out = []
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        return out
    wallet = None
    for a in (result.get("perpAssets") or []):
        w = _f(a.get("walletBalance"))
        if w:
            wallet = (wallet or 0.0) + w
    for group in (result.get("positions") or []):
        for p in ((group or {}).get("positions") or []):
            size = _f(p.get("positionAmount"))
            notional = _f(p.get("notionalValue"))
            upnl = _f(p.get("unrealizedProfit"))
            sym_raw = p.get("symbol") or ""
            if not size or not sym_raw or notional is None:
                continue
            side = "long" if size > 0 else "short"
            abs_sz = abs(size)
            mark = notional / abs_sz if abs_sz > 0 else None
            entry = None
            if mark is not None and upnl is not None and abs_sz > 0:
                entry = (mark - upnl / abs_sz) if side == "long" \
                    else (mark + upnl / abs_sz)
            out.append({
                "venue": "aster", "address": address,
                "symbol": normalize_symbol(sym_raw), "side": side,
                "size": abs_sz, "notional_usd": notional,
                "margin_used_usd": None, "leverage": None,
                "entry_price": entry, "mark_price": mark,
                "liquidation_price": None,
                "unrealized_pnl": upnl,
                "account_value_usd": wallet,
                "source": "aster_rpc", "confidence": 0.9,
                "updated_at": now,
            })
    return out


def parse_hl_clearinghouse(payload: dict, address: str, now: float) -> list:
    """clearinghouseState → position dicts (all native fields). Empty
    assetPositions is a valid answer (address not on HL) → []."""
    out = []
    if not isinstance(payload, dict):
        return out
    account_value = _f((payload.get("marginSummary") or {})
                       .get("accountValue"))
    for ap in (payload.get("assetPositions") or []):
        pos = (ap or {}).get("position") or {}
        szi = _f(pos.get("szi"))
        coin = pos.get("coin") or ""
        if not szi or not coin:
            continue
        side = "long" if szi > 0 else "short"
        lev = pos.get("leverage") or {}
        out.append({
            "venue": "hyperliquid", "address": address,
            "symbol": normalize_symbol(coin), "side": side,
            "size": abs(szi),
            "notional_usd": _f(pos.get("positionValue")),
            "margin_used_usd": _f(pos.get("marginUsed")),
            "leverage": _f(lev.get("value")),
            "entry_price": _f(pos.get("entryPx")),
            "mark_price": None,
            "liquidation_price": _f(pos.get("liquidationPx")),
            "unrealized_pnl": _f(pos.get("unrealizedPnl")),
            "account_value_usd": account_value,
            "source": "hyperliquid", "confidence": 1.0,
            "updated_at": now,
        })
    return out


class WhalePositionPlane:
    """Polls Aster RPC + Hyperliquid for every registry address, diffs
    consecutive snapshots, and emits WhaleMirror-contract flow events.
    Never raises."""

    def __init__(self, registry: list, log_dir: str = "logs",
                 min_notional_delta_usd: float = 10_000.0,
                 time_fn=time.time):
        # min_notional_delta_usd: emission floor on the ESTIMATED TRADE
        # notional (|Δqty| × event price), not the raw notional delta —
        # revaluation never emits (audit P0 2026-08-30).
        self._registry = [w for w in registry if w.get("address")]
        self._path = os.path.join(log_dir, "whale_positions.jsonl")
        self._min_delta = float(min_notional_delta_usd)
        self._time = time_fn
        self._client: httpx.AsyncClient | None = None
        self._prev: dict = {}          # (venue, address, symbol) -> position
        self._first_seen: dict = {}    # (venue, address, symbol) -> ts
        self._account_values: dict = {}  # (venue, address) -> usd
        self._aster_dead = False

    async def _cli(self) -> httpx.AsyncClient:
        if self._client is None:
            import ssl
            ctx = ssl.create_default_context(cafile=certifi.where())
            self._client = httpx.AsyncClient(timeout=10.0, verify=ctx)
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
            logger.warning("whale_positions_journal_error", error=str(e)[:120])

    # ── Venue polls ──────────────────────────────────────────────────────

    async def _fetch_aster(self, address: str) -> list:
        cli = await self._cli()
        try:
            r = await cli.post(ASTER_RPC_URL, json={
                "jsonrpc": "2.0", "id": 1, "method": "aster_getBalance",
                "params": [address, "latest"]})
            if r.status_code != 200:
                logger.warning("whale_positions_aster_http",
                               address=address[:10], status=r.status_code)
                return []
            payload = r.json()
            if isinstance(payload, dict) and payload.get("error"):
                # -32603 "account does not exist" — a VALID negative answer
                # (address has no futures account), not a transport fault.
                msg = str((payload.get("error") or {}).get("message", ""))
                if "does not exist" not in msg:
                    logger.warning("whale_positions_aster_rpc_error",
                                   address=address[:10], error=msg[:120])
                return []
            return parse_aster_balance(payload, address, self._time())
        except Exception as e:
            logger.warning("whale_positions_aster_error",
                           address=address[:10], error=str(e)[:120])
            return []

    async def _fetch_hl(self, address: str) -> list:
        cli = await self._cli()
        try:
            r = await cli.post(HL_INFO_URL, json={
                "type": "clearinghouseState", "user": address})
            if r.status_code != 200:
                logger.warning("whale_positions_hl_http",
                               address=address[:10], status=r.status_code)
                return []
            return parse_hl_clearinghouse(r.json(), address, self._time())
        except Exception as e:
            logger.warning("whale_positions_hl_error",
                           address=address[:10], error=str(e)[:120])
            return []

    # ── Delta engine (the product is behavior, not positions) ───────────

    def _diff_one(self, pos: dict) -> dict | None:
        """One flow event on CHANGE (WhaleMirror contract), else None.

        Quantity-vs-notional separation (external audit P0, 2026-08-30): a
        position snapshot is NOT a trade event. Classification keys on the
        QUANTITY delta; the notional delta decomposes into behavior
        (|Δqty| × event price = estimated_trade_notional) and revaluation
        (prev qty × Δprice = mtm_change_usd). A whale holding a constant
        quantity through a 10% pump emits NOTHING — mark-to-market is not
        behavior, and a revaluation-driven ADDED would fire the live
        consensus size boost on zero information. Emission floor applies to
        the estimated trade notional — dust quantity moves are noise."""
        now = self._time()
        key = (pos["venue"], pos["address"], pos["symbol"])
        prev = self._prev.get(key)
        self._prev[key] = pos
        cur_not = float(pos.get("notional_usd") or 0.0)
        if key not in self._first_seen:
            self._first_seen[key] = now
            pos["opened_at_confidence"] = "low"   # predates our watch
        if prev is None:
            # First sighting of a live position = aged bag → silent
            # (Hasbrouck). The OPENED event fires only when WE observe
            # 0 → nonzero across polls.
            return None
        prev_not = float(prev.get("notional_usd") or 0.0)
        prev_side = prev.get("side")
        side = pos["side"]
        d_not = cur_not - prev_not
        prev_qty = float(prev.get("size") or 0.0)
        cur_qty = float(pos.get("size") or 0.0)
        d_qty = cur_qty - prev_qty
        # Implied marks from notional/size (both adapters always carry them;
        # HL's mark_price field is None by construction).
        event_px = (cur_not / cur_qty) if cur_qty > 0 else (
            (prev_not / prev_qty) if prev_qty > 0 else 0.0)
        prev_px = (prev_not / prev_qty) if prev_qty > 0 else 0.0
        mtm_change = prev_qty * (event_px - prev_px) if prev_qty > 0 else 0.0
        est_trade_notional = abs(d_qty) * event_px
        if prev_not <= 0 and cur_not > 0:
            kind, direction = OPENED, side
            pos["opened_at_confidence"] = "high"  # we SAW the open
            self._first_seen[key] = now
        elif cur_not <= 0:
            kind, direction = CLOSED, prev_side
        elif prev_side != side:
            kind, direction = FLIPPED, side
            pos["opened_at_confidence"] = "high"
            self._first_seen[key] = now
        elif d_qty > 0:
            kind, direction = ADDED, side
        elif d_qty < 0:
            kind, direction = TRIMMED, side
        else:
            return None   # pure re-mark — zero behavior
        if kind in (ADDED, TRIMMED) and est_trade_notional < self._min_delta:
            return None
        acct = pos.get("account_value_usd")
        if acct:
            self._account_values[(pos["venue"], pos["address"])] = acct
        liq_dist = None
        if pos.get("liquidation_price") and pos.get("mark_price"):
            liq_dist = abs(pos["liquidation_price"] - pos["mark_price"]) \
                / pos["mark_price"]
        ev = {
            "venue": pos["venue"], "address": pos["address"],
            "symbol": pos["symbol"], "direction": direction, "kind": kind,
            "size": pos["size"], "prev_size": prev.get("size", 0.0),
            "quality": "direct", "ts": now,
            # enriched (WPP-only consumers; WhaleMirror ignores extras)
            "qty_delta": d_qty,
            "estimated_trade_notional": round(est_trade_notional, 2),
            "mtm_change_usd": round(mtm_change, 2),
            "notional_delta_usd": round(d_not, 2),
            "notional_usd": cur_not,
            "margin_used_usd": pos.get("margin_used_usd"),
            "leverage": pos.get("leverage"),
            "liq_distance_pct": liq_dist,
            "tier": whale_tier(acct),
            "opened_at_confidence": pos.get("opened_at_confidence", "low"),
            "features": whale_features(
                acct, kind, pos.get("opened_at_confidence", "low"),
                self._first_seen.get(key), now),
        }
        return ev

    def _vanish_check(self, venue: str, address: str, seen: set) -> list:
        """A position present last poll but absent now = CLOSED (the venue
        omits zero rows). Emits the close the row-diff can't see."""
        out = []
        now = self._time()
        for (v, a, sym), prev in list(self._prev.items()):
            if v != venue or a != address or (v, a, sym) in seen:
                continue
            if float(prev.get("notional_usd") or 0.0) <= 0:
                continue
            ev = {
                "venue": v, "address": a, "symbol": sym,
                "direction": prev.get("side"), "kind": CLOSED,
                "size": 0.0, "prev_size": prev.get("size", 0.0),
                "quality": "direct", "ts": now,
                "notional_delta_usd": -float(prev.get("notional_usd") or 0.0),
                "notional_usd": 0.0, "margin_used_usd": None,
                "leverage": None, "liq_distance_pct": None,
                "tier": whale_tier(self._account_values.get((v, a))),
                "opened_at_confidence": prev.get("opened_at_confidence", "low"),
                "features": None,
            }
            out.append(ev)
            self._prev[(v, a, sym)] = {**prev, "notional_usd": 0.0, "size": 0.0}
        return out

    async def poll_all(self) -> list:
        """One pass over registry × {aster, hyperliquid}. Returns flow
        events (WhaleMirror contract + enrichment). Journals snapshots."""
        flows = []
        for w in self._registry:
            addr = w["address"]
            for venue, fetch in (("aster", self._fetch_aster),
                                 ("hyperliquid", self._fetch_hl)):
                try:
                    positions = await fetch(addr)
                except Exception as e:
                    # The fetchers never raise by construction; this guard
                    # keeps the CONTRACT even if an adapter regresses.
                    logger.warning("whale_positions_poll_error",
                                   venue=venue, address=addr[:10],
                                   error=str(e)[:120])
                    continue
                seen = set()
                for pos in positions:
                    seen.add((pos["venue"], pos["address"], pos["symbol"]))
                    self._journal({"ts": self._time(), "kind": "snapshot",
                                   "label": w.get("label", ""), **pos})
                    ev = self._diff_one(pos)
                    if ev:
                        flows.append(ev)
                        self._journal({"ts": self._time(), "kind": "flow",
                                       "label": w.get("label", ""), **ev})
                for ev in self._vanish_check(venue, addr, seen):
                    flows.append(ev)
                    self._journal({"ts": self._time(), "kind": "flow",
                                   "label": w.get("label", ""), **ev})
        return flows

    def account_value(self, venue: str, address: str) -> float | None:
        return self._account_values.get((venue, address))


def whale_features(account_value_usd: float | None, kind: str,
                   opened_at_confidence: str, first_seen_ts: float | None,
                   now: float) -> dict:
    """Feature vector — NOT a confidence score (audit amendment #1: the
    calibrated predictive layer is learned from shadow data, not hand-
    weights). Components are raw, interpretable, and None where the plane
    cannot know yet (market_impact, historical_edge — they accrue in the
    shadow journal, not here)."""
    age_s = (now - first_seen_ts) if first_seen_ts else None
    return {
        "capital_usd": account_value_usd,
        "tier": whale_tier(account_value_usd),
        "event_kind": kind,
        "opened_at_confidence": opened_at_confidence,
        "position_age_s": round(age_s, 1) if age_s is not None else None,
        "historical_edge": None,     # learned — shadow journal
        "market_impact_ic": None,    # learned — shadow journal
    }
