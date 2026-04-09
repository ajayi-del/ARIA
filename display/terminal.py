import time
import asyncio
import structlog
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from datetime import datetime, timedelta
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
        journal: TradeJournal = None,
        perf: PerformanceTracker = None
    ):
        self.config = config
        self.orderbook_stores = orderbook_stores
        self.mark_price_stores = mark_price_stores
        self.candle_buffers = candle_buffers
        self.trade_flow_stores = trade_flow_stores
        self.health_check = health_check
        self.market_engine = market_engine
        self._journal = journal
        self._perf = perf
        
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
            Layout(name="center", ratio=1),
            Layout(name="right", ratio=1)
        )

        layout["header"].update(self._build_header())
        layout["left"].update(self._build_assets_panel())
        
        layout["center"].split(
            Layout(name="health", size=10),
            Layout(name="funding_radar", ratio=2),
            Layout(name="arb_positions", ratio=1)
        )
        layout["center"]["health"].update(self._build_health_panel())
        layout["center"]["funding_radar"].update(self._build_funding_radar())
        layout["center"]["arb_positions"].update(self._build_arb_positions())
        
        layout["right"].split(
            Layout(name="trade_flow", ratio=2),
            Layout(name="equity_curve", ratio=1),
            Layout(name="stats", ratio=1)
        )
        layout["right"]["trade_flow"].update(self._build_trade_flow())
        layout["right"]["equity_curve"].update(self._build_equity_curve())
        layout["right"]["stats"].update(self._build_performance_panel())

        return layout

    def _build_header(self) -> Panel:
        now = datetime.now().strftime("%H:%M:%S")
        mode = self.config.mode.upper()
        if mode == "PAPER":
            mode_text = f"[#7f8c8d]{mode}[/]"
            style = "#e8edf2 on #0d1014"
            title = f"ARIA v1.0 — {mode} Trading"
        elif mode == "TESTNET":
            mode_text = f"[#ffffff]{mode}[/]"
            style = "#e8edf2 on #0d1014"
            title = f"ARIA v1.0 — {mode} Deployment"
        else:
            mode_text = f"⚡ [bold #ff4444]LIVE[/]"
            style = "white on #880000"
            title = f"ARIA v1.0 — MAINNET"
            
        header_text = Text.from_markup(f"{title} | {now} | {mode_text}")
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

        bar_len = 10
        if imb < 0:
            filled = int(abs(imb) * bar_len)
            bar_color = "#ff4757" # red
            bar = f"[{bar_color}]{'█' * filled}[/]{'░' * (bar_len - filled)}"
        else:
            filled = int(imb * bar_len)
            bar_color = "#00d084" # green
            bar = f"[{bar_color}]{'█' * filled}[/]{'░' * (bar_len - filled)}"

        if ob_age < 200:
            ob_age_color = "#00d084"
        elif ob_age < 500:
            ob_age_color = "#f5a623"
        else:
            ob_age_color = "#ff4757"

        delta_color = "#00d084" if buy_delta >= 0 else "#ff4757"

        content = (
            f"Row 1: Last Price: {last_price_str}\n"
            f"Row 2: Mark: {mark_price_str} | Local: {last_price_str} | Div: {div_str}\n"
            f"Row 3: Imbalance: {bar} ({imb:+.2f})\n"
            f"Row 4: OB age: [{ob_age_color}]{ob_age}ms[/]\n"
            f"Row 5: Buy Delta (60s): [{delta_color}]{buy_delta:+,.2f}[/]"
        )

        return Panel(Text.from_markup(content), title=asset, style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_health_panel(self) -> Panel:
        health = self.health_check()
        
        spot_conn = "[#00d084]● connected[/]" if health["spot_connected"] else "[#ff4757]✕ down[/]"
        perps_conn = "[#00d084]● connected[/]" if health["perps_connected"] else "[#ff4757]✕ down[/]"

        content = (
            f"Spot WebSocket:  {spot_conn}\n"
            f"Perps WebSocket: {perps_conn}\n"
            f"Messages total: {health['total_messages_received']}\n\n"
        )
        
        for asset in self.config.assets:
            age = 999999
            if asset in self.orderbook_stores:
                age = self.orderbook_stores[asset].age_ms()
            
            color = "#00d084" if age < 500 else "#ff4757"
            content += f"{asset} age: [{color}]{age}ms[/]\n"

        return Panel(Text.from_markup(content), title="Feed Health", style="#e8edf2 on #0d1014", border_style="#4a5a6a")

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
    
    def _build_stats(self) -> Panel:
        uptime = int(time.time() - self.start_time)
        td = timedelta(seconds=uptime)
        
        health = self.health_check()
        
        content = (
            f"Started: {datetime.fromtimestamp(self.start_time).strftime('%H:%M:%S')}\n"
            f"Uptime: {td}\n"
            f"Messages rcvd: {health['total_messages_received']}\n"
        )
        
        return Panel(Text.from_markup(content), title="Session", style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_funding_radar(self) -> Panel:
        """Build funding radar panel with scores and signals"""
        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#4a5a6a")
        table.add_column("Asset")
        table.add_column("Rate", justify="right")
        table.add_column("24h Avg", justify="right")
        table.add_column("Score", justify="right")
        table.add_column("Signal")

        for asset in self.config.assets:
            snap = self._funding_snapshots.get(asset)
            if snap:
                score = snap.carry_score
                # Color logic
                if score >= 2.5:
                    score_color = "#f5a623" # orange
                    signal_text = f"[bold #f5a623]SHORT ARB ←[/]"
                elif score <= -2.5:
                    score_color = "#f5a623" # orange
                    signal_text = f"[bold #f5a623]LONG ARB ←[/]"
                elif score > 0:
                    score_color = "#00d084" # green
                    signal_text = "—"
                elif score < 0:
                    score_color = "#ff4757" # red
                    signal_text = "—"
                else:
                    score_color = "white"
                    signal_text = "—"
                
                table.add_row(
                    asset,
                    f"{snap.rate:.3f}%",
                    f"{snap.rate_24h_avg:.3f}%",
                    f"[{score_color}]{score:+.1f}[/]",
                    signal_text
                )
            else:
                table.add_row(asset, "0.000%", "0.000%", "0.0", "—")

        return Panel(table, title="FUNDING RADAR", style="#e8edf2 on #0d1014", border_style="#4a5a6a")

    def _build_arb_positions(self) -> Panel:
        """Build arb positions panel"""
        if not self._open_arbs:
            return Panel(Text("No arb positions open", justify="center"), title="ARB POSITIONS", style="#e8edf2 on #0d1014", border_style="#4a5a6a")

        table = Table(expand=True, style="#e8edf2 on #0d1014", border_style="#4a5a6a")
        table.add_column("Symbol")
        table.add_column("Direction")
        table.add_column("Entry Rate")
        table.add_column("Collected")
        table.add_column("Hours")

        now_ms = int(time.time() * 1000)
        for arb in self._open_arbs:
            hours = (now_ms - arb.opened_at_ms) / 3600000
            table.add_row(
                arb.symbol,
                f"[bold #f5a623]{arb.direction.upper().replace('_', ' ')}[/]",
                f"{arb.entry_rate:.3f}%",
                f"${arb.funding_collected:.2f}",
                f"{hours:.1f}h"
            )

        return Panel(table, title="ARB POSITIONS", style="#e8edf2 on #0d1014", border_style="#4a5a6a")
