"""
intelligence/bybit_lens.py — Bybit meta-cognition lens (2026-09-26, Governor
directive: "The campaign should have a meta cognition layer that checks ByBit
positions live for hedges on sodex — this is cheap price discovery").

╔══════════════════════════════════════════════════════════════════════════╗
║ READ-ONLY LAW — STRUCTURAL, NOT POLICY.                                  ║
║ The Bybit hedge EXECUTION sleeve was ELIMINATED 2026-09-21 by Governor   ║
║ order (flags off; resurrection requires an explicit order). This module  ║
║ IS A READ-ONLY DATA PLANE. It must be structurally impossible for it to  ║
║ place, modify, or cancel any order: it NEVER receives an executor, a     ║
║ client, or any order-capable object. It receives only INJECTED POLLER    ║
║ OUTPUTS — plain dicts/lists already fetched by the coordinator's loop.   ║
║ It owns no network handle, no signer, no callback into execution. Every  ║
║ decision leaves as an immutable verdict dataclass for the coordinator;   ║
║ nothing here auto-acts. Hedge execution on the SoDEX side (HedgeRegistry ║
║ venue="sodex" legs) is the coordinator's job, gated by his own kills.    ║
╚══════════════════════════════════════════════════════════════════════════╝

Doctrine (locked 2026-09-26): Bybit is the deep venue; ARIA trades SoDEX.
Watching Bybit positioning is cheap price discovery on two planes:
  (a) OPERATOR plane — the Governor trades Bybit manually. His live account
      positions reveal directional conviction: if he is long ETH on Bybit at
      size, a SoDEX campaign short on ETH is fighting the house → CONFLICT.
      Same side → CONFIRM (hedge unnecessary; conviction aligned).
  (b) PUBLIC plane — OI / funding / turnover is the crowd's positioning.
      Extreme funding against our side WITH an OI spike = squeeze risk →
      CROWD_EXTREME (hedge candidate). Silence is the honest null: no signal,
      no verdict.
Price discovery: the deep venue leads. A rich Bybit mark vs the SoDEX mark
means the SoDEX mark lags UP — basis notes (≥ threshold) feed the campaign's
mark-quality awareness, one deque per symbol, 1h bounded.

Department shape (docs/DEPARTMENT_TEMPLATE.md): zero-I/O brain. The
coordinator's supervised loop owns the read-only BybitClient construction
and calls the injected pollers; this module only parses and judges. Boot:
empty state = abstain.

Kill switch: config.bybit_lens_enabled=False (default) reproduces the
pre-module system exactly — hedge_verdict returns [] and price_discovery_note
returns None, always. Parsing methods remain pure and side-effect-free.

Config knobs (getattr, defaults):
  bybit_lens_enabled                False   master kill switch
  bybit_lens_min_operator_notional  50.0    $ notional for a CONFLICT to matter
  bybit_lens_funding_extreme        0.0005  |funding|/8h squeeze threshold (strict >)
  bybit_lens_oi_spike_pct           15.0    OI 24h change % squeeze threshold (strict >)
  bybit_lens_basis_note_pct         0.0015  basis note threshold (strict >, 0.15%)

Telemetry namespace (events ride the verdict dataclasses; the coordinator
logs them):
  bybit_lens_conflict        operator opposes a SoDEX campaign position
  bybit_lens_confirm         operator aligned with a SoDEX campaign position
  bybit_lens_crowd_extreme   public funding+OI squeeze risk against a position
  bybit_lens_basis_note      |bybit − sodex| basis beyond threshold
  bybit_lens_poll_error      splice-level poller failure (coordinator emits;
                             helper poll_error_event() formats the row)
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

# ── Telemetry event names (coordinator logs these verbatim) ──────────────────

EVENT_CONFLICT = "bybit_lens_conflict"
EVENT_CONFIRM = "bybit_lens_confirm"
EVENT_CROWD_EXTREME = "bybit_lens_crowd_extreme"
EVENT_BASIS_NOTE = "bybit_lens_basis_note"
EVENT_POLL_ERROR = "bybit_lens_poll_error"

BASIS_WINDOW_S = 3600.0   # bounded basis history per symbol

# ── Symbol mapping (BTCUSDT ↔ BTC-USD, 1000PEPEUSDT ↔ 1000PEPE-USD) ─────────


def to_canonical(symbol: str) -> str:
    """Bybit wire symbol → ARIA canonical. Idempotent: canonical passes through."""
    s = str(symbol or "").strip().upper()
    if not s:
        return ""
    if s.endswith("-USD"):
        return s
    for suffix in ("USDT", "USDC", "PERP"):
        if s.endswith(suffix) and len(s) > len(suffix):
            return s[: -len(suffix)] + "-USD"
    return s + "-USD" if s else ""


def to_bybit(symbol: str) -> str:
    """ARIA canonical → Bybit wire symbol. Idempotent: wire passes through."""
    s = str(symbol or "").strip().upper()
    if not s:
        return ""
    if s.endswith("-USD"):
        return s[: -len("-USD")] + "USDT"
    return s


def _side_norm(raw) -> Optional[str]:
    """Buy/Sell/long/short (any case) → 'long' | 'short'. None = unparseable."""
    s = str(raw or "").strip().lower()
    if s in ("buy", "long"):
        return "long"
    if s in ("sell", "short"):
        return "short"
    return None


def _f(raw) -> Optional[float]:
    """Lenient float parse; None on any failure (fail-closed per field)."""
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v


# ── Data shapes ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PositionView:
    """One normalized Bybit account position (operator plane)."""
    symbol: str          # canonical "BTC-USD"
    bybit_symbol: str    # "BTCUSDT"
    side: str            # "long" | "short"
    qty: float
    entry: float
    mark: float
    upnl: float
    leverage: float
    notional: float      # qty × (mark if known else entry)


@dataclass(frozen=True)
class AccountSnapshot:
    """The operator's Bybit account at one poll. Empty positions = abstain."""
    ts: float
    equity: float
    margin_used: float
    positions: Tuple[PositionView, ...]
    skipped_rows: int = 0

    def position_for(self, symbol: str) -> Optional[PositionView]:
        canon = to_canonical(symbol)
        for p in self.positions:
            if p.symbol == canon:
                return p
        return None


