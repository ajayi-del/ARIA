import time
import asyncio
import structlog
from collections import deque
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from datetime import datetime, timedelta, timezone
from typing import Callable
from memory.performance import PerformanceTracker
from memory.trade_journal import TradeJournal
import traceback

from core.config import Settings
from core.market_engine import MarketEngine
from data.sodex_feed import SoDEXFeed
from memory.performance import SessionDrawdownTracker

logger = structlog.get_logger(__name__)


class TerminalDisplay:
    def __init__(
        self,
        config: Settings,
        orderbook_stores: dict,
        mark_price_stores: dict,
        candle_buffers: dict,
        trade_flow_stores: dict,
        health_check: Callable[[], dict],
        market_engine: MarketEngine = None,
        calendar_engine=None,  # CalendarEngine
        journal: TradeJournal = None,
        perf: PerformanceTracker = None,
        system_state=None,  # SystemStateManager
        position_manager=None,
        interpreter=None,  # IntelligenceInterpreter
        ws_manager=None,
        dd_tracker: SessionDrawdownTracker = None,
        cascade_tracker=None,    # CascadeTracker
        adaptive_calibrator=None,  # AdaptiveCalibrator
        bybit_ticker_stores: dict = None,   # Bybit OI + funding (real market reference)
    ):
        self.config = config
        self.orderbook_stores = orderbook_stores
        self.mark_price_stores = mark_price_stores
        self.candle_buffers = candle_buffers
        self.trade_flow_stores = trade_flow_stores
        self.health_check = health_check
        self.market_engine = market_engine
        self.calendar_engine = calendar_engine
        self._journal = journal
        self._perf = perf
        self.system_state = system_state
        self._position_manager = position_manager
        self.interpreter = interpreter
        self._ws_manager = ws_manager
        self._dd_tracker = dd_tracker
        self._cascade_tracker = cascade_tracker
        self._adaptive_calibrator = adaptive_calibrator
        self._bybit_ticker_stores = bybit_ticker_stores or {}
        # Per-asset previous last price for activity blink detection
        # Using last_price (Bybit trade) not mark_price (SoDEX) — SoDEX marks are
        # infrequent on thin books, Bybit trades stream every second.
        self._prev_mark: dict = {}    # legacy name kept for compat
        self._prev_last: dict = {}    # last_price comparison for activity blink

        # Phase 4.5 Data
        self._funding_snapshots = {}
        self._open_arbs = []

        # v1.4 On-chain + True Arb data
        self._vc_status: dict = {}
        self._true_arb_positions: list = []

        # v1.3 Cached Async Data
        self._calendar_states = {}
        self._upcoming_events = []

        self._equity_history = []  # List of (timestamp, perps balance)
        self._spot_balance: float = 0.0
        self._fee_data: dict = {}
        self.start_time = time.time()
        self._task = None

        # Render-time performance cache — expensive ops throttled to avoid
        # blocking the event loop on every 500ms display tick.
        self._perf_cache: object = None          # last PerformanceStats result
        self._perf_cache_ts: float = 0.0         # epoch time of last compute()
        self._perf_cache_ttl: float = 5.0        # recompute every 5 s

        # Trade candidate log — gates-passed submissions and SoDEX outcomes
        self._trade_candidate_log: deque = deque(maxlen=8)

        # v2.0 MarketContext — updated each signal tick from main.py
        self._market_context = None

        # ── Display cache ─────────────────────────────────────────────────────
        # All panel builders read ONLY from here. Populated by _populate_cache()
        # before each render. O(1) render, zero external calls in panel builders.
        self._display_cache: dict = {
            "assets": {},           # symbol → {last_price, mark_price, divergence_pct, imbalance, ob_age, buy_delta, warmup_count, warmup_target, warmup_phase}
            "signals": {},          # symbol → {weighted_score, direction, atr, atr_ratio, coherence_mult, freshness_mult, calendar_mult, sweep, vpin}
            "flow": {},             # symbol → {buy_vol, sell_vol, delta, aggressor_ratio}
            "funding": {},          # funding snapshots dict
            "cascade": None,        # cascade_tracker.get_summary()
            "market_mode": "normal",
            "positions": [],        # open directional positions
            "arb_legs": [],         # true arb positions
            "calendar": {},         # calendar states per asset
            "calendar_events": [],  # upcoming events list
            "session": {},          # perf stats + balance + drawdown
            "equity": [],           # equity history
            "context": None,        # MarketContext
            "system_state": {},     # global phase + counts
            "calibrator": {},       # calibration summary
            "chain": {},            # vc_status
            "mag7": {},             # mag7 state
            "macro": {},            # macro intelligence state
            "fee": {},              # fee engine summary
            "stuck_positions": {},  # symbol → {"count": int, "last_err": str} for unclosed orders
            "last_updated_ms": 0,
        }

    # ── Public update methods ─────────────────────────────────────────────────

    def update_cache(self, key: str, value) -> None:
        """Direct cache key update — used by intelligence loop in main.py."""
        self._display_cache[key] = value

    def update_stuck_positions(self, stuck: dict) -> None:
        """
        Called by stop_guardian in main.py after every failure handling cycle.
        stuck = _stop_close_fails = {symbol: {"count": int, "backoff_until": float, "last_err": str}}
        Header shows ⚠ UNCLOSED alert when any entries remain.
        """
        self._display_cache["stuck_positions"] = dict(stuck)

    def push_trade_candidate(
        self,
        *,
        symbol: str,
        direction: str,
        score: float,
        entry: float,
        stop: float,
        tp1: float,
        size: float,
        leverage: int,
        rr: float,
        status: str,            # "SUBMITTED" | "PLACED" | "REJECTED"
        error: str = None,
    ) -> None:
        """Record a gate-passed trade candidate or its SoDEX outcome."""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        if (self._trade_candidate_log
                and self._trade_candidate_log[-1]["sym"] == symbol
                and self._trade_candidate_log[-1]["status"] == "SUBMITTED"
                and status in ("PLACED", "REJECTED")):
            self._trade_candidate_log[-1]["status"] = status
            self._trade_candidate_log[-1]["error"] = error
            self._trade_candidate_log[-1]["ts"] = ts
        else:
            self._trade_candidate_log.append({
                "ts": ts, "sym": symbol, "dir": direction,
                "score": score, "entry": entry, "stop": stop,
                "tp1": tp1, "size": size, "lev": leverage,
                "rr": rr, "status": status, "error": error,
            })

    def update_equity(self, balance: float) -> None:
        self._equity_history.append((int(time.time() * 1000), balance))
        if len(self._equity_history) > 200:
            self._equity_history = self._equity_history[-200:]

    def update_spot_balance(self, balance: float) -> None:
        self._spot_balance = float(balance) if balance is not None else 0.0

    def update_fee_data(self, fee_summary: dict) -> None:
        self._fee_data = fee_summary

    def update_funding(self, snapshots: dict) -> None:
        self._funding_snapshots = snapshots

    def update_arbs(self, arbs: list) -> None:
        self._open_arbs = arbs

    def update_vc_status(self, status: dict) -> None:
        self._vc_status = status

    def update_true_arb_positions(self, positions: list) -> None:
        self._true_arb_positions = positions

    def update_market_context(self, ctx) -> None:
        self._market_context = ctx

    # ── Cache population ──────────────────────────────────────────────────────

    def _populate_cache(self) -> None:
        """
        Collect all display data into _display_cache in one pass.
        Called once per render cycle BEFORE generate_layout().
        No external store calls are allowed inside panel builders.
        """
        # ── Per-asset: price, OB, flow, signals ───────────────────────────────
        assets_cache: dict = {}
        flow_cache: dict = {}
        signals_cache: dict = {}

        for asset in self.config.assets:
            a: dict = {
                "last_price": 0.0,
                "mark_price": 0.0,
                "divergence_pct": 0.0,
                "imbalance": 0.0,
                "ob_age": 999999,
                "buy_delta": 0.0,
                "warmup_count": 0,
                "warmup_target": 50,
                "warmup_phase": "WARMING_UP",
            }

            if asset in self.trade_flow_stores:
                store = self.trade_flow_stores[asset]
                lp = store.latest_price()
                if lp is not None:
                    a["last_price"] = lp
                a["buy_delta"] = store.delta()

            if asset in self.mark_price_stores:
                mp_data = self.mark_price_stores[asset].get()
                if mp_data:
                    a["mark_price"] = mp_data.get("mark_price", 0.0)
                    if mp_data.get("last_price", 0) != 0:
                        a["last_price"] = mp_data["last_price"]
                    a["divergence_pct"] = mp_data.get("divergence_pct", 0.0)

            if asset in self.orderbook_stores:
                a["imbalance"] = self.orderbook_stores[asset].imbalance()
                a["ob_age"] = self.orderbook_stores[asset].age_ms()

            # Bybit intelligence — funding rate, OI, 24h change for display
            _bt = self._bybit_ticker_stores.get(asset, {})
            a["bybit_funding"] = _bt.get("funding_rate", 0.0)
            a["bybit_oi"] = _bt.get("open_interest", 0.0)
            a["bybit_oi_prev"] = _bt.get("prev_open_interest", 0.0)
            # Price activity: compare Bybit last_price between renders → blink/color.
            # Using last_price (Bybit L2 trade) not mark_price (SoDEX M) because SoDEX
            # marks update infrequently on thin books — trade prices stream every second.
            _cur_last = a.get("last_price", 0.0)
            _prev_last = self._prev_last.get(asset, _cur_last)
            a["price_up"] = _cur_last > _prev_last and _cur_last > 0
            a["price_down"] = _cur_last < _prev_last and _cur_last > 0
            if _cur_last > 0:
                self._prev_last[asset] = _cur_last
            # Keep mark comparison for divergence (SoDEX vs Bybit spread)
            _cur_mark = a.get("mark_price", 0.0)
            if _cur_mark > 0:
                self._prev_mark[asset] = _cur_mark

            if self.system_state:
                try:
                    status = self.system_state.get_warmup_status().get(asset, {})
                    a["warmup_count"] = status.get("count", 0)
                    a["warmup_target"] = status.get("target", 50)
                    from core.system_state import SystemPhase
                    phase_enum = self.system_state._symbol_phase.get(asset, SystemPhase.WARMING_UP)
                    a["warmup_phase"] = phase_enum.name
                except Exception:
                    pass

            assets_cache[asset] = a

            # Flow volumes
            f: dict = {"buy_vol": 0.0, "sell_vol": 0.0, "delta": 0.0, "aggressor_ratio": 0.0}
            if asset in self.trade_flow_stores:
                store = self.trade_flow_stores[asset]
                f["buy_vol"] = store.buy_volume()
                f["sell_vol"] = store.sell_volume()
                f["delta"] = store.delta()
                f["aggressor_ratio"] = store.aggressor_ratio()
            flow_cache[asset] = f

            # Signal state
            state = None
            if self.interpreter:
                state = self.interpreter.get_market_state(asset)
            elif self.market_engine:
                state = self.market_engine.get_market_state(asset)

            signals_cache[asset] = {
                "weighted_score": getattr(state, "weighted_score", 0.0) if state else 0.0,
                "direction": getattr(state, "trade_direction", "none") if state else "none",
                "atr": getattr(state, "atr", 0.0) if state else 0.0,
                "mark_price": getattr(state, "mark_price", 0.0) if state else 0.0,
                "atr_ratio": getattr(state, "atr_vs_baseline", 1.0) if state else 1.0,
                "coherence_mult": getattr(state, "coherence_mult", 0.0) if state else 0.0,
                "freshness_mult": getattr(state, "freshness_mult", 1.0) if state else 1.0,
                "calendar_mult": getattr(state, "calendar_mult", 1.0) if state else 1.0,
                "sweep": getattr(state, "sweep", "none") if state else "none",
                "vpin": getattr(state, "vpin", 0.0) if state else 0.0,
            }

        # ── Positions ─────────────────────────────────────────────────────────
        positions = []
        if self._position_manager:
            try:
                positions = list(self._position_manager.get_all())
            except Exception:
                pass

        # ── Session / performance stats ────────────────────────────────────────
        balance = self._equity_history[-1][1] if self._equity_history else 0.0
        deployed = sum(getattr(p, "initial_margin", 0.0) for p in positions)
        session: dict = {
            "win_rate": 0.0, "total_pnl": 0.0, "sqn": 0.0, "closed": 0,
            "balance": balance,
            "spot_balance": self._spot_balance or 0.0,
            "deployed": deployed,
            "dd_pct": 0.0, "dd_regime": "normal", "dd_streak": 0,
            "uptime_s": int(time.time() - self.start_time),
        }
        if self._perf and self._journal:
            try:
                # Recompute at most every 5 s — journal.compute() iterates all
                # closed entries and is expensive at 4 Hz on 60+ trade journals.
                _now_pc = time.time()
                if self._perf_cache is None or (_now_pc - self._perf_cache_ts) >= self._perf_cache_ttl:
                    self._perf_cache = self._perf.compute(self._journal)
                    self._perf_cache_ts = _now_pc
                stats = self._perf_cache
                if stats is not None:
                    session["win_rate"] = stats.win_rate * 100
                    session["total_pnl"] = stats.total_pnl_usd
                    session["sqn"] = stats.sqn
                    session["closed"] = stats.closed_trades
            except Exception:
                pass
        if self._dd_tracker:
            try:
                session["dd_pct"] = self._dd_tracker.session_drawdown_pct
                session["dd_regime"] = self._dd_tracker.drawdown_regime
                session["dd_streak"] = self._dd_tracker.consecutive_losses
            except Exception:
                pass

        # ── System state ──────────────────────────────────────────────────────
        system_data: dict = {"global_phase": "OFFLINE", "active_signals": 0, "live_trades": 0}
        if self.system_state:
            try:
                system_data["global_phase"] = self.system_state.get_global_phase().value.upper()
            except Exception:
                pass
        # Count only signals that are both directional AND above the coherence floor.
        # A raw direction ≠ "none" is insufficient — low-conviction signals (score < 3.0)
        # pollute the header count with noise.  3.0 matches the risk-engine minimum for
        # most regime states.  This keeps the header in sync with the signal engine panel.
        _sig_floor = max(getattr(self.config, "live_min_coherence", 1.0), 3.0)
        system_data["active_signals"] = sum(
            1 for s in signals_cache.values()
            if s["direction"] != "none"
               and s["weighted_score"] >= _sig_floor
               and s.get("atr", 0.0) > 0
               and s.get("mark_price", 0.0) > 0
        )
        system_data["live_trades"] = len(positions)

        # ── Calibrator ────────────────────────────────────────────────────────
        calibrator_data: dict = {}
        if self._adaptive_calibrator is not None:
            try:
                calibrator_data = self._adaptive_calibrator.get_calibration_summary()
            except Exception:
                pass

        # ── Cascade ───────────────────────────────────────────────────────────
        cascade_data = None
        if self._cascade_tracker is not None:
            try:
                cascade_data = self._cascade_tracker.get_summary()
            except Exception:
                pass

        # ── Chain / macro / mag7 ─────────────────────────────────────────────
        chain_data = dict(self._vc_status) if self._vc_status else {}

        mag7_data: dict = {}
        if self.interpreter and hasattr(self.interpreter, "_mag7"):
            try:
                _m = self.interpreter._mag7
                mag7_data = {
                    "stale": _m.is_stale(),
                    "direction": _m.direction,
                    "strength": _m.strength,
                }
            except Exception:
                pass

        macro_data: dict = {}
        if self.interpreter and hasattr(self.interpreter, "_macro"):
            try:
                _ms = self.interpreter._macro.state
                macro_data = {
                    "macro_direction": _ms.macro_direction,
                    "assets_confirming": _ms.assets_confirming,
                    "assets_total_active": _ms.assets_total_active,
                    "capitulation_detected": _ms.capitulation_detected,
                    "post_event_active": _ms.post_event_active,
                    "funding_regime": _ms.funding_regime,
                    "xaut_direction": _ms.xaut_direction,
                    "xaut_confirms_regime": _ms.xaut_confirms_regime,
                    "xaut_macro_mult": _ms.xaut_macro_mult,
                    "volume_quality_mult": _ms.volume_quality_mult,
                }
            except Exception:
                pass

        # ── Commit to cache atomically ────────────────────────────────────────
        self._display_cache.update({
            "assets": assets_cache,
            "signals": signals_cache,
            "flow": flow_cache,
            "funding": dict(self._funding_snapshots),
            "cascade": cascade_data,
            "market_mode": self._market_context.market_mode if self._market_context else "normal",
            "positions": positions,
            "arb_legs": list(self._true_arb_positions or []),
            "calendar": dict(self._calendar_states),
            "calendar_events": list(self._upcoming_events),
            "session": session,
            "equity": list(self._equity_history),
            "context": self._market_context,
            "system_state": system_data,
            "calibrator": calibrator_data,
            "chain": chain_data,
            "mag7": mag7_data,
            "macro": macro_data,
            "fee": dict(self._fee_data) if self._fee_data else {},
            "last_updated_ms": int(time.monotonic() * 1000),
        })

    # ── Safety wrapper ─────────────────────────────────────────────────────────

    def _safe_panel(self, builder_method, title: str) -> Panel:
        """Prevent panel builder exceptions from crashing the UI."""
        try:
            return builder_method()
        except Exception as e:
            structlog.get_logger(__name__).error(f"panel_build_error_{title}", error=str(e))
            return Panel(
                f"[red]Error: {str(e)}[/red]\n{traceback.format_exc() if self.config.debug else ''}",
                title=f"[red]{title}[/red]",
                border_style="red"
            )

    # ── Render loop ────────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Consolidated rendering loop with screen takeover."""
        if self.calendar_engine:
            try:
                self._calendar_states = await self.calendar_engine.get_states_all(self.config.assets)
                self._upcoming_events = await self.calendar_engine.event_store.get_upcoming(hours_ahead=72)
            except Exception:
                pass

        # Purge any stdout that leaked before Rich takes over (pre-init logs,
        # shell echos) — ANSI clear + cursor-home so nothing bleeds through
        # the alternate screen buffer when screen=True switches to it.
        import sys as _sys_live
        _sys_live.stdout.write("\033[2J\033[H")
        _sys_live.stdout.flush()

        # 2 Hz render — trading terminal needs responsiveness, not animation.
        # At 4 Hz the display consumed ~76 % of its 250 ms budget just rendering;
        # 2 Hz (500 ms) halves that load and keeps the event loop free for execution.
        with Live(self.generate_layout(), refresh_per_second=2, screen=True) as live:
            _cal_tick = 0  # calendar refresh counter
            while True:
                try:
                    # Calendar refresh every 10 ticks = every 5 s.
                    # get_states_all() hits SQLite; calling it every 250 ms was
                    # a significant hidden bottleneck on journals with 60+ entries.
                    _cal_tick += 1
                    if self.calendar_engine and _cal_tick >= 10:
                        _cal_tick = 0
                        self._calendar_states = await self.calendar_engine.get_states_all(self.config.assets)
                        if int(time.time()) % 60 == 0:
                            self._upcoming_events = await self.calendar_engine.event_store.get_upcoming(hours_ahead=72)

                    # Populate cache — one data-collection pass before rendering
                    self._populate_cache()

                    # Render timing — skip frame if layout takes > 100ms to avoid blocking event loop
                    try:
                        async with asyncio.timeout(0.1):
                            _t_render = time.monotonic()
                            layout = self.generate_layout()
                            _render_ms = (time.monotonic() - _t_render) * 1000
                            if _render_ms > 250:
                                logger.warning("slow_render", elapsed_ms=round(_render_ms, 1))
                            live.update(layout)
                    except asyncio.TimeoutError:
                        logger.debug("render_skipped_timeout")
                except Exception as _e:
                    import traceback as _tb
                    logger.error("display_render_error",
                                 error=str(_e),
                                 traceback=_tb.format_exc().strip())
                await asyncio.sleep(0.5)

    def generate_layout(self) -> Layout:
        layout = Layout()
        layout.split(
            Layout(name="header", size=3),
            Layout(name="body")
        )
        layout["body"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="center", ratio=1),
            Layout(name="right", ratio=1)
        )

        layout["header"].update(self._safe_panel(self._build_header, "Header"))

        # Left column: market scanner (top) + trade builder (bottom).
        # Trade builder sits directly under the coin table so open candidates
        # are visible immediately next to the scanner that generated them.
        layout["left"].split(
            Layout(name="market_scanner", ratio=3),
            Layout(name="trade_builder", ratio=1),
        )
        layout["left"]["market_scanner"].update(self._safe_panel(self._build_assets_panel, "Assets"))
        layout["left"]["trade_builder"].update(self._safe_panel(self._build_trade_candidates_panel, "Trade Candidates"))

        layout["center"].split(
            Layout(name="market_mode", size=7),
            Layout(name="intelligence", ratio=2),
            Layout(name="open_positions", ratio=1),
            Layout(name="calendar_status", ratio=1),
            Layout(name="funding_radar", ratio=1),
        )
        layout["center"]["market_mode"].update(self._safe_panel(self._build_context_panel, "Market News"))
        layout["center"]["intelligence"].update(self._safe_panel(self._build_intelligence_panel, "Intelligence"))
        layout["center"]["open_positions"].update(self._safe_panel(self._build_open_positions_panel, "Open Positions"))
        layout["center"]["calendar_status"].update(self._safe_panel(self._build_calendar_panel, "Calendar"))
        layout["center"]["funding_radar"].update(self._safe_panel(self._build_funding_radar, "Funding Radar"))

        layout["right"].split(
            Layout(name="trade_flow", ratio=2),
            Layout(name="chain_intelligence", size=7),
            Layout(name="true_arb_positions", ratio=1),
            Layout(name="allocation", size=5),
            Layout(name="fee_intelligence", size=6),
            Layout(name="equity_curve", ratio=1),
            Layout(name="stats_row", size=6)
        )
        layout["right"]["trade_flow"].update(self._safe_panel(self._build_trade_flow, "Trade Flow"))
        layout["right"]["chain_intelligence"].update(self._safe_panel(self._build_chain_intelligence_panel, "Chain Intelligence"))
        layout["right"]["true_arb_positions"].update(self._safe_panel(self._build_true_arb_panel, "True Arb Positions"))
        layout["right"]["allocation"].update(self._safe_panel(self._build_allocation_panel, "Allocation"))
        layout["right"]["fee_intelligence"].update(self._safe_panel(self._build_fee_intelligence_panel, "Fee Intelligence"))
        layout["right"]["equity_curve"].update(self._safe_panel(self._build_equity_curve, "Equity Curve"))
        layout["right"]["stats_row"].update(self._safe_panel(self._build_stats_panel, "Stats"))

        return layout

    # ── Panel builders — read ONLY from _display_cache ────────────────────────

    def _build_header(self) -> Panel:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        mode = self.config.mode.upper()

        if mode == "LIVE":
            mode_text = "[bold #ff4444 blink]⚡ LIVE[/]"
            border = "bold red"
        elif mode == "TESTNET":
            mode_text = "[bold #f5a623]◈ TESTNET[/]"
            border = "#f5a623"
        else:
            mode_text = "[#888888]◌ PAPER[/]"
            border = "#4a5a6a"

        sys = self._display_cache.get("system_state", {})
        global_phase = sys.get("global_phase", "OFFLINE")
        live_trades = sys.get("live_trades", 0)

        stuck = self._display_cache.get("stuck_positions", {})

        phase_color = "#00d084" if global_phase in ("TRADING", "READY") else "#ff4757"
        trade_color = "#f5a623 blink" if live_trades > 0 else "dim"

        if stuck:
            # Build compact alert: ⚠ UNCLOSED: ETH-USD(12) SOL-USD(3)
            _parts = []
            for _sym, _cb in stuck.items():
                _cnt = _cb.get("count", 1) if isinstance(_cb, dict) else 1
                _parts.append(f"{_sym}({_cnt})")
            _stuck_str = " ".join(_parts)
            _stuck_color = "bold #ff4444 blink" if any(
                (v.get("count", 1) if isinstance(v, dict) else 1) >= 5 for v in stuck.values()
            ) else "bold #f5a623 blink"
            order_segment = f"[{_stuck_color}]⚠ UNCLOSED: {_stuck_str}[/]"
        else:
            order_segment = "[dim]✓ NO UNCLOSED ORDERS[/]"

        header_text = Text.from_markup(
            f"[bold #00aaff]ARIA v1.3[/]  [{phase_color}]{global_phase}[/]  {mode_text}  "
            f"[dim]│[/]  {now}  [dim]│[/]  SODEX MAINNET  [dim]│[/]  "
            f"{order_segment}"
            f"[dim]  ●  [/][{trade_color}]{live_trades} LIVE TRADE{'S' if live_trades != 1 else ''}[/]  "
            f"[dim]│[/]  [dim]8-ASSET AUTONOMOUS PERPS[/]"
        )
        header_text.justify = "center"
        return Panel(header_text, style="#0d1014", border_style=border)

    def _build_context_panel(self) -> Panel:
        """
        Market News panel — most visually prominent element in the UI.
        Two display states:
          CASCADE: phase, cascade direction, notional, aftermath signals, weight overrides
          NORMAL:  regime, top flow biases, funding spread summary, next event
        """
        ctx = self._display_cache.get("context")
        if ctx is None:
            return Panel(
                "[dim]Awaiting market context...[/dim]",
                title="[dim]◈ MARKET NEWS[/dim]",
                border_style="dim"
            )

        mode = ctx.market_mode

        _MODE_STYLE = {
            "cascade_blocked":  ("bold #ff4444 blink", "⛔ CASCADE BLOCKED",  "red"),
            "cascade_momentum": ("bold #ff8c00 blink", "⚡ CASCADE MOMENTUM", "#ff8c00"),
            "cascade_primed":   ("bold #00d084",       "◈ CASCADE PRIMED",    "#00d084"),
            "calendar_caution": ("bold #f5a623",       "⚠ CALENDAR CAUTION", "#f5a623"),
            "defensive":        ("bold #ff6b6b",       "🛡 DEFENSIVE",        "#ff6b6b"),
            "normal":           ("bold #00aaff",       "● NORMAL",            "#00aaff"),
        }
        text_style, label, border = _MODE_STYLE.get(
            mode, ("bold #00aaff", "● NORMAL", "#00aaff")
        )

        t = Text()
        t.append(f" {label} ", style=text_style)
        t.append("  ")
        t.append(f"[{ctx.regime.upper()}]", style="dim")

        if mode in ("cascade_blocked", "cascade_momentum", "cascade_primed"):
            direction_char = "▼ BEAR" if ctx.cascade_direction == "bearish" else "▲ BULL"
            dir_color = "#ff4444" if ctx.cascade_direction == "bearish" else "#00d084"
            notional_str = (
                f"${ctx.cascade_notional / 1_000_000:.1f}M"
                if ctx.cascade_notional >= 1_000_000
                else f"${ctx.cascade_notional / 1_000:.0f}k"
                if ctx.cascade_notional >= 1_000
                else f"${ctx.cascade_notional:.0f}"
            )
            t.append(f"\n Cascade: ", style="dim")
            t.append(direction_char, style=dir_color)
            t.append(f"  Notional: {notional_str}", style="bold")
            t.append(f"  Type: {ctx.cascade_type.upper()}", style="dim")
            t.append(f"\n Aftermath signals: ", style="dim")
            t.append(f"{ctx.cascade_aftermath_count}/5", style=(
                "bold #00d084" if ctx.cascade_aftermath_count >= 3 else
                "bold #f5a623" if ctx.cascade_aftermath_count >= 1 else "dim"
            ))
            if ctx.signal_weights:
                weight_str = "  ".join(
                    f"{k[:4]}:{v:.1f}×" for k, v in sorted(ctx.signal_weights.items())
                    if v != 1.0
                )
                t.append(f"\n Weights: {weight_str}", style="#888888")
        else:
            conf_pct = f"{ctx.regime_confidence * 100:.0f}%"
            t.append(f"\n Regime: {ctx.regime.replace('_', ' ').upper()}  ", style="dim")
            t.append(f"conf {conf_pct}", style="dim")

            buy_syms = [s for s, b in ctx.flow_bias.items() if b == "buy"]
            sell_syms = [s for s, b in ctx.flow_bias.items() if b == "sell"]
            if buy_syms or sell_syms:
                flow_parts = []
                if buy_syms:
                    flow_parts.append(f"[#00d084]▲ {' '.join(s.replace('-USD', '') for s in buy_syms[:3])}[/]")
                if sell_syms:
                    flow_parts.append(f"[#ff4444]▼ {' '.join(s.replace('-USD', '') for s in sell_syms[:3])}[/]")
                t.append("\n Flow:  ", style="dim")
                t.append_text(Text.from_markup("  ".join(flow_parts)))

            if ctx.calendar_hours is not None:
                hrs = ctx.calendar_hours
                cal_color = "#ff4444" if hrs < 1 else "#f5a623" if hrs < 4 else "#888888"
                t.append(f"\n Event: ", style="dim")
                t.append(f"{hrs:.1f}h away  ({ctx.calendar_regime})", style=cal_color)
            else:
                t.append(f"\n Calendar: clear", style="dim #888888")

            # ── Time Regime Overlay ────────────────────────────────────────────
            _tr_phase = getattr(ctx, "time_regime_phase", "")
            _tr_notes = getattr(ctx, "time_regime_notes", "")
            if _tr_phase or _tr_notes:
                _tr_color = "#f5a623" if "event" in _tr_phase or "block" in _tr_phase else "#888888"
                t.append(f"\n Regime: ", style="dim")
                t.append(f"{_tr_phase}", style=_tr_color)
                if _tr_notes:
                    # Show first note segment only to keep display compact
                    _first_note = _tr_notes.split(" | ")[0]
                    t.append(f"  {_first_note}", style="dim #666666")

        return Panel(t, title=f"[bold {border}]◈ MARKET NEWS[/]", border_style=border, padding=(0, 1))

    def _build_assets_panel(self) -> Panel:
        """
        Compact market scanner — one row per asset.

        Columns (execution-critical only, no decorative padding):
          SYM | PRICE | DIV% | OB | SCORE | DIR | FR% | OI | STATUS

        Replaces 15 individual mini-panels (one per coin). A single Rich table
        renders in ~1ms vs ~15ms for 15 nested panels; critical at 2 Hz.
        """
        table = Table(
            expand=True,
            style="#e8edf2 on #0d1014",
            border_style="#2a3a4a",
            show_lines=False,
            padding=(0, 0),
        )
        table.add_column("SYM",    min_width=7,  no_wrap=True)
        table.add_column("PRICE",  min_width=9,  justify="right", no_wrap=True)
        table.add_column("DIV",    min_width=5,  justify="right", no_wrap=True)
        table.add_column("OB",     min_width=4,  justify="right", no_wrap=True)
        table.add_column("SCR",    min_width=4,  justify="right", no_wrap=True)
        table.add_column("DIR",    min_width=5,  no_wrap=True)
        table.add_column("FR%",    min_width=7,  justify="right", no_wrap=True)
        table.add_column("OI",     min_width=5,  justify="right", no_wrap=True)
        table.add_column("STATUS", min_width=11, no_wrap=True)

        assets_cache  = self._display_cache.get("assets", {})
        signals_cache = self._display_cache.get("signals", {})
        calendar_cache = self._display_cache.get("calendar", {})
        positions = self._display_cache.get("positions", [])
        open_syms = {getattr(p, "symbol", "") for p in positions}

        for asset in self.config.assets:
            a   = assets_cache.get(asset, {})
            sig = signals_cache.get(asset, {})

            sym_short = asset.replace("-USD", "")

            # ── Price ──────────────────────────────────────────────────────────
            price     = a.get("last_price", 0.0) or a.get("mark_price", 0.0)
            price_up  = a.get("price_up", False)
            price_dn  = a.get("price_down", False)
            _px_color = "#00d084" if price_up else ("#ff4757" if price_dn else "#aaaaaa")
            _blink    = " blink" if (price_up or price_dn) else ""
            if price >= 1000:
                price_str = f"{price:,.0f}"
            elif price >= 1:
                price_str = f"{price:.3f}"
            elif price > 0:
                price_str = f"{price:.5f}"
            else:
                price_str = "—"

            # ── Divergence % ───────────────────────────────────────────────────
            div = a.get("divergence_pct", 0.0)
            div_color = "#f5a623" if abs(div) > 0.05 else "dim"
            div_str   = f"{div:+.2f}" if div != 0 else "—"

            # ── Orderbook age ──────────────────────────────────────────────────
            ob_age = a.get("ob_age", 999999)
            if ob_age > 10000:
                ob_str   = "—"
                ob_color = "dim"
            elif ob_age > 1000:
                ob_str   = f"{ob_age // 1000}s"
                ob_color = "#f5a623"
            else:
                ob_str   = f"{ob_age}ms"
                ob_color = "#00d084"

            # ── Signal score + direction ────────────────────────────────────────
            w_score   = sig.get("weighted_score", 0.0)
            direction = sig.get("direction", "none").upper()
            scr_color = "#00d084" if w_score >= 5.0 else ("#f5a623" if w_score >= 3.5 else "dim")
            scr_str   = f"{w_score:.1f}" if w_score > 0 else "—"

            if direction == "LONG":
                dir_cell = "[bold #00d084]▲ L[/]"
            elif direction == "SHORT":
                dir_cell = "[bold #ff4757]▼ S[/]"
            else:
                dir_cell = "[dim]—[/]"

            # ── Funding rate ───────────────────────────────────────────────────
            fr      = a.get("bybit_funding", 0.0)
            fr_pct  = fr * 100.0
            fr_color = "#ff4757" if fr_pct > 0.01 else ("#00d084" if fr_pct < -0.01 else "dim")
            fr_str   = f"{fr_pct:+.4f}" if fr != 0 else "—"

            # ── Open Interest ──────────────────────────────────────────────────
            oi      = a.get("bybit_oi", 0.0)
            oi_prev = a.get("bybit_oi_prev", 0.0)
            oi_delta = oi - oi_prev if oi_prev > 0 else 0.0
            if oi_delta > 0:
                oi_color, oi_arrow = "#00d084", "↑"
            elif oi_delta < 0:
                oi_color, oi_arrow = "#ff4757", "↓"
            else:
                oi_color, oi_arrow = "dim", "─"
            oi_str = f"{oi_arrow}{oi/1e6:.1f}M" if oi > 0 else "—"

            # ── Status cell — execution readiness, highest priority first ──────
            warmup_phase = a.get("warmup_phase", "WARMING_UP")
            warmup_count = a.get("warmup_count", 0)
            cal = calendar_cache.get(asset)
            cal_regime = getattr(cal, "regime", "CLEAR") if cal else "CLEAR"
            cal_hours  = getattr(cal, "hours_to_event", None) if cal else None
            cal_event  = getattr(cal, "nearest_event_name", "") if cal else ""

            in_trade = asset in open_syms

            if in_trade:
                status_str  = "● OPEN"
                status_color = "#f5a623"
            elif warmup_phase == "WARMING_UP":
                status_str  = f"~{warmup_count}/50"
                status_color = "#555555"
            elif cal_regime == "BLOCK":
                tag = cal_event[:6] if cal_event else "EVT"
                status_str  = f"BLK {tag}"
                status_color = "#ff4757"
            elif cal_hours is not None and cal_hours < 1.0:
                tag = cal_event[:6] if cal_event else "EVT"
                status_str  = f"⚡{tag} {cal_hours*60:.0f}m"
                status_color = "#ff4757"
            elif cal_hours is not None and cal_hours < 4.0:
                status_str  = f"~{cal_hours:.1f}h"
                status_color = "#f5a623"
            elif w_score >= 4.5 and direction not in ("NONE", "none"):
                arrow = "▲" if direction == "LONG" else "▼"
                status_str  = f"{arrow}STRONG {w_score:.1f}"
                status_color = "#00d084" if direction == "LONG" else "#ff4757"
            else:
                status_str  = "READY"
                status_color = "#00d084"

            # ── Highlight row if this coin has an open position ────────────────
            row_style = "bold" if in_trade else ""

            table.add_row(
                f"[bold {_px_color}]{sym_short}[/]",
                f"[{_px_color}{_blink}]{price_str}[/]",
                f"[{div_color}]{div_str}[/]",
                f"[{ob_color}]{ob_str}[/]",
                f"[{scr_color}]{scr_str}[/]",
                dir_cell,
                f"[{fr_color}]{fr_str}[/]",
                f"[{oi_color}]{oi_str}[/]",
                f"[{status_color}]{status_str}[/]",
                style=row_style,
            )

        return Panel(
            table,
            title="[bold #00aaff]◈ MARKET SCANNER[/]",
            style="#e8edf2 on #0d1014",
            border_style="#2a3a4a",
            padding=(0, 0),
        )

    def _build_intelligence_panel(self) -> Panel:
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#00aaff", show_lines=False)
        table.add_column("SYM", min_width=8)
        table.add_column("Wtd", justify="right", min_width=5)
        table.add_column("Dir", min_width=5)
        table.add_column("ATR", justify="right", min_width=7)
        table.add_column("ATR×", justify="right", min_width=5)
        table.add_column("Coh×", justify="right", min_width=5)
        table.add_column("Frsh×", justify="right", min_width=5)
        table.add_column("Cal×", justify="right", min_width=5)
        table.add_column("Eff×", justify="right", min_width=5)
        table.add_column("Sweep", min_width=6)
        table.add_column("VPIN", justify="right", min_width=5)

        signals = self._display_cache.get("signals", {})

        for asset in self.config.assets:
            sig = signals.get(asset, {})
            wtd = sig.get("weighted_score", 0.0)
            direction = sig.get("direction", "none").upper()
            atr = sig.get("atr", 0.0)
            atr_ratio = sig.get("atr_ratio", 1.0)
            coh_mult = sig.get("coherence_mult", 0.0)
            frsh_mult = sig.get("freshness_mult", 1.0)
            cal_mult = sig.get("calendar_mult", 1.0)
            eff_mult = coh_mult * frsh_mult * cal_mult
            sweep = sig.get("sweep", "none")
            vpin = sig.get("vpin", 0.0)

            score_color = "#00d084" if wtd >= 5.0 else ("#f5a623" if wtd >= 4.0 else "#ff4757")
            dir_color = "#00d084" if direction == "LONG" else ("#ff4757" if direction == "SHORT" else "dim")
            atr_ratio_color = "#ff4757" if atr_ratio > 1.5 else ("#f5a623" if atr_ratio > 1.2 else "#00d084")
            vpin_color = "#ff4757" if vpin > 0.7 else ("#f5a623" if vpin > 0.5 else "white")
            sweep_color = "#00d084" if sweep == "buy_side" else ("#ff4757" if sweep == "sell_side" else "dim")

            if atr >= 1000:
                atr_str = f"{atr:,.0f}"
            elif atr >= 1:
                atr_str = f"{atr:.2f}"
            else:
                atr_str = f"{atr:.4f}"

            table.add_row(
                f"[bold]{asset.replace('-USD', '')}[/]",
                f"[{score_color}]{wtd:.1f}[/]",
                f"[{dir_color}]{direction[:5]}[/]",
                atr_str,
                f"[{atr_ratio_color}]{atr_ratio:.2f}[/]",
                f"{coh_mult:.2f}",
                f"{frsh_mult:.2f}",
                f"{cal_mult:.2f}",
                f"[bold]{eff_mult:.2f}[/]",
                f"[{sweep_color}]{sweep.replace('_side', '').upper() if sweep != 'none' else '—'}[/]",
                f"[{vpin_color}]{vpin:.2f}[/]"
            )

        mag7 = self._display_cache.get("mag7", {})
        mag7_subtitle = ""
        if mag7:
            if mag7.get("stale"):
                mag7_subtitle = "  [dim]MAG7: STALE[/]"
            elif mag7.get("direction") == "bullish":
                mag7_subtitle = f"  [#00d084]MAG7: BULL {mag7.get('strength', 0.0):.2f}[/]"
            elif mag7.get("direction") == "bearish":
                mag7_subtitle = f"  [#ff4757]MAG7: BEAR {mag7.get('strength', 0.0):.2f}[/]"
            else:
                mag7_subtitle = "  [dim]MAG7: NEUT[/]"

        return Panel(
            table,
            title=f"[bold #00aaff]▶ SIGNAL ENGINE — LIVE TIER ANALYSIS[/]{mag7_subtitle}",
            style="#e8edf2 on #0d1014",
            border_style="#00aaff"
        )

    def _build_trade_flow(self) -> Panel:
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#4a5a6a")
        table.add_column("Asset")
        table.add_column("Buy Vol", justify="right")
        table.add_column("Sell Vol", justify="right")
        table.add_column("Delta", justify="right")
        table.add_column("Ratio", justify="right")

        flow = self._display_cache.get("flow", {})
        for asset in self.config.assets:
            f = flow.get(asset, {})
            bv = f.get("buy_vol", 0.0)
            sv = f.get("sell_vol", 0.0)
            delta = f.get("delta", 0.0)
            ratio = f.get("aggressor_ratio", 0.0)
            delta_color = "#00d084" if delta >= 0 else "#ff4757"
            table.add_row(
                asset,
                f"{bv:,.2f}",
                f"{sv:,.2f}",
                f"[{delta_color}]{delta:+,.2f}[/]",
                f"{ratio:.2f}"
            )

        return Panel(table, title="Trade Flow (60s)", style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_equity_curve(self) -> Panel:
        equity = self._display_cache.get("equity", [])
        if not equity:
            return Panel(Text("Waiting for trades...", style="dim"), title="Equity Curve")

        balances = [b for t, b in equity]
        if len(balances) < 2:
            return Panel(Text("Collecting points...", style="dim"), title="Equity Curve")

        max_b = max(balances)
        min_b = min(balances)
        spread = max_b - min_b if max_b != min_b else 100

        rows = 4
        cols = 20
        chart = [[" " for _ in range(cols)] for _ in range(rows)]
        points = balances[-cols:]

        for i, val in enumerate(points):
            y = int((val - min_b) / spread * (rows - 1))
            char = "─"
            if i > 0:
                if points[i] > points[i - 1]:
                    char = "╮"
                elif points[i] < points[i - 1]:
                    char = "╯"
            chart[rows - 1 - y][i] = char

        lines = ["".join(row) for row in chart]
        chart_text = "\n".join(lines)
        return Panel(Text(chart_text, style="green"), title="Equity Curve")

    def _build_calendar_panel(self) -> Panel:
        """
        Unified calendar panel — single source of truth for all calendar data.
        Top section: per-asset regime/multipliers from CalendarEngine.get_states_all().
        Bottom section: upcoming events (72h) from CalendarEngine.event_store.get_upcoming().
        Both read from _display_cache["calendar"] and _display_cache["calendar_events"] —
        populated once per cycle by _populate_cache(), never fetched inside the panel builder.
        """
        from rich.console import Group as RichGroup

        # ── Per-asset regime table ─────────────────────────────────────────────
        status_table = Table(
            show_header=True, expand=True,
            style="#e8edf2 on #0d1014", border_style="#4a5a6a",
            title="[bold #aaaaaa]Asset Status[/]",
        )
        status_table.add_column("Asset", min_width=8)
        status_table.add_column("Regime", justify="center", min_width=8)
        status_table.add_column("Size", justify="right", min_width=5)
        status_table.add_column("Stop", justify="right", min_width=5)
        status_table.add_column("Note", no_wrap=False)

        states = self._display_cache.get("calendar", {})
        _cal_signals = self._display_cache.get("signals", {})
        # Time regime phase — same source as Market News; integrated per-asset note
        _ctx = self._display_cache.get("context")
        _tr_phase = getattr(_ctx, "time_regime_phase", "") if _ctx else ""
        _tr_notes = getattr(_ctx, "time_regime_notes", "") if _ctx else ""
        # Abbreviate time regime for per-asset note (keep it short)
        _tr_abbrev = ""
        if _tr_phase:
            _phase_map = {
                "early_week": "early wk",
                "mid_week":   "mid wk",
                "late_week":  "late wk",
                "mid_month_tue_wed": "mid-mo chop",
                "mid_month":  "mid-mo",
                "event_block": "event⚡",
                "pre_open":   "pre-open",
                "lunch_lull": "lunch",
                "power_hour": "power hr",
            }
            _tr_abbrev = _phase_map.get(_tr_phase, _tr_phase.replace("_", " "))

        if states:
            for asset, s in states.items():
                reason_str = (s.reason or "").replace("_", " ")
                if s.regime == "BLOCK":
                    regime_color = "#ff4757"
                    asset_str = f"[bold #ff4757]{asset}[/]"
                elif s.regime == "CAUTION":
                    regime_color = "#f5a623"
                    asset_str = f"[#f5a623]{asset}[/]"
                else:
                    regime_color = "#4a5a6a"
                    asset_str = f"[dim]{asset}[/dim]"

                # Signal direction prefix — shows real-time market bias
                _sig = _cal_signals.get(asset, {})
                _dir = _sig.get("direction", "none")
                _score = _sig.get("weighted_score", 0.0)
                if _dir == "long" and _score > 0:
                    _dir_prefix = "[#00d084]↑[/] "
                elif _dir == "short" and _score > 0:
                    _dir_prefix = "[#ff4757]↓[/] "
                else:
                    _dir_prefix = ""

                evt_name = (s.nearest_event_name or "")[:16] or "—"
                hrs = s.hours_to_event
                note_body = f"{evt_name} {hrs:.1f}h" if hrs is not None else reason_str or "clear"
                # Append time regime for CLEAR assets so context is visible per-row
                if _tr_abbrev and s.regime == "CLEAR":
                    note_body = f"{note_body} | [dim #666666]{_tr_abbrev}[/]"
                elif _tr_abbrev and s.regime != "BLOCK":
                    note_body = f"{note_body} [{_tr_abbrev}]"
                note = f"{_dir_prefix}{note_body}"
                status_table.add_row(
                    asset_str,
                    f"[{regime_color}]{s.regime}[/]",
                    f"{float(s.size_multiplier):.2f}×",
                    f"{float(s.stop_atr_multiplier):.1f}×",
                    f"[dim]{note}[/dim]" if s.regime == "CLEAR" and not _dir_prefix else note,
                )
        else:
            status_table.add_row("[dim]Loading…[/dim]", "—", "—", "—", "—")

        # ── Upcoming events table ──────────────────────────────────────────────
        events_table = Table(
            show_header=True, expand=True,
            style="#e8edf2 on #0d1014", border_style="#4a5a6a",
            title="[bold #aaaaaa]Upcoming (72h)[/]",
        )
        events_table.add_column("Event", no_wrap=False)
        events_table.add_column("Type", min_width=6)
        events_table.add_column("In", justify="right", min_width=5)

        events = self._display_cache.get("calendar_events", [])
        if events:
            now = datetime.now(timezone.utc)
            for ev in events[:8]:   # cap at 8 rows to fit panel
                delta = ev.event_time - now
                total_s = delta.total_seconds()
                hours = total_s / 3600.0
                if hours > 1:
                    countdown = f"{hours:.1f}h"
                else:
                    countdown = f"{total_s / 60:.0f}m"
                color = "#ff4757" if hours < 6 else ("#f5a623" if hours < 24 else "white")
                events_table.add_row(ev.name[:22], ev.event_type, f"[{color}]{countdown}[/]")
        else:
            events_table.add_row("[dim]No events scheduled[/dim]", "—", "—")

        # ── Time regime overlay + signal weights ──────────────────────────────────
        # Mirror the same time-regime and weight info shown in Market News so the
        # calendar panel is self-contained (day-of-week, time-of-month, event block,
        # plus any active signal tier-weight overrides).
        ctx = self._display_cache.get("context")
        _group_items = [status_table, events_table]
        if ctx is not None:
            tr_phase = getattr(ctx, "time_regime_phase", "")
            tr_notes = getattr(ctx, "time_regime_notes", "")
            sw       = getattr(ctx, "signal_weights", {}) or {}

            _overlay_parts: list = []
            if tr_phase:
                _is_event = "event" in tr_phase or "block" in tr_phase
                _ph_color = "#f5a623" if _is_event else "#888888"
                _overlay_parts.append(
                    f"[dim]Time:[/dim] [{_ph_color}]{tr_phase}[/]"
                )
            if tr_notes:
                # Show all notes, pipe-separated, truncated for space
                _first_note = tr_notes.split(" | ")[0]
                _overlay_parts.append(f"[dim #666666]{_first_note}[/]")
            non_default_w = {k: v for k, v in sw.items() if v != 1.0}
            if non_default_w:
                _wstr = "  ".join(
                    f"[#888888]{k[:5]}:[/][bold]{v:.1f}×[/]"
                    for k, v in sorted(non_default_w.items())
                )
                _overlay_parts.append(f"[dim]Wts:[/dim] {_wstr}")

            if _overlay_parts:
                from rich.text import Text as RichText
                _overlay_text = RichText.from_markup("  ".join(_overlay_parts))
                _group_items.append(_overlay_text)

        return Panel(
            RichGroup(*_group_items),
            title="[bold]CALENDAR[/bold]",
            style="#e8edf2 on #0d1014",
            border_style="#4a5a6a",
        )

    def _build_funding_radar(self) -> Panel:
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#4a5a6a")
        table.add_column("Asset")
        table.add_column("Rate", justify="right")
        table.add_column("Carry", justify="right")
        table.add_column("Signal", justify="center")
        table.add_column("Direction")

        funding = self._display_cache.get("funding", {})
        # Augment with live Bybit funding rates for any symbol without a FundingRadar snap
        _bybit_rates_for_display = {
            s: d.get("funding_rate", 0.0)
            for s, d in self._bybit_ticker_stores.items()
            if d.get("funding_rate", 0.0) != 0.0
        }
        for asset, snap in funding.items():
            try:
                rate = getattr(snap, "rate", 0.0)
                score = getattr(snap, "carry_score", 0.0)
                signal = getattr(snap, "arb_signal", False)
                direction = getattr(snap, "direction", "none")
                rate_color = "#00d084" if rate > 0 else "#ff4757"
                table.add_row(
                    str(asset),
                    f"[{rate_color}]{rate * 100:.4f}%[/]" if isinstance(rate, float) else str(rate),
                    f"{score:.2f}" if isinstance(score, float) else str(score),
                    "✓" if signal else "✗",
                    str(direction or "—")
                )
            except Exception as e:
                logger.error("funding_row_render_error", asset=str(asset), error=str(e))
                table.add_row(str(asset), "ERROR", "ERROR", "ERROR", "ERROR")

        return Panel(table, title="FUNDING RADAR", style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_open_positions_panel(self) -> Panel:
        import time as _time
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#00aaff", show_lines=False)
        table.add_column("Sym", min_width=5)
        table.add_column("Dir", min_width=5)
        table.add_column("Entry", justify="right", min_width=9)
        table.add_column("Mark", justify="right", min_width=9)
        table.add_column("uPnL", justify="right", min_width=8)
        table.add_column("Stop", justify="right", min_width=10)
        table.add_column("TPs", justify="right", min_width=14)
        table.add_column("Lev", justify="right", min_width=4)
        table.add_column("Age", justify="right", min_width=7)
        table.add_column("Liq%", justify="right", min_width=5)

        positions = self._display_cache.get("positions", [])
        assets_cache = self._display_cache.get("assets", {})
        now_ms = int(_time.time() * 1000)

        if not positions:
            table.add_row("[dim]—[/]", "[dim]No open positions[/]", "", "", "", "", "", "", "", "")
        else:
            for pos in positions:
                sym = getattr(pos, "symbol", "?")
                side = getattr(pos, "side", "long")
                entry = getattr(pos, "entry_price", 0.0)
                stop = getattr(pos, "stop_price", 0.0)
                tp1 = getattr(pos, "tp1_price", 0.0)
                tp2 = getattr(pos, "tp2_price", 0.0)
                tp3 = getattr(pos, "tp3_price", 0.0)
                size = getattr(pos, "size", 0.0)
                lev = getattr(pos, "leverage", 1)
                liq = getattr(pos, "liq_price", 0.0)
                tp1_hit = getattr(pos, "tp1_hit", False)
                tp2_hit = getattr(pos, "tp2_hit", False)
                opened_at = getattr(pos, "opened_at_ms", now_ms)

                # Use cached mark price (no live store call)
                mark = assets_cache.get(sym, {}).get("mark_price", entry) or entry
                if mark <= 0:
                    mark = entry

                upnl = (mark - entry) * size if side == "long" else (entry - mark) * size
                dir_color = "#00d084" if side == "long" else "#ff4757"
                pnl_color = "#00d084" if upnl >= 0 else "#ff4757"
                sym_short = sym.replace("-USD", "")

                def _fmt_price(p: float) -> str:
                    if p >= 1000:
                        return f"{p:,.1f}"
                    elif p >= 1:
                        return f"{p:.3f}"
                    return f"{p:.5f}"

                stop_str = _fmt_price(stop) if stop > 0 else "[bold #ff4444]NO STOP[/]"

                def _tp_label(price: float, hit: bool, label: str) -> str:
                    if price <= 0:
                        return f"[dim]{label}:—[/]"
                    marker = "[#00d084]✓[/]" if hit else ""
                    return f"{marker}{label}:{_fmt_price(price)}"

                tp_str = " ".join([
                    _tp_label(tp1, tp1_hit, "T1"),
                    _tp_label(tp2, tp2_hit, "T2"),
                    _tp_label(tp3, False, "T3"),
                ])

                age_ms = max(0, now_ms - opened_at)
                age_s = age_ms // 1000
                if age_s < 60:
                    age_str = f"{age_s}s"
                elif age_s < 3600:
                    age_str = f"{age_s // 60}m{age_s % 60:02d}s"
                else:
                    age_str = f"{age_s // 3600}h{(age_s % 3600) // 60:02d}m"

                if liq > 0 and mark > 0:
                    liq_pct = abs(mark - liq) / mark * 100
                    liq_color = "#ff4444" if liq_pct < 5 else ("#f5a623" if liq_pct < 15 else "#888888")
                    liq_str = f"[{liq_color}]{liq_pct:.1f}%[/]"
                else:
                    liq_str = "[dim]—[/]"

                table.add_row(
                    f"[bold]{sym_short}[/]",
                    f"[{dir_color}]{side.upper()}[/]",
                    _fmt_price(entry),
                    _fmt_price(mark),
                    f"[{pnl_color}]{upnl:+.2f}[/]",
                    stop_str,
                    tp_str,
                    f"{lev}x",
                    age_str,
                    liq_str,
                )

        mode = self.config.mode.upper()
        title_color = "#ff4444" if mode == "LIVE" else "#888888"
        return Panel(
            table,
            title=f"[bold {title_color}]▶ OPEN POSITIONS ({mode})[/]",
            style="#e8edf2 on #0d1014",
            border_style="#00aaff"
        )

    def _build_trade_candidates_panel(self) -> Panel:
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#f5a623", show_lines=False)
        table.add_column("Time", min_width=8)
        table.add_column("Sym", min_width=6)
        table.add_column("Dir", min_width=5)
        table.add_column("Scr", justify="right", min_width=4)
        table.add_column("Entry", justify="right", min_width=8)
        table.add_column("Stop", justify="right", min_width=7)
        table.add_column("RR", justify="right", min_width=4)
        table.add_column("Status", min_width=10)

        if not self._trade_candidate_log:
            table.add_row("[dim]—[/]", "[dim]No candidates yet[/]", "", "", "", "", "", "")
        else:
            for entry in reversed(self._trade_candidate_log):
                status = entry["status"]
                if status == "PLACED":
                    status_str = "[bold #00d084]✓ PLACED[/]"
                elif status == "REJECTED":
                    err_short = (entry.get("error") or "unknown")[:20]
                    status_str = f"[bold #ff4757]✗ REJECTED[/] [dim]{err_short}[/]"
                else:
                    status_str = "[bold #f5a623]⟳ SENT[/]"

                dir_color = "#00d084" if entry["dir"] == "long" else "#ff4757"
                sym_short = entry["sym"].replace("-USD", "")

                def _fp(p: float) -> str:
                    if p >= 1000:
                        return f"{p:,.1f}"
                    elif p >= 1:
                        return f"{p:.3f}"
                    return f"{p:.5f}"

                table.add_row(
                    f"[dim]{entry['ts']}[/]",
                    f"[bold]{sym_short}[/]",
                    f"[{dir_color}]{entry['dir'].upper()[:5]}[/]",
                    f"{entry['score']:.1f}",
                    _fp(entry["entry"]),
                    _fp(entry["stop"]),
                    f"{entry['rr']:.1f}R",
                    status_str,
                )

        return Panel(
            table,
            title="[bold #f5a623]▶ TRADE CANDIDATES[/]",
            style="#e8edf2 on #0d1014",
            border_style="#f5a623"
        )

    def _build_arb_positions(self) -> Panel:
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#4a5a6a")
        table.add_column("Asset")
        table.add_column("Direction")
        table.add_column("Size", justify="right")
        table.add_column("Entry", justify="right")
        table.add_column("Current", justify="right")
        table.add_column("Notional", justify="right")
        table.add_column("P&L", justify="right")

        assets_cache = self._display_cache.get("assets", {})
        for pos in self._open_arbs:
            try:
                symbol = getattr(pos, "symbol", "unknown")
                direction = getattr(pos, "direction", "none")
                size = getattr(pos, "size", 0.0)
                entry = getattr(pos, "entry_price", 0.0)
                pnl = getattr(pos, "current_pnl", 0.0)
                current_price = assets_cache.get(symbol, {}).get("mark_price", 0.0)
                notional = size * current_price
                pnl_color = "green" if pnl >= 0 else "red"
                table.add_row(
                    str(symbol),
                    str(direction),
                    f"{size:.4f}",
                    f"${entry:,.2f}" if entry > 0 else "—",
                    f"${current_price:,.2f}",
                    f"${notional:,.2f}",
                    f"[{pnl_color}]${pnl:+.4f}[/]"
                )
            except Exception as e:
                logger.error("arb_row_render_error", error=str(e))
                table.add_row("ERROR", "...", "0.0000", "$0.00", "$0.00", "$0.00", "$0.0000")

        return Panel(table, title="ACTIVE ARB LEGS", style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_allocation_panel(self) -> Panel:
        total_slots = 30
        session = self._display_cache.get("session", {})
        balance = session.get("balance", 0.0)
        spot_balance = session.get("spot_balance", 0.0)
        deployed = session.get("deployed", 0.0)

        total_capital = deployed + balance
        deploy_pct = min(1.0, deployed / total_capital) if total_capital > 0 else 0.0
        filled = int(deploy_pct * total_slots)
        free_slots = total_slots - filled
        deploy_color = "#00ff88" if deploy_pct < 0.25 else "#ffcc00" if deploy_pct < 0.60 else "#ff4455"
        bar = f"[{deploy_color}]{'█' * filled}[/][dim]{'░' * free_slots}[/dim]"

        spot_line = (
            f"  [dim]Spot: [bold]${spot_balance:,.2f}[/bold][/dim]"
            if spot_balance > 0 else ""
        )
        content = (
            f"[dim]Alloc:[/dim] [{deploy_color}]{deploy_pct * 100:.1f}%[/] deployed  "
            f"[dim]|[/dim]  [bold]${deployed:,.2f}[/bold] / [dim]Perps ${balance:,.2f}[/dim]{spot_line}\n"
            f"[{bar}]"
        )
        return Panel(Text.from_markup(content), style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_stats_panel(self) -> Panel:
        session = self._display_cache.get("session", {})
        uptime_s = session.get("uptime_s", 0)
        hours, rem = divmod(uptime_s, 3600)
        mins, secs = divmod(rem, 60)
        uptime_str = f"{hours:02d}:{mins:02d}:{secs:02d}"

        win_rate = session.get("win_rate", 0.0)
        total_pnl = session.get("total_pnl", 0.0)
        sqn = session.get("sqn", 0.0)
        closed = session.get("closed", 0)
        balance = session.get("balance", 0.0)
        spot_balance = session.get("spot_balance", 0.0)
        deployed = session.get("deployed", 0.0)
        dd_pct = session.get("dd_pct", 0.0)
        dd_regime = session.get("dd_regime", "normal")
        dd_streak = session.get("dd_streak", 0)

        _total_cap = deployed + balance
        deploy_pct = (deployed / _total_cap * 100) if _total_cap > 0 else 0.0

        mode = self.config.mode.upper()
        mode_color = "#ff4444" if mode == "LIVE" else ("#f5a623" if mode == "TESTNET" else "#888888")
        pnl_color = "#00d084" if total_pnl >= 0 else "#ff4757"
        sqn_color = "#00d084" if sqn >= 2.0 else ("#f5a623" if sqn >= 1.0 else "dim")
        _dd_regime_color = {
            "normal": "#00d084",
            "caution": "#f5a623",
            "defensive": "#ff4757",
            "halt": "bold #ff0000",
        }.get(dd_regime, "dim")

        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column()
        grid.add_column()
        grid.add_column()
        grid.add_column()

        bal_label = (
            f"[bold]Perps[/] ${balance:,.2f}  [dim]Spot ${spot_balance:,.2f}[/]"
            if spot_balance > 0
            else f"[bold]Bal[/] ${balance:,.2f}"
        )
        grid.add_row(
            bal_label,
            f"[bold]Dep[/] ${deployed:,.2f} ({deploy_pct:.1f}%)",
            f"[bold]WR[/] [yellow]{win_rate:.1f}%[/] ({closed}T)",
            f"[bold]P&L[/] [{pnl_color}]${total_pnl:+.2f}[/]"
        )
        grid.add_row(
            f"[bold]SQN[/] [{sqn_color}]{sqn:.2f}[/]",
            f"[{mode_color}][bold]{mode}[/][/]",
            f"[dim]Up {uptime_str}[/]",
            f"[bold]DD[/] [{_dd_regime_color}]{dd_pct:.1f}% {dd_regime.upper()}[/]  [dim]L-Str {dd_streak}[/]"
        )

        cal = self._display_cache.get("calibrator", {})
        if cal:
            try:
                coh_min = cal.get("coherence_min", 0.0)
                loss_str = cal.get("loss_streak", 0)
                fast_wr = cal.get("fast_wr", 0.0)
                med_wr = cal.get("medium_wr", 0.0)
                coh_color = "#ff4757" if coh_min > 3.5 else ("#f5a623" if coh_min > 2.5 else "#00d084")
                grid.add_row(
                    f"[bold]Coh-Min[/] [{coh_color}]{coh_min:.1f}[/]",
                    f"[dim]L-Str[/] {loss_str}",
                    f"[dim]WR5[/] {fast_wr * 100:.0f}%  [dim]WR10[/] {med_wr * 100:.0f}%",
                    f"[dim]Adaptive[/]",
                )
            except Exception:
                pass

        return Panel(grid, title="[bold #00aaff]SESSION[/]",
                     style="#e8edf2 on #0d1014", border_style="#00aaff")

    def _build_chain_intelligence_panel(self) -> Panel:
        chain = self._display_cache.get("chain", {})
        healthy = chain.get("healthy", False)
        last_block = chain.get("last_block", 0)
        events_60s = chain.get("events_60s", 0)
        cascade_active = chain.get("cascade_active", False)
        rpc = chain.get("rpc_endpoint", "—")
        failures = chain.get("consecutive_failures", 0)

        if cascade_active:
            status_str = "[bold red blink]⚠ CASCADE — DO NOT TRADE[/]"
            border_style = "bold red"
        elif healthy:
            status_str = "[bold #00d084]● LIVE[/]"
            border_style = "#00d084"
        else:
            status_str = f"[bold #f5a623]◌ RECONNECTING ({failures}×)[/]"
            border_style = "#f5a623"

        rpc_short = rpc.replace("https://", "").split("/")[0][:28]
        block_str = f"{last_block:,}" if last_block else "—"
        events_color = "#ff4757" if events_60s >= 3 else ("#f5a623" if events_60s >= 1 else "dim")

        content = (
            f"[dim]Chain:[/dim]  SoDEX EVM (ID 286623)   {status_str}\n"
            f"[dim]RPC:[/dim]    [dim]{rpc_short}[/dim]\n"
            f"[dim]Block:[/dim]  {block_str}   "
            f"[dim]Liqs/60s:[/dim] [{events_color}]{events_60s}[/]"
        )

        signals = chain.get("active_signals", [])
        if signals:
            sig_lines = []
            for s in signals[:3]:
                sym = s.get("symbol", "?")[:4]
                src = s.get("source", "?")[:10]
                d = s.get("direction", "?")[0].upper()
                str_ = s.get("strength", 0.0)
                age = s.get("age_s", 0)
                clr = "#00d084" if d == "L" else "#ff4757"
                sig_lines.append(f"[{clr}]{sym:5} {src:12} {d} {str_:.2f}[/]  [dim]{age}s ago[/]")
            content += "\n" + "\n".join(sig_lines)

        # Cascade state from cache
        cascade = self._display_cache.get("cascade")
        if cascade is not None:
            try:
                phase = cascade.get("phase", "idle").upper()
                phase_colors = {
                    "IDLE":      "dim",
                    "DETECTING": "#f5a623",
                    "BLOCKED":   "bold #ff4757 blink",
                    "PRIMED":    "bold #00d084 blink",
                    "MOMENTUM":  "bold #ffcc00 blink",
                }
                p_clr = phase_colors.get(phase, "dim")
                snap = cascade.get("snapshot") or {}
                snap_dir = snap.get("direction", "")
                snap_note = f"${snap.get('notional_usd', 0):,.0f}" if snap.get("notional_usd") else ""
                primed_dir = cascade.get("primed_direction", "") or cascade.get("momentum_direction", "")
                vel = cascade.get("velocity", 0.0)
                aftermath = cascade.get("aftermath_signals", {})
                aft_str = " ".join(
                    f"[#00d084]{k[:4]}[/]" if v else f"[dim]{k[:4]}[/]"
                    for k, v in aftermath.items()
                ) if aftermath else "[dim]—[/]"
                cascade_line = (
                    f"\n[dim]─────────── CASCADE ───────────[/]"
                    f"\n[{p_clr}]{phase}[/]"
                    + (f"  [{('#00d084' if primed_dir == 'long' else '#ff4757')}]{primed_dir.upper()}[/]" if primed_dir else "")
                    + (f"  {snap_dir[:4].upper()}" if snap_dir else "")
                    + (f"  {snap_note}" if snap_note else "")
                    + (f"  vel={vel:.2f}" if vel else "")
                    + (f"\n{aft_str}" if aftermath else "")
                )
                content += cascade_line
            except Exception:
                pass

        # Macro intelligence from cache
        macro = self._display_cache.get("macro", {})
        if macro:
            try:
                _mdir = macro.get("macro_direction", "neutral")
                _mclr = "#00d084" if _mdir == "long" else ("#ff4757" if _mdir == "short" else "dim")
                _at = macro.get("assets_total_active", 0)
                _confirming = f"{macro.get('assets_confirming', 0)}/{_at}" if _at > 0 else "—"
                _cap = "[bold #ff4757]CAP[/]" if macro.get("capitulation_detected") else "[dim]—[/]"
                _pe = "[bold #f5a623]POST-EVT[/]" if macro.get("post_event_active") else "[dim]—[/]"
                _fr = macro.get("funding_regime", "").replace("crowded_", "CR:").upper()
                _fr_clr = "#f5a623" if "CR:" in _fr else "dim"
                _xaut_dir = macro.get("xaut_direction", "")
                _xaut_conf = macro.get("xaut_confirms_regime", False)
                _xaut_mult = macro.get("xaut_macro_mult", 1.0)
                _xaut_str = (
                    f"[#00d084]AU↑ {_xaut_mult:.2f}×[/]" if _xaut_dir == "long" and _xaut_conf
                    else f"[#ff4757]AU↓ {_xaut_mult:.2f}×[/]" if _xaut_dir == "short" and _xaut_conf
                    else "[dim]AU—[/]"
                )
                content += (
                    f"\n[dim]────────────── MACRO ──────────────[/]"
                    f"\n[{_mclr}]{_mdir.upper():5}[/] {_confirming}  [{_fr_clr}]{_fr}[/]  {_xaut_str}"
                    f"  {_cap}  {_pe}"
                    f"  [dim]vol {macro.get('volume_quality_mult', 1.0):.2f}×[/]"
                )
            except Exception:
                pass

        return Panel(
            Text.from_markup(content),
            title="[bold #aa77ff]⛓ CHAIN INTELLIGENCE[/]",
            style="#e8edf2 on #0d1014",
            border_style=border_style,
        )

    def _build_true_arb_panel(self) -> Panel:
        table = Table(expand=True, style="#e8edf2 on #0d1014",
                      border_style="#aa77ff", show_lines=False)
        table.add_column("Sym", min_width=5)
        table.add_column("Dir", min_width=10)
        table.add_column("Qty", justify="right", min_width=7)
        table.add_column("Entry$", justify="right", min_width=8)
        table.add_column("Held", justify="right", min_width=6)
        table.add_column("Fund$", justify="right", min_width=7)

        positions = self._display_cache.get("arb_legs", [])
        if not positions:
            table.add_row("[dim]—[/]", "[dim]No true arb positions[/]", "", "", "", "")
        else:
            now_t = time.time()
            for pos in positions:
                sym = getattr(pos, "symbol", "?").replace("-USD", "")
                direction = getattr(pos, "direction", "?")
                qty = getattr(pos, "spot_qty", 0.0)
                entry = getattr(pos, "spot_entry", 0.0)
                opened_at = getattr(pos, "opened_at", now_t)
                hold_h = (now_t - opened_at) / 3600
                funding = getattr(pos, "funding_collected_usd", 0.0)

                dir_short = "L↑S↓" if "long_spot" in direction else "S↓L↑"
                dir_color = "#00d084" if "long_spot" in direction else "#ff4757"
                hold_color = "#00d084" if hold_h >= 8 else "#f5a623"

                table.add_row(
                    f"[bold]{sym}[/]",
                    f"[{dir_color}]{dir_short}[/]",
                    f"{qty:.4f}",
                    f"${entry:,.2f}" if entry > 0 else "—",
                    f"[{hold_color}]{hold_h:.1f}h[/]",
                    f"[bold #00d084]${funding:.4f}[/]" if funding > 0 else "[dim]$0.0000[/]",
                )

        return Panel(
            table,
            title="[bold #aa77ff]⚖ TRUE ARB (SPOT+PERP)[/]",
            style="#e8edf2 on #0d1014",
            border_style="#aa77ff",
        )

    def _build_fee_intelligence_panel(self) -> Panel:
        fee = self._display_cache.get("fee", {})
        if not fee:
            return Panel(
                Text.from_markup("[dim]Fee data loading…[/dim]"),
                title="[bold #ffcc00]FEE INTELLIGENCE[/]",
                style="#e8edf2 on #0d1014",
                border_style="#ffcc00"
            )

        tier = fee.get("tier", 0)
        max_tier = 4
        vol_14d = fee.get("weighted_14d_volume", 0.0)
        gap = fee.get("volume_to_next_tier", 0.0)
        soso = fee.get("soso_staked", 0.0)
        staking_pct = fee.get("staking_discount_pct", 0.0)

        tier_bar = ""
        for i in range(max_tier + 1):
            if i < tier:
                tier_bar += "[bold #00d084]█[/]"
            elif i == tier:
                tier_bar += "[bold #ffcc00]█[/]"
            else:
                tier_bar += "[dim]░[/dim]"

        tier_color = "#00d084" if tier >= 3 else ("#ffcc00" if tier >= 1 else "dim")
        next_str = f"[dim]${gap:,.0f} to Tier {tier + 1}[/dim]" if gap > 0 else "[bold #00d084]MAX TIER[/]"

        staking_str = (
            f"[bold]{soso:,.0f}[/bold] SOSO → [bold #00d084]{staking_pct:.0f}%[/] off"
            if soso > 0
            else "[dim]0 SOSO staked[/dim]"
        )

        perp_t = fee.get("perps_taker_pct", 0.0)
        perp_m = fee.get("perps_maker_pct", 0.0)
        spot_t = fee.get("spot_taker_pct", 0.0)
        spot_m = fee.get("spot_maker_pct", 0.0)
        arb_rt = fee.get("arb_round_trip_maker_pct", 0.0)
        arb_be = fee.get("arb_break_even_3periods_maker_pct", 0.0)

        content = (
            f"[dim]Tier[/] [{tier_color}]{tier}[/]  {tier_bar}  {next_str}\n"
            f"[dim]14D vol:[/dim] [bold]${vol_14d:,.0f}[/]  {staking_str}\n"
            f"[dim]Perp[/]  T:[bold]{perp_t:.4f}%[/] M:[bold]{perp_m:.4f}%[/]  "
            f"[dim]Spot[/] T:[bold]{spot_t:.4f}%[/] M:[bold]{spot_m:.4f}%[/]\n"
            f"[dim]Arb RT[/] [bold]{arb_rt:.4f}%[/]  "
            f"[dim]BE/3prd[/] [bold]{arb_be:.4f}%[/]"
        )
        return Panel(
            Text.from_markup(content),
            title="[bold #ffcc00]FEE INTELLIGENCE[/]",
            style="#e8edf2 on #0d1014",
            border_style="#ffcc00",
        )
