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

from core.config import Settings
from core.market_engine import MarketEngine

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
        system_state = None  # SystemStateManager
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
        
        # Phase 4.5 Data
        self._funding_snapshots = {}
        self._open_arbs = []
        
        # Phase 6 Data
        self._equity_history = []  # List of (timestamp, balance)

        self.start_time = time.time()
        self._task = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def update_funding(self, snapshots: dict) -> None:
        """Update funding snapshot data for display"""
        self._funding_snapshots = snapshots

    def update_arbs(self, arbs: list) -> None:
        """Update open arb positions data for display"""
        self._open_arbs = arbs

    def update_equity(self, balance: float) -> None:
        """Update equity history for ASCII chart"""
        self._equity_history.append((time.time(), balance))
        # Keep last 50 points
        if len(self._equity_history) > 50:
            self._equity_history.pop(0)

    async def _run(self) -> None:
        with Live(self.generate_layout(), refresh_per_second=1, screen=True) as live:
            try:
                while True:
                    live.update(self.generate_layout())
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                pass

    def generate_layout(self) -> Layout:
        layout = Layout()
        layout.split(
            Layout(name="header", size=3),
            Layout(name="body")
        )
        layout["body"].split_row(
            Layout(name="left", ratio=1),
            Layout(name="center", ratio=1.2),
            Layout(name="right", ratio=1)
        )

        layout["header"].update(self._build_header())
        layout["left"].update(self._build_assets_panel())
        
        layout["center"].split(
            Layout(name="intelligence", ratio=1.5),
            Layout(name="calendar_status", ratio=1),
            Layout(name="funding_radar", ratio=1),
            Layout(name="arb_positions", ratio=1)
        )
        layout["center"]["intelligence"].update(self._build_intelligence_panel())
        layout["center"]["calendar_status"].update(self._build_calendar_status_panel())
        layout["center"]["funding_radar"].update(self._build_funding_radar())
        layout["center"]["arb_positions"].update(self._build_arb_positions())
        
        layout["right"].split(
            Layout(name="trade_flow", ratio=2),
            Layout(name="calendar_events", ratio=1),
            Layout(name="allocation", size=5),
            Layout(name="equity_curve", ratio=1),
            Layout(name="stats_row", size=6)
        )
        layout["right"]["trade_flow"].update(self._build_trade_flow())
        layout["right"]["calendar_events"].update(self._build_calendar_events_panel())
        layout["right"]["allocation"].update(self._build_allocation_panel())
        layout["right"]["equity_curve"].update(self._build_equity_curve())
        layout["right"]["stats_row"].update(self._build_stats_panel())

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
            state = self.market_engine.get_market_state(asset) if self.market_engine else None
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
            upcoming = self.calendar_engine.event_store.get_upcoming(hours_ahead=72, now_utc=now)
            for ev in upcoming:
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
            states = self.calendar_engine.get_states_all(self.config.assets)
            for asset, s in states.items():
                regime_color = "#00d084" if s.regime == "CLEAR" else ("#f5a623" if s.regime == "CAUTION" else "#ff4757")
                table.add_row(
                    asset,
                    f"[{regime_color}]{s.regime}[/]",
                    f"{s.size_multiplier:.2f}x",
                    f"{s.stop_atr_multiplier:.1f}x",
                    s.reason
                )
        else:
            table.add_row("Engine Not Linked", "—", "—", "—", "—")

        return Panel(table, title="ASSET CALENDAR STATUS", style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_stats_panel(self) -> Panel:
        """Combined stats and session info."""
        uptime = int(time.time() - self.start_time)
        td = timedelta(seconds=uptime)
        health = self.health_check()
        
        # Performance bits
        win_rate = 0.0
        pnl = 0.0
        if self._perf and self._journal:
            stats = self._perf.compute(self._journal)
            win_rate = stats.win_rate * 100
            pnl = stats.total_pnl_usd

        content = (
            f"Uptime: {td} | msgs: {health['total_messages_received']}\n"
            f"Win Rate: [bold yellow]{win_rate:.1f}%[/] | Total P&L: [green if pnl >= 0]${pnl:.2f}[/]\n"
            f"Mode: [bold white]{self.config.mode.upper()}[/] | Source: [dim]{self.config.data_source}[/]"
        )
        return Panel(Text.from_markup(content), title="SESSION STATS", style="#e8edf2 on #0d1014", border_style="#4a5a6a")