@dataclass(frozen=True)
class SymbolPlane:
    """Public crowd positioning for one symbol."""
    symbol: str                       # canonical
    open_interest: Optional[float]
    funding_rate: Optional[float]
    turnover_24h: Optional[float]
    price_24h_change_pct: Optional[float]
    oi_24h_change_pct: Optional[float]
    mark_price: Optional[float]


@dataclass(frozen=True)
class PublicPlane:
    """The public Bybit plane at one poll."""
    ts: float
    planes: Dict[str, SymbolPlane] = field(default_factory=dict)

    def plane_for(self, symbol: str) -> Optional[SymbolPlane]:
        return self.planes.get(to_canonical(symbol))


@dataclass(frozen=True)
class LensVerdict:
    """One hedge-relevant verdict for one SoDEX-side position. The
    coordinator consumes; nothing here acts."""
    kind: str            # "conflict" | "confirm" | "crowd_extreme"
    symbol: str          # canonical SoDEX symbol
    side: str            # the SoDEX position's side ("long" | "short")
    event: str           # telemetry event name
    payload: dict
    ts: float


@dataclass(frozen=True)
class BasisNote:
    """Deep-venue-vs-SoDEX basis beyond threshold. direction: which venue is
    rich. A rich Bybit = the deep venue leads and the SoDEX mark lags UP."""
    symbol: str
    direction: str       # "bybit_rich" | "sodex_rich"
    magnitude_bps: float
    bybit_mark: float
    sodex_mark: float
    event: str
    ts: float


def poll_error_event(where: str, error: str, now_ts: float) -> dict:
    """Formats the splice-level poll-failure telemetry row. The lens owns no
    I/O, so poll failures happen in the coordinator's loop — it emits this."""
    return {"event": EVENT_POLL_ERROR, "where": str(where),
            "error": str(error)[:200], "ts": float(now_ts)}


# ── The lens ─────────────────────────────────────────────────────────────────


