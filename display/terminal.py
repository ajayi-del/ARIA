import time
import asyncio
import structlog
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
        interpreter = None  # IntelligenceInterpreter
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
        
        # Phase 4.5 Data
        self._funding_snapshots = {}
        self._open_arbs = []
        
        # v1.3 Cached Async Data
        self._calendar_states = {}
        self._upcoming_events = []
        
        self._equity_history = []  # List of (timestamp, balance)
        self.start_time = time.time()
        self._task = None

    def update_equity(self, balance: float) -> None:
        import time
        self._equity_history.append((int(time.time() * 1000), balance))
        # Keep last 200 points only
        if len(self._equity_history) > 200:
            self._equity_history = self._equity_history[-200:]

    def update_funding(self, snapshots: dict) -> None:
        """Updates the internal funding radar data."""
        self._funding_snapshots = snapshots

    def update_arbs(self, arbs: list) -> None:
        """Updates the internal active arbitrage positions."""
        self._open_arbs = arbs

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
            Layout(name="calendar_status", ratio=1),
            Layout(name="funding_radar", ratio=1),
            Layout(name="arb_positions", ratio=1)
        )
        layout["center"]["intelligence"].update(self._safe_panel(self._build_intelligence_panel, "Intelligence"))
        layout["center"]["calendar_status"].update(self._safe_panel(self._build_calendar_status_panel, "Calendar Status"))
        layout["center"]["funding_radar"].update(self._safe_panel(self._build_funding_radar, "Funding Radar"))
        layout["center"]["arb_positions"].update(self._safe_panel(self._build_arb_positions, "Arb Positions"))
        
        layout["right"].split(
            Layout(name="trade_flow", ratio=2),
            Layout(name="calendar_events", ratio=1),
            Layout(name="allocation", size=5),
            Layout(name="equity_curve", ratio=1),
            Layout(name="stats_row", size=6)
        )
        layout["right"]["trade_flow"].update(self._safe_panel(self._build_trade_flow, "Trade Flow"))
        layout["right"]["calendar_events"].update(self._safe_panel(self._build_calendar_events_panel, "Calendar Events"))
        layout["right"]["allocation"].update(self._safe_panel(self._build_allocation_panel, "Allocation"))
        layout["right"]["equity_curve"].update(self._safe_panel(self._build_equity_curve, "Equity Curve"))
        layout["right"]["stats_row"].update(self._safe_panel(self._build_stats_panel, "Stats"))

        return layout

    def _build_header(self) -> Panel:
        now = datetime.now().strftime("%H:%M:%S")
        mode = self.config.mode.upper()
        if mode == "PAPER":
            mode_text = f"[#7f8c8d]{mode}[/]"
            style = "#e8edf2 on #0d1014"
            title = f"ARIA v1.3 — 8 Assets"
        elif mode == "TESTNET":
            mode_text = f"[#ffffff]{mode}[/]"
            style = "#e8edf2 on #0d1014"
            title = f"ARIA v1.3 — 8 Assets"
        else:
            mode_text = f"⚡ [bold #ff4444]LIVE[/]"
            style = "white on #880000"
            title = f"ARIA v1.3 — 8 Assets"
            
        global_phase = self.system_state.get_global_phase().value.upper() if self.system_state else "OFFLINE"
        phase_color = "#00d084" if global_phase == "TRADING" else ("#f5a623" if global_phase == "READY" else "#ff4757")
            
        header_text = Text.from_markup(f"{title} | {now} | {mode_text} | [{phase_color}]{global_phase}[/]")
        header_text.justify = "center"
        return Panel(header_text, style=style)

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

        # Row 3: OB age | Score | Direction
        state = self.market_engine.get_market_state(asset) if self.market_engine else None
        w_score = state.weighted_score if state and hasattr(state, 'weighted_score') else 0.0
        direction = state.trade_direction.upper() if state and hasattr(state, 'trade_direction') else "NONE"
        
        ob_age_color = "#00d084" if ob_age < 200 else ("#f5a623" if ob_age < 500 else "#ff4757")
        row3 = f"OB: [{ob_age_color}]{ob_age}ms[/] | Score: [bold yellow]{w_score:.1f}[/] | Dir: {direction}"

        # Row 4: Warm-up Status
        warmup_row = ""
        if self.system_state:
            status = self.system_state.get_warmup_status().get(asset, {})
            count = status.get("count", 0)
            target = status.get("target", 50)
            phase = status.get("phase", "unknown").upper()
            p_color = "#00d084" if phase == "TRADING" else ("#f5a623" if phase == "READY" else "#ff4757")
            
            warmup_row = f"\nWarm-up: [{p_color}]{phase}[/] ({count}/{target})"

        content = (
            f"L: {last_price_str} | M: {mark_price_str} | D: {div_str}\n"
            f"{row2}\n"
            f"{row3}"
            f"{warmup_row}"
        )

        return Panel(Text.from_markup(content), style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_intelligence_panel(self) -> Panel:
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#4a5a6a")
        table.add_column("Asset")
        table.add_column("Raw", justify="right")
        table.add_column("Wtd", justify="right")
        table.add_column("Dir")
        table.add_column("Clust")
        table.add_column("VPIN", justify="right")

        for asset in self.config.assets:
            # v1.3: Pivot to Interpreter for live intelligence
            state = None
            if self.interpreter:
                state = self.interpreter.get_market_state(asset)
            elif self.market_engine:
                state = self.market_engine.get_market_state(asset)
                
            raw = state.raw_score if state and hasattr(state, 'raw_score') else 0
            wtd = state.weighted_score if state and hasattr(state, 'weighted_score') else 0.0
            direction = state.trade_direction.upper() if state and hasattr(state, 'trade_direction') else "NONE"
            cluster = "YES" if state and getattr(state, 'cluster_validated', False) else "NO"
            vpin = getattr(state, 'vpin', 0.0)

            table.add_row(
                asset,
                str(raw),
                f"[bold yellow]{wtd:.1f}[/]",
                direction,
                cluster,
                f"{vpin:.2f}"
            )

        return Panel(table, title="MULTI-ASSET INTELLIGENCE", style="#e8edf2 on #0d1014", border_style="#4a5a6a")

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
            for asset, s in states.items():
                regime_color = "#00d084" if s.regime == "CLEAR" else ("#f5a623" if s.regime == "CAUTION" else "#ff4757")
                table.add_row(
                    str(asset),
                    f"[{regime_color}]{str(s.regime)}[/]",
                    f"{float(s.size_multiplier):.2f}x",
                    f"{float(s.stop_atr_multiplier):.1f}x",
                    str(s.reason or "—")
                )
        else:
            table.add_row("Engine Not Linked", "—", "—", "—", "—")

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
        """Shows asset allocation bar."""
        total_slots = 30
        content = "Allocation: ["
        
        # Simple mock or pull from position manager
        # For now, just show a dim bar if no positions
        bar = "░" * total_slots
        content += f"{bar}] 0% Utilized"
        
        return Panel(Text.from_markup(content), style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_stats_panel(self) -> Panel:
        """Combined stats and session info."""
        uptime = int(time.time() - self.start_time)
        td = timedelta(seconds=uptime)
        
        # Health check
        health = self.health_check()
        
        # Performance bits
        win_rate = 0.0
        total_pnl = 0.0
        if self._perf and self._journal:
            stats = self._perf.compute(self._journal)
            win_rate = stats.win_rate * 100
            total_pnl = stats.total_pnl_usd

        # Get latest balance from equity history (cached asynchronously in main.py)
        # v1.3: In production, balance should come from the latest known equity
        balance = self.config.paper_starting_balance
        if self._equity_history:
            balance = self._equity_history[-1][1]
        elif self._paper_client and hasattr(self._paper_client, 'last_balance'):
            balance = self._paper_client.last_balance
        
        # Calculate deployed capital
        open_positions = []
        if self._position_manager:
            try:
                open_positions = self._position_manager.get_all()
            except Exception:
                open_positions = []
        
        deployed = sum(getattr(p, 'initial_margin', 0.0) for p in open_positions)
        available = balance - deployed
        
        deploy_pct = (deployed / balance * 100) if balance > 0 else 0.0
        
        mode = self.config.mode.upper()
        source = self.config.data_source

        stats_text = (
            f"Balance: ${balance:,.2f}\n"
            f"Deployed: ${deployed:,.2f} ({deploy_pct:.1f}%) | Available: ${available:,.2f}\n"
            f"Win Rate: [bold yellow]{win_rate:.1f}%[/] | Total P&L: [green if total_pnl >= 0]${total_pnl:+.2f}[/]\n"
            f"Mode: [bold white]{mode}[/] | Source: [dim]{source}[/] | Uptime: {td}"
        )
        return Panel(Text.from_markup(stats_text), title="SESSION STATS", style="#e8edf2 on #0d1014", border_style="#4a5a6a")
