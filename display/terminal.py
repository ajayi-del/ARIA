import time
import asyncio
from datetime import datetime, timedelta
from typing import Callable

from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.live import Live
from rich.text import Text

from core.config import Settings

class TerminalDisplay:
    def __init__(
        self,
        config: Settings,
        orderbook_stores: dict,
        mark_price_stores: dict,
        candle_buffers: dict,
        trade_flow_stores: dict,
        health_check: Callable[[], dict]
    ):
        self.config = config
        self.orderbook_stores = orderbook_stores
        self.mark_price_stores = mark_price_stores
        self.candle_buffers = candle_buffers
        self.trade_flow_stores = trade_flow_stores
        self.health_check = health_check

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
        layout["center"].update(self._build_health_panel())
        
        layout["right"].split(
            Layout(name="trade_flow", ratio=2),
            Layout(name="stats", ratio=1)
        )
        layout["right"]["trade_flow"].update(self._build_trade_flow())
        layout["right"]["stats"].update(self._build_stats())

        return layout

    def _build_header(self) -> Panel:
        now = datetime.now().strftime("%H:%M:%S")
        mode = self.config.mode.upper()
        if mode == "PAPER":
            mode_text = f"[#f5a623]{mode}[/]"
        elif mode == "TESTNET":
            mode_text = f"[#3d9eff]{mode}[/]"
        else:
            mode_text = f"[#00d084]{mode}[/]"
            
        header_text = Text.from_markup(f"ARIA v0.1 — Phase 1: Data Collection | {now} | {mode_text}")
        header_text.justify = "center"
        return Panel(header_text, style="#e8edf2 on #0d1014")

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