class BybitLens:
    """Zero-I/O meta-cognition brain. Never receives an executor, a client,
    or any order-capable object — only poller OUTPUTS (dicts/lists). Bounded
    state: last account snapshot, last public plane, 1h of signed basis
    observations per symbol. Boot: empty = abstain."""

    def __init__(self) -> None:
        self.last_account: Optional[AccountSnapshot] = None
        self.last_public: Optional[PublicPlane] = None
        self._basis: Dict[str, Deque[Tuple[float, float]]] = {}

    # ── Operator plane ───────────────────────────────────────────────────

    def poll_account(self, raw_positions: List[dict], raw_balance: dict,
                     now_ts) -> AccountSnapshot:
        """Parse Bybit V5 position rows into PositionViews. Pure parse,
        fail-closed: one malformed row is skipped and counted, the rest
        parse. Zero-size rows skipped. raw_balance mirrors the V5
        wallet-balance shape (flat account dict, or {"list": [account]})."""
        views: List[PositionView] = []
        skipped = 0
        for row in raw_positions or []:
            try:
                if not isinstance(row, dict):
                    skipped += 1
                    continue
                raw_sym = row.get("symbol") or row.get("coin")
                canon = to_canonical(raw_sym)
                if not canon:
                    skipped += 1
                    continue
                qty = _f(row.get("size", row.get("qty")))
                if qty is None:
                    skipped += 1
                    continue
                qty = abs(qty)
                if qty <= 0:
                    continue  # designed skip, not malformed
                side = _side_norm(row.get("side"))
                if side is None:
                    skipped += 1
                    continue
                entry = _f(row.get("avgPrice", row.get("entry"))) or 0.0
                mark = _f(row.get("markPrice")) or 0.0
                upnl = _f(row.get("unrealisedPnl", row.get("upnl"))) or 0.0
                lev = _f(row.get("leverage")) or 1.0
                ref = mark if mark > 0 else entry
                views.append(PositionView(
                    symbol=canon, bybit_symbol=to_bybit(canon), side=side,
                    qty=qty, entry=entry, mark=mark, upnl=upnl,
                    leverage=lev, notional=qty * ref,
                ))
            except Exception:
                skipped += 1  # one bad row never poisons the rest
        equity, margin_used = self._parse_balance(raw_balance)
        snap = AccountSnapshot(ts=float(now_ts or 0), equity=equity,
                               margin_used=margin_used,
                               positions=tuple(views), skipped_rows=skipped)
        self.last_account = snap
        return snap

    @staticmethod
    def _parse_balance(raw_balance) -> Tuple[float, float]:
        acct: dict = {}
        if isinstance(raw_balance, dict):
            if isinstance(raw_balance.get("list"), list) and raw_balance["list"]:
                first = raw_balance["list"][0]
                if isinstance(first, dict):
                    acct = first
            else:
                acct = raw_balance
        equity = _f(acct.get("totalEquity", acct.get("equity"))) or 0.0
        margin = _f(acct.get("totalInitialMargin",
                             acct.get("totalUsedMargin",
                                      acct.get("marginUsed")))) or 0.0
        return equity, margin

    # ── Public plane ─────────────────────────────────────────────────────

    def poll_public(self, symbol_tickers: Dict[str, dict],
                    now_ts) -> PublicPlane:
        """Parse injected ticker dicts (raw Bybit V5 ticker keys OR the
        data/bybit_feed.py normalized store shape) into SymbolPlanes.
        Keys may be wire ("BTCUSDT") or canonical ("BTC-USD") symbols."""
        planes: Dict[str, SymbolPlane] = {}
        for raw_sym, tick in (symbol_tickers or {}).items():
            try:
                canon = to_canonical(raw_sym)
                if not canon or not isinstance(tick, dict):
                    continue
                oi = _f(tick.get("openInterest", tick.get("open_interest")))
                fr = _f(tick.get("fundingRate", tick.get("funding_rate")))
                t24 = _f(tick.get("turnover24h", tick.get("turnover_24h")))
                p24 = _f(tick.get("price24hPcnt", tick.get("price_24h_change_pct")))
                if p24 is not None and abs(p24) < 1.0 and "price24hPcnt" in tick:
                    p24 = p24 * 100.0  # V5 gives a fraction; normalize to pct
                mark = _f(tick.get("markPrice", tick.get("mark_price")))
                oi_chg = _f(tick.get("oi_24h_change_pct",
                                     tick.get("oi24hChangePct",
                                              tick.get("openInterest24hChangePct"))))
                if oi_chg is None:
                    prev_oi = _f(tick.get("prev_open_interest"))
                    if prev_oi and prev_oi > 0 and oi is not None:
                        oi_chg = (oi - prev_oi) / prev_oi * 100.0
                planes[canon] = SymbolPlane(
                    symbol=canon, open_interest=oi, funding_rate=fr,
                    turnover_24h=t24, price_24h_change_pct=p24,
                    oi_24h_change_pct=oi_chg, mark_price=mark,
                )
            except Exception:
                continue
        plane = PublicPlane(ts=float(now_ts or 0), planes=planes)
        self.last_public = plane
        return plane

    # ── Verdicts ─────────────────────────────────────────────────────────

    def hedge_verdict(self, cfg, *, account: AccountSnapshot,
                      sodex_book: List[dict], public: PublicPlane,
                      now_ts) -> List[LensVerdict]:
        """For each SoDEX-side open campaign position, judge the operator and
        crowd planes. Verdicts only; silence is the honest null; the
        coordinator considers a SoDEX hedge or exit — this NEVER auto-acts.
        Kill switch: bybit_lens_enabled=False → []."""
        if not getattr(cfg, "bybit_lens_enabled", False):
            return []
        min_notional = float(getattr(cfg, "bybit_lens_min_operator_notional", 50.0))
        fr_thr = float(getattr(cfg, "bybit_lens_funding_extreme", 0.0005))
        oi_thr = float(getattr(cfg, "bybit_lens_oi_spike_pct", 15.0))
        ts = float(now_ts or 0)
        out: List[LensVerdict] = []
        for row in sodex_book or []:
            try:
                if not isinstance(row, dict):
                    continue
                canon = to_canonical(row.get("symbol"))
                side = _side_norm(row.get("side"))
                if not canon or side is None:
                    continue
                # ── Operator plane: conflict / confirm ──
                op = account.position_for(canon) if account else None
                if op is not None:
                    if op.side != side:
                        if op.notional >= min_notional:
                            out.append(LensVerdict(
                                kind="conflict", symbol=canon, side=side,
                                event=EVENT_CONFLICT, ts=ts,
                                payload={
                                    "bybit_side": op.side,
                                    "bybit_notional": op.notional,
                                    "note": "operator holds the opposite side "
                                            "at size — fighting the house; "
                                            "consider SoDEX hedge or exit; "
                                            "never auto-act",
                                }))
                    else:
                        out.append(LensVerdict(
                            kind="confirm", symbol=canon, side=side,
                            event=EVENT_CONFIRM, ts=ts,
                            payload={
                                "bybit_side": op.side,
                                "bybit_notional": op.notional,
                                "note": "operator aligned — conviction "
                                        "alignment; hedge unnecessary",
                            }))
                # ── Public plane: crowd squeeze against our side ──
                plane = public.plane_for(canon) if public else None
                if plane is not None and plane.funding_rate is not None \
                        and plane.oi_24h_change_pct is not None:
                    fr = plane.funding_rate
                    adverse = (side == "short" and fr > fr_thr) or \
                              (side == "long" and fr < -fr_thr)
                    if adverse and plane.oi_24h_change_pct > oi_thr:
                        out.append(LensVerdict(
                            kind="crowd_extreme", symbol=canon, side=side,
                            event=EVENT_CROWD_EXTREME, ts=ts,
                            payload={
                                "funding_rate": fr,
                                "oi_change_pct": plane.oi_24h_change_pct,
                                "note": "crowd funding extreme against the "
                                        "position with OI spiking — squeeze "
                                        "risk; hedge candidate",
                            }))
            except Exception:
                continue  # one bad book row never kills the sweep
        return out

    # ── Price discovery ──────────────────────────────────────────────────

    def price_discovery_note(self, cfg, *, symbol: str, bybit_mark: float,
                             sodex_mark: float, now_ts) -> Optional[BasisNote]:
        """|bybit − sodex| / mid beyond the basis threshold (strict >) →
        BasisNote naming the rich venue. The deep venue leads: a rich Bybit
        = the SoDEX mark lags UP. Records a bounded 1h observation per
        symbol on every priced poll. Kill switch: disabled → None."""
        if not getattr(cfg, "bybit_lens_enabled", False):
            return None
        try:
            bb = float(bybit_mark)
            ss = float(sodex_mark)
        except (TypeError, ValueError):
            return None
        if bb <= 0 or ss <= 0:
            return None
        canon = to_canonical(symbol)
        ts = float(now_ts or 0)
        mid = (bb + ss) / 2.0
        signed = (bb - ss) / mid
        self._record_basis(canon, ts, signed)
        thr = float(getattr(cfg, "bybit_lens_basis_note_pct", 0.0015))
        if abs(signed) > thr:
            return BasisNote(
                symbol=canon,
                direction="bybit_rich" if bb > ss else "sodex_rich",
                magnitude_bps=abs(signed) * 1e4,
                bybit_mark=bb, sodex_mark=ss,
                event=EVENT_BASIS_NOTE, ts=ts,
            )
        return None

    # ── Bounded basis history ────────────────────────────────────────────

    def _record_basis(self, canon: str, ts: float, signed_pct: float) -> None:
        dq = self._basis.get(canon)
        if dq is None:
            dq = deque(maxlen=240)   # 240 obs @ 15s cadence covers the hour
            self._basis[canon] = dq
        dq.append((ts, signed_pct))
        cutoff = ts - BASIS_WINDOW_S
        while dq and dq[0][0] < cutoff:
            dq.popleft()

    def basis_window(self, symbol: str, now_ts) -> Tuple[Tuple[float, float], ...]:
        """Signed basis observations within the last hour. Empty = abstain
        (the boot state)."""
        dq = self._basis.get(to_canonical(symbol))
        if not dq:
            return ()
        cutoff = float(now_ts or 0) - BASIS_WINDOW_S
        return tuple((t, v) for t, v in dq if t >= cutoff)
