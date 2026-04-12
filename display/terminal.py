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
        calendar_engine = None, # CalendarEngine
        journal: TradeJournal = None,
        perf: PerformanceTracker = None,
        system_state = None,  # SystemStateManager
        paper_client = None,
        position_manager = None,
        interpreter = None,  # IntelligenceInterpreter
        ws_manager = None
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
        self._paper_client = paper_client
        self._position_manager = position_manager
        self.interpreter = interpreter
        self._ws_manager = ws_manager
        
        # Phase 4.5 Data
        self._funding_snapshots = {}
        self._open_arbs = []

        # v1.4 On-chain + True Arb data
        self._vc_status: dict = {}        # ValueChain monitor status snapshot
        self._true_arb_positions: list = []  # TrueArbPosition list

        # v1.3 Cached Async Data
        self._calendar_states = {}
        self._upcoming_events = []

        self._equity_history = []  # List of (timestamp, perps balance)
        self._spot_balance: float = 0.0  # Latest spot account balance (independent from perps)
        self._fee_data: dict = {}          # Latest fee engine summary for display
        self.start_time = time.time()
        self._task = None

        # Trade candidate log — gates-passed submissions and SoDEX outcomes
        # Each entry: {"ts": str, "sym": str, "dir": str, "score": float,
        #              "entry": float, "stop": float, "tp1": float,
        #              "size": float, "lev": int, "rr": float,
        #              "status": str, "error": str|None}
        self._trade_candidate_log: deque = deque(maxlen=8)

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
        # If the last entry is a SUBMITTED for the same symbol, update it in place
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
        import time
        self._equity_history.append((int(time.time() * 1000), balance))
        # Keep last 200 points only
        if len(self._equity_history) > 200:
            self._equity_history = self._equity_history[-200:]

    def update_spot_balance(self, balance: float) -> None:
        """Update the spot account balance (separate from perps on SoDEX)."""
        self._spot_balance = balance

    def update_fee_data(self, fee_summary: dict) -> None:
        """Update fee engine summary for the fee intelligence panel."""
        self._fee_data = fee_summary

    def update_funding(self, snapshots: dict) -> None:
        """Updates the internal funding radar data."""
        self._funding_snapshots = snapshots

    def update_arbs(self, arbs: list) -> None:
        """Updates the internal active arbitrage positions."""
        self._open_arbs = arbs

    def update_vc_status(self, status: dict) -> None:
        """Update ValueChain monitor health/stats for display."""
        self._vc_status = status

    def update_true_arb_positions(self, positions: list) -> None:
        """Update true delta-neutral arb positions for display."""
        self._true_arb_positions = positions

    def _safe_panel(self, builder_method, title: str) -> Panel:
        """Wrapper to prevent panel builder exceptions from crashing the UI."""
        try:
            return builder_method()
        except Exception as e:
            # log for debugging but return a visual indicator
            structlog.get_logger(__name__).error(f"panel_build_error_{title}", error=str(e))
            return Panel(
                f"[red]Error: {str(e)}[/red]\n{traceback.format_exc() if self.config.debug else ''}",
                title=f"[red]{title}[/red]",
                border_style="red"
            )

    async def run(self) -> None:
        """Consolidated rendering loop with screen takeover."""
        # Initial async fetch to populate cache before first frame
        if self.calendar_engine:
            try:
                self._calendar_states = await self.calendar_engine.get_states_all(self.config.assets)
                self._upcoming_events = await self.calendar_engine.event_store.get_upcoming(hours_ahead=72)
            except Exception:
                pass

        with Live(self.generate_layout(), refresh_per_second=4, screen=True) as live:
            while True:
                try:
                    # Async data refresh (every ~1s to avoid DB slamming)
                    if self.calendar_engine and int(time.time() * 4) % 4 == 0:
                        self._calendar_states = await self.calendar_engine.get_states_all(self.config.assets)
                        if int(time.time()) % 60 == 0:
                            self._upcoming_events = await self.calendar_engine.event_store.get_upcoming(hours_ahead=72)

                    live.update(self.generate_layout())
                except Exception:
                    pass  # Never crash the render loop
                await asyncio.sleep(0.25)

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
        layout["left"].update(self._safe_panel(self._build_assets_panel, "Assets"))
        
        layout["center"].split(
            Layout(name="intelligence", ratio=2),
            Layout(name="open_positions", ratio=1),
            Layout(name="calendar_status", ratio=1),
            Layout(name="funding_radar", ratio=1),
            Layout(name="trade_candidates", ratio=1)
        )
        layout["center"]["intelligence"].update(self._safe_panel(self._build_intelligence_panel, "Intelligence"))
        layout["center"]["open_positions"].update(self._safe_panel(self._build_open_positions_panel, "Open Positions"))
        layout["center"]["calendar_status"].update(self._safe_panel(self._build_calendar_status_panel, "Calendar Status"))
        layout["center"]["funding_radar"].update(self._safe_panel(self._build_funding_radar, "Funding Radar"))
        layout["center"]["trade_candidates"].update(self._safe_panel(self._build_trade_candidates_panel, "Trade Candidates"))
        
        layout["right"].split(
            Layout(name="trade_flow", ratio=2),
            Layout(name="calendar_events", ratio=1),
            Layout(name="chain_intelligence", size=7),
            Layout(name="true_arb_positions", ratio=1),
            Layout(name="allocation", size=5),
            Layout(name="fee_intelligence", size=6),
            Layout(name="equity_curve", ratio=1),
            Layout(name="stats_row", size=6)
        )
        layout["right"]["trade_flow"].update(self._safe_panel(self._build_trade_flow, "Trade Flow"))
        layout["right"]["calendar_events"].update(self._safe_panel(self._build_calendar_events_panel, "Calendar Events"))
        layout["right"]["chain_intelligence"].update(self._safe_panel(self._build_chain_intelligence_panel, "Chain Intelligence"))
        layout["right"]["true_arb_positions"].update(self._safe_panel(self._build_true_arb_panel, "True Arb Positions"))
        layout["right"]["allocation"].update(self._safe_panel(self._build_allocation_panel, "Allocation"))
        layout["right"]["fee_intelligence"].update(self._safe_panel(self._build_fee_intelligence_panel, "Fee Intelligence"))
        layout["right"]["equity_curve"].update(self._safe_panel(self._build_equity_curve, "Equity Curve"))
        layout["right"]["stats_row"].update(self._safe_panel(self._build_stats_panel, "Stats"))

        return layout

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

        from core.system_state import SystemPhase
        global_phase = self.system_state.get_global_phase().value.upper() if self.system_state else "OFFLINE"
        phase_color = "#00d084" if global_phase in ("TRADING", "READY") else "#ff4757"
        source = "SODEX MAINNET"

        # Count active signals
        active_signals = 0
        if self.interpreter:
            for asset in self.config.assets:
                s = self.interpreter.get_market_state(asset)
                if s and getattr(s, 'trade_direction', 'none') != 'none':
                    active_signals += 1

        sig_color = "#00d084" if active_signals > 0 else "dim"
        header_text = Text.from_markup(
            f"[bold #00aaff]ARIA v1.3[/]  [{phase_color}]{global_phase}[/]  {mode_text}  "
            f"[dim]│[/]  {now}  [dim]│[/]  {source}  [dim]│[/]  "
            f"[{sig_color}]{active_signals} ACTIVE SIGNAL{'S' if active_signals!=1 else ''}[/]  "
            f"[dim]│[/]  [dim]8-ASSET AUTONOMOUS PERPS[/]"
        )
        header_text.justify = "center"
        return Panel(header_text, style="#0d1014", border_style=border)

    def _build_assets_panel(self) -> Layout:
        layout = Layout()
        assets_layouts = []
        for asset in self.config.assets:
            assets_layouts.append(Layout(self._build_single_asset_panel(asset), name=asset))
        layout.split(*assets_layouts)
        return layout

    def _build_single_asset_panel(self, asset: str) -> Panel:
        last_price = 0.0
        mark_price = 0.0
        divergence_pct = 0.0
        imb = 0.0
        ob_age = 999999
        buy_delta = 0.0

        if asset in self.trade_flow_stores:
            lp = self.trade_flow_stores[asset].latest_price()
            if lp is not None:
                last_price = lp
            buy_delta = self.trade_flow_stores[asset].delta()

        if asset in self.mark_price_stores:
            mp_data = self.mark_price_stores[asset].get()
            mark_price = mp_data["mark_price"]
            if mp_data["last_price"] != 0:
                last_price = mp_data["last_price"]
            divergence_pct = mp_data["divergence_pct"]

        if asset in self.orderbook_stores:
            imb = self.orderbook_stores[asset].imbalance()
            ob_age = self.orderbook_stores[asset].age_ms()

        last_price_str = f"{last_price:,.2f}" if last_price else "N/A"
        mark_price_str = f"{mark_price:,.2f}" if mark_price else "N/A"
        div_str = f"{divergence_pct:.2f}%"

        # Row 2: Imbalance bar | Buy Delta
        bar_len = 8
        if imb < 0:
            filled = int(abs(imb) * bar_len)
            bar = f"[#ff4757]{'█' * filled}[/]{'░' * (bar_len - filled)}"
        else:
            filled = int(imb * bar_len)
            bar = f"[#00d084]{'█' * filled}[/]{'░' * (bar_len - filled)}"
        
        delta_color = "#00d084" if buy_delta >= 0 else "#ff4757"
        row2 = f"IMB: {bar} | Δ: [{delta_color}]{buy_delta:+,.0f}[/]"

        # Row 3: OB age | Score | Direction — use interpreter (authoritative) over market_engine
        state = None
        if self.interpreter:
            state = self.interpreter.get_market_state(asset)
        elif self.market_engine:
            state = self.market_engine.get_market_state(asset)
        w_score = state.weighted_score if state and hasattr(state, 'weighted_score') else 0.0
        direction = state.trade_direction.upper() if state and hasattr(state, 'trade_direction') else "NONE"
        
        # v1.3: Cleaner OB Age Display
        if ob_age > 10000:
            ob_str = "OB: —"
            ob_color = "dim"
        elif ob_age > 1000:
            ob_str = f"OB: {ob_age//1000}s"
            ob_color = "#f5a623"
        else:
            ob_str = f"OB: {ob_age}ms"
            ob_color = "#00d084"
            
        row3 = f"[{ob_color}]{ob_str}[/] | Score: [bold yellow]{w_score:.1f}[/] | Dir: {direction}"

        # Row 4: Warm-up Status
        warmup_row = ""
        if self.system_state:
            status = self.system_state.get_warmup_status().get(asset, {})
            count = status.get("count", 0)
            target = status.get("target", 50)
            # v1.3: Simplified Readiness Display
            from core.system_state import SystemPhase
            phase_enum = self.system_state._symbol_phase.get(asset, SystemPhase.WARMING_UP)
            
            if phase_enum == SystemPhase.WARMING_UP:
                warmup_str = f"WARMING ({count}/50)"
                p_color = "#f5a623"
            else:
                warmup_str = "READY ✓"
                p_color = "#00d084"
            
            warmup_row = f"\n[{p_color}]{warmup_str}[/]"

        content = (
            f"L: {last_price_str} | M: {mark_price_str} | D: {div_str}\n"
            f"{row2}\n"
            f"{row3}"
            f"{warmup_row}"
        )

        return Panel(Text.from_markup(content), style="#e8edf2 on #0d1014", border_style="#4a5a6a")

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

        for asset in self.config.assets:
            state = None
            if self.interpreter:
                state = self.interpreter.get_market_state(asset)
            elif self.market_engine:
                state = self.market_engine.get_market_state(asset)

            wtd = state.weighted_score if state else 0.0
            direction = getattr(state, 'trade_direction', 'none').upper() if state else 'NONE'
            atr = getattr(state, 'atr', 0.0) if state else 0.0
            atr_ratio = getattr(state, 'atr_vs_baseline', 1.0) if state else 1.0
            coh_mult = getattr(state, 'coherence_mult', 0.0) if state else 0.0
            frsh_mult = getattr(state, 'freshness_mult', 1.0) if state else 1.0
            cal_mult = getattr(state, 'calendar_mult', 1.0) if state else 1.0
            eff_mult = coh_mult * frsh_mult * cal_mult
            sweep = getattr(state, 'sweep', 'none') if state else 'none'
            vpin = getattr(state, 'vpin', 0.0) if state else 0.0

            # Color scoring
            score_color = "#00d084" if wtd >= 5.0 else ("#f5a623" if wtd >= 4.0 else "#ff4757")
            dir_color = "#00d084" if direction == "LONG" else ("#ff4757" if direction == "SHORT" else "dim")
            atr_ratio_color = "#ff4757" if atr_ratio > 1.5 else ("#f5a623" if atr_ratio > 1.2 else "#00d084")
            vpin_color = "#ff4757" if vpin > 0.7 else ("#f5a623" if vpin > 0.5 else "white")
            sweep_color = "#00d084" if sweep == "buy_side" else ("#ff4757" if sweep == "sell_side" else "dim")

            # ATR formatted by asset
            if atr >= 1000:
                atr_str = f"{atr:,.0f}"
            elif atr >= 1:
                atr_str = f"{atr:.2f}"
            else:
                atr_str = f"{atr:.4f}"

            table.add_row(
                f"[bold]{asset.replace('-USD','')}[/]",
                f"[{score_color}]{wtd:.1f}[/]",
                f"[{dir_color}]{direction[:5]}[/]",
                atr_str,
                f"[{atr_ratio_color}]{atr_ratio:.2f}[/]",
                f"{coh_mult:.2f}",
                f"{frsh_mult:.2f}",
                f"{cal_mult:.2f}",
                f"[bold]{eff_mult:.2f}[/]",
                f"[{sweep_color}]{sweep.replace('_side','').upper() if sweep!='none' else '—'}[/]",
                f"[{vpin_color}]{vpin:.2f}[/]"
            )

        return Panel(table, title="[bold #00aaff]▶ SIGNAL ENGINE — LIVE TIER ANALYSIS[/]",
                     style="#e8edf2 on #0d1014", border_style="#00aaff")

    def _build_trade_flow(self) -> Panel:
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#4a5a6a")
        table.add_column("Asset")
        table.add_column("Buy Vol", justify="right")
        table.add_column("Sell Vol", justify="right")
        table.add_column("Delta", justify="right")
        table.add_column("Ratio", justify="right")

        for asset in self.config.assets:
            if asset in self.trade_flow_stores:
                bv = self.trade_flow_stores[asset].buy_volume()
                sv = self.trade_flow_stores[asset].sell_volume()
                delta = self.trade_flow_stores[asset].delta()
                ratio = self.trade_flow_stores[asset].aggressor_ratio()
                
                delta_color = "#00d084" if delta >= 0 else "#ff4757"
                
                table.add_row(
                    asset,
                    f"{bv:,.2f}",
                    f"{sv:,.2f}",
                    f"[{delta_color}]{delta:+,.2f}[/]",
                    f"{ratio:.2f}"
                )

        return Panel(table, title="Trade Flow (60s)", style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_performance_panel(self) -> Panel:
        if not self._perf or not self._journal:
            return Panel(Text("Performance data not initialized", style="dim"), title="Performance")
        
        stats = self._perf.compute(self._journal)
        
        grid = Table.grid(expand=True)
        grid.add_column(style="dim")
        grid.add_column(justify="right")
        
        grid.add_row("Total Trades", str(stats.total_trades))
        grid.add_row("Win Rate", f"{stats.win_rate*100:.1f}%")
        grid.add_row("Profit Factor", f"{stats.profit_factor:.2f}")
        grid.add_row("Total P&L", f"[green if stats.total_pnl_usd > 0]${stats.total_pnl_usd:.2f}[/]")
        grid.add_row("Max Drawdown", f"{stats.max_drawdown_pct:.1f}%")
        
        return Panel(grid, title="Performance")

    def _build_equity_curve(self) -> Panel:
        """
        Builds an ASCII equity curve from history.
        """
        if not self._equity_history:
            return Panel(Text("Waiting for trades...", style="dim"), title="Equity Curve")
            
        balances = [b for t, b in self._equity_history]
        if len(balances) < 2:
            return Panel(Text("Collecting points...", style="dim"), title="Equity Curve")
            
        max_b = max(balances)
        min_b = min(balances)
        spread = max_b - min_b if max_b != min_b else 100
        
        rows = 4
        cols = 20
        chart = [[" " for _ in range(cols)] for _ in range(rows)]
        
        # Resample or take last N
        points = balances[-cols:]
        
        for i, val in enumerate(points):
            y = int((val - min_b) / spread * (rows - 1))
            char = "─"
            if i > 0:
                if points[i] > points[i-1]: char = "╮"
                elif points[i] < points[i-1]: char = "╯"
            chart[rows - 1 - y][i] = char
            
        lines = ["".join(row) for row in chart]
        chart_text = "\n".join(lines)
        
        return Panel(Text(chart_text, style="green"), title="Equity Curve")
    
    def _build_calendar_events_panel(self) -> Panel:
        """Shows upcoming high-impact events."""
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#4a5a6a")
        table.add_column("Event")
        table.add_column("Type")
        table.add_column("Countdown", justify="right")

        if self.calendar_engine:
            now = datetime.now(timezone.utc)
            for ev in self._upcoming_events:
                delta = ev.event_time - now
                hours = delta.total_seconds() / 3600.0
                countdown = f"{hours:.1f}h" if hours > 1 else f"{delta.total_seconds()/60:.0f}m"
                
                # Color code based on impact/proximity
                color = "white"
                if hours < 6: color = "#ff4757"
                elif hours < 24: color = "#f5a623"
                
                table.add_row(ev.name, ev.event_type, f"[{color}]{countdown}[/]")
        else:
            table.add_row("Engine Not Linked", "—", "—")

        return Panel(table, title="UPCOMING EVENTS (72H)", style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_calendar_status_panel(self) -> Panel:
        """Shows current multipliers and regimes for all assets."""
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#4a5a6a")
        table.add_column("Asset")
        table.add_column("Regime")
        table.add_column("Size Mult", justify="right")
        table.add_column("Stop Mult", justify="right")
        table.add_column("Reason")

        if self.calendar_engine:
            states = self._calendar_states
            if states:
                for asset, s in states.items():
                    reason_str = s.reason or ""
                    # Color-code by regime: green=CLEAR, amber=CAUTION, red=BLOCK
                    if s.regime == "BLOCK":
                        regime_color = "#ff4757"
                        asset_str = f"[bold]{asset}[/bold]"
                    elif s.regime == "CAUTION":
                        regime_color = "#f5a623"
                        asset_str = str(asset)
                    else:
                        regime_color = "#4a5a6a"   # muted — CLEAR is normal
                        asset_str = f"[dim]{asset}[/dim]"
                    table.add_row(
                        asset_str,
                        f"[{regime_color}]{str(s.regime)}[/]",
                        f"{float(s.size_multiplier):.2f}x",
                        f"{float(s.stop_atr_multiplier):.1f}x",
                        f"[dim]{reason_str or '—'}[/dim]" if s.regime == "CLEAR" else (reason_str or "—")
                    )
            else:
                table.add_row("[dim]Loading…[/dim]", "—", "—", "—", "—")
        else:
            table.add_row("[dim]Engine not linked[/dim]", "—", "—", "—", "—")

        return Panel(table, title="ASSET CALENDAR STATUS", style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_funding_radar(self) -> Panel:
        """Shows funding rates and arb signals."""
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#4a5a6a")
        table.add_column("Asset")
        table.add_column("Rate", justify="right")
        table.add_column("Carry", justify="right")
        table.add_column("Signal", justify="center")
        table.add_column("Direction")

        for asset, snap in self._funding_snapshots.items():
            try:
                rate = getattr(snap, 'rate', 0.0)
                score = getattr(snap, 'carry_score', 0.0)
                signal = getattr(snap, 'arb_signal', False)
                direction = getattr(snap, 'direction', "none")
                
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
        """Shows open directional positions from position_manager with live unrealized P&L."""
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

        positions = []
        if self._position_manager:
            try:
                positions = self._position_manager.get_all()
            except Exception:
                pass

        now_ms = int(_time.time() * 1000)

        if not positions:
            table.add_row("[dim]—[/]", "[dim]No open positions[/]", "", "", "", "", "", "", "", "")
        else:
            for pos in positions:
                sym = getattr(pos, 'symbol', '?')
                side = getattr(pos, 'side', 'long')
                entry = getattr(pos, 'entry_price', 0.0)
                stop = getattr(pos, 'stop_price', 0.0)
                tp1 = getattr(pos, 'tp1_price', 0.0)
                tp2 = getattr(pos, 'tp2_price', 0.0)
                tp3 = getattr(pos, 'tp3_price', 0.0)
                size = getattr(pos, 'size', 0.0)
                lev = getattr(pos, 'leverage', 1)
                liq = getattr(pos, 'liq_price', 0.0)
                tp1_hit = getattr(pos, 'tp1_hit', False)
                tp2_hit = getattr(pos, 'tp2_hit', False)
                opened_at = getattr(pos, 'opened_at_ms', now_ms)

                # Live mark price for unrealized P&L and liq distance
                mark = entry
                mark_store = self.mark_price_stores.get(sym)
                if mark_store:
                    mp = mark_store.mark_price
                    if mp and mp > 0:
                        mark = mp

                if side == "long":
                    upnl = (mark - entry) * size
                else:
                    upnl = (entry - mark) * size

                dir_color = "#00d084" if side == "long" else "#ff4757"
                pnl_color = "#00d084" if upnl >= 0 else "#ff4757"

                sym_short = sym.replace("-USD", "")

                def _fmt_price(p: float) -> str:
                    if p >= 1000:
                        return f"{p:,.1f}"
                    elif p >= 1:
                        return f"{p:.3f}"
                    return f"{p:.5f}"

                # Stop: highlight missing stop in red
                if stop > 0:
                    stop_str = _fmt_price(stop)
                else:
                    stop_str = "[bold #ff4444]NO STOP[/]"

                # TPs: compact status — show hit markers and prices
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

                # Age in trade
                age_ms = max(0, now_ms - opened_at)
                age_s = age_ms // 1000
                if age_s < 60:
                    age_str = f"{age_s}s"
                elif age_s < 3600:
                    age_str = f"{age_s // 60}m{age_s % 60:02d}s"
                else:
                    age_str = f"{age_s // 3600}h{(age_s % 3600) // 60:02d}m"

                # Liquidation distance %
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
        """Shows last 8 gate-passed trade candidates and their SoDEX outcomes."""
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
                    row_style = ""
                elif status == "REJECTED":
                    err_short = (entry.get("error") or "unknown")[:20]
                    status_str = f"[bold #ff4757]✗ REJECTED[/] [dim]{err_short}[/]"
                    row_style = ""
                else:
                    status_str = "[bold #f5a623]⟳ SENT[/]"
                    row_style = ""

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
        """Shows active funding arb positions."""
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#4a5a6a")
        table.add_column("Asset")
        table.add_column("Direction")
        table.add_column("Size", justify="right")
        table.add_column("Entry", justify="right")
        table.add_column("Current", justify="right")
        table.add_column("Notional", justify="right")
        table.add_column("P&L", justify="right")

        for pos in self._open_arbs:
            try:
                # Defensive field access
                symbol = getattr(pos, "symbol", "unknown")
                direction = getattr(pos, "direction", "none")
                size = getattr(pos, "size", 0.0)
                entry = getattr(pos, "entry_price", 0.0)
                pnl = getattr(pos, "current_pnl", 0.0)
                
                # Get current price from mark store
                mark_store = self.mark_price_stores.get(symbol)
                current_price = 0.0
                if mark_store:
                    mp_data = mark_store.get()
                    if mp_data:
                        current_price = float(mp_data.get("mark_price", 0.0))
                
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
        """Shows real capital allocation from live balance + open positions."""
        total_slots = 30
        perps_balance = self._equity_history[-1][1] if self._equity_history else 0.0
        spot_balance = self._spot_balance

        deployed = 0.0
        if self._position_manager:
            try:
                for p in self._position_manager.get_all():
                    deployed += getattr(p, 'initial_margin', 0.0)
            except Exception:
                pass

        deploy_pct = min(1.0, deployed / perps_balance) if perps_balance > 0 else 0.0
        filled = int(deploy_pct * total_slots)
        free_slots = total_slots - filled
        deploy_color = "#00ff88" if deploy_pct < 0.25 else "#ffcc00" if deploy_pct < 0.60 else "#ff4455"
        bar = f"[{deploy_color}]{'█' * filled}[/][dim]{'░' * free_slots}[/dim]"

        # Show both perps and spot balances — they are INDEPENDENT accounts on SoDEX
        spot_line = (
            f"  [dim]Spot: [bold]${spot_balance:,.2f}[/bold][/dim]"
            if spot_balance > 0 else ""
        )
        content = (
            f"[dim]Alloc:[/dim] [{deploy_color}]{deploy_pct*100:.1f}%[/] deployed  "
            f"[dim]|[/dim]  [bold]${deployed:,.2f}[/bold] / [dim]Perps ${perps_balance:,.2f}[/dim]{spot_line}\n"
            f"[{bar}]"
        )
        return Panel(Text.from_markup(content), style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_stats_panel(self) -> Panel:
        """Combined stats and session info."""
        uptime_s = int(time.time() - self.start_time)
        td = timedelta(seconds=uptime_s)
        hours, rem = divmod(uptime_s, 3600)
        mins, secs = divmod(rem, 60)
        uptime_str = f"{hours:02d}:{mins:02d}:{secs:02d}"

        win_rate = 0.0
        total_pnl = 0.0
        sqn = 0.0
        closed = 0
        if self._perf and self._journal:
            stats = self._perf.compute(self._journal)
            win_rate = stats.win_rate * 100
            total_pnl = stats.total_pnl_usd
            sqn = stats.sqn
            closed = stats.closed_trades

        balance = self._equity_history[-1][1] if self._equity_history else 0.0

        open_positions = []
        if self._position_manager:
            try:
                open_positions = self._position_manager.get_all()
            except Exception:
                pass

        deployed = sum(getattr(p, 'initial_margin', 0.0) for p in open_positions)
        deploy_pct = (deployed / balance * 100) if balance > 0 else 0.0

        mode = self.config.mode.upper()
        mode_color = "#ff4444" if mode == "LIVE" else ("#f5a623" if mode == "TESTNET" else "#888888")
        pnl_color = "#00d084" if total_pnl >= 0 else "#ff4757"
        sqn_color = "#00d084" if sqn >= 2.0 else ("#f5a623" if sqn >= 1.0 else "dim")

        grid = Table.grid(expand=True, padding=(0,1))
        grid.add_column()
        grid.add_column()
        grid.add_column()
        grid.add_column()
        spot_balance = self._spot_balance
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
            ""
        )
        return Panel(grid, title="[bold #00aaff]SESSION[/]",
                     style="#e8edf2 on #0d1014", border_style="#00aaff")

    def _build_chain_intelligence_panel(self) -> Panel:
        """ValueChain on-chain liquidation monitor status (Tier 6)."""
        vc = self._vc_status or {}
        healthy = vc.get("healthy", False)
        last_block = vc.get("last_block", 0)
        events_60s = vc.get("events_60s", 0)
        cascade = vc.get("cascade_active", False)
        rpc = vc.get("rpc_endpoint", "—")
        failures = vc.get("consecutive_failures", 0)

        if cascade:
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
        return Panel(
            Text.from_markup(content),
            title="[bold #aa77ff]⛓ CHAIN INTELLIGENCE[/]",
            style="#e8edf2 on #0d1014",
            border_style=border_style,
        )

    def _build_true_arb_panel(self) -> Panel:
        """Shows open true delta-neutral (spot+perp) arb positions."""
        table = Table(expand=True, style="#e8edf2 on #0d1014",
                      border_style="#aa77ff", show_lines=False)
        table.add_column("Sym", min_width=5)
        table.add_column("Dir", min_width=10)
        table.add_column("Qty", justify="right", min_width=7)
        table.add_column("Entry$", justify="right", min_width=8)
        table.add_column("Held", justify="right", min_width=6)
        table.add_column("Fund$", justify="right", min_width=7)

        positions = self._true_arb_positions or []
        if not positions:
            table.add_row("[dim]—[/]", "[dim]No true arb positions[/]", "", "", "", "")
        else:
            for pos in positions:
                sym = getattr(pos, "symbol", "?").replace("-USD", "")
                direction = getattr(pos, "direction", "?")
                qty = getattr(pos, "spot_qty", 0.0)
                entry = getattr(pos, "spot_entry", 0.0)
                opened_at = getattr(pos, "opened_at", time.time())
                hold_h = (time.time() - opened_at) / 3600
                funding = getattr(pos, "funding_collected_usd", 0.0)

                dir_short = "L↑S↓" if "long_spot" in direction else "S↓L↑"
                dir_color = "#00d084" if "long_spot" in direction else "#ff4757"
                hold_str = f"{hold_h:.1f}h"
                hold_color = "#00d084" if hold_h >= 8 else "#f5a623"

                table.add_row(
                    f"[bold]{sym}[/]",
                    f"[{dir_color}]{dir_short}[/]",
                    f"{qty:.4f}",
                    f"${entry:,.2f}" if entry > 0 else "—",
                    f"[{hold_color}]{hold_str}[/]",
                    f"[bold #00d084]${funding:.4f}[/]" if funding > 0 else "[dim]$0.0000[/]",
                )

        return Panel(
            table,
            title="[bold #aa77ff]⚖ TRUE ARB (SPOT+PERP)[/]",
            style="#e8edf2 on #0d1014",
            border_style="#aa77ff",
        )

    def _build_fee_intelligence_panel(self) -> Panel:
        """
        SoDEX fee tier progress and break-even analysis.
        Driven by SoDEXFeeEngine.tier_summary() — updated daily + on live rate fetch.
        """
        fee = self._fee_data
        if not fee:
            content = "[dim]Fee data loading…[/dim]"
            return Panel(Text.from_markup(content),
                         title="[bold #ffcc00]FEE INTELLIGENCE[/]",
                         style="#e8edf2 on #0d1014", border_style="#ffcc00")

        tier = fee.get("tier", 0)
        max_tier = 4
        vol_14d = fee.get("weighted_14d_volume", 0.0)
        gap = fee.get("volume_to_next_tier", 0.0)
        soso = fee.get("soso_staked", 0.0)
        staking_pct = fee.get("staking_discount_pct", 0.0)

        # Tier progress bar (5 tiers → 5 segments)
        tier_bar = ""
        for i in range(max_tier + 1):
            if i < tier:
                tier_bar += "[bold #00d084]█[/]"
            elif i == tier:
                tier_bar += "[bold #ffcc00]█[/]"
            else:
                tier_bar += "[dim]░[/dim]"

        tier_color = "#00d084" if tier >= 3 else ("#ffcc00" if tier >= 1 else "dim")
        next_str = f"[dim]${gap:,.0f} to Tier {tier+1}[/dim]" if gap > 0 else "[bold #00d084]MAX TIER[/]"

        perp_t = fee.get("perps_taker_pct", 0.0)
        perp_m = fee.get("perps_maker_pct", 0.0)
        spot_t = fee.get("spot_taker_pct", 0.0)
        spot_m = fee.get("spot_maker_pct", 0.0)
        arb_rt = fee.get("arb_round_trip_maker_pct", 0.0)
        arb_be = fee.get("arb_break_even_3periods_maker_pct", 0.0)

        staking_str = (
            f"[bold]{soso:,.0f}[/bold] SOSO → [bold #00d084]{staking_pct:.0f}%[/] off"
            if soso > 0
            else "[dim]0 SOSO staked[/dim]"
        )

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
