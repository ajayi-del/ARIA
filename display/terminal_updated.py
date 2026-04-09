import os
import asyncio
import structlog
from typing import Dict, Any
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich.align import Align
from datetime import datetime
from core.config import Settings
from core.market_engine import MarketEngine
from data.orderbook_store import OrderbookStore
from data.mark_price_store import MarkPriceStore
from data.candle_buffer import CandleBuffer
from data.trade_flow_store import TradeFlowStore

logger = structlog.get_logger(__name__)


class TerminalDisplay:
    def __init__(
        self,
        config: Settings,
        orderbook_stores: Dict[str, OrderbookStore],
        mark_price_stores: Dict[str, MarkPriceStore],
        candle_buffers: Dict[str, Dict[str, CandleBuffer]],
        trade_flow_stores: Dict[str, TradeFlowStore],
        health_check,
        market_engine: MarketEngine = None  # NEW
    ):
        self.config = config
        self.orderbook_stores = orderbook_stores
        self.mark_price_stores = mark_price_stores
        self.candle_buffers = candle_buffers
        self.trade_flow_stores = trade_flow_stores
        self.health_check = health_check
        self.market_engine = market_engine  # NEW
        
        self.console = Console()
        self.layout = Layout()
        self._running = False
        self._display_task = None
        
        # Setup layout
        self._setup_layout()

    def _setup_layout(self):
        """Setup the terminal layout"""
        self.layout.split(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3)
        )
        
        self.layout["main"].split_row(
            Layout(name="market_data", ratio=1),
            Layout(name="signals", ratio=1)
        )
        
        self.layout["market_data"].split_column(
            Layout(name="orderbook", ratio=1),
            Layout(name="candles", ratio=1),
            Layout(name="trades", ratio=1)
        )

    async def start(self) -> None:
        """Start the terminal display"""
        logger.info("Starting Terminal Display")
        self._running = True
        self._display_task = asyncio.create_task(self._display_loop())

    async def stop(self) -> None:
        """Stop the terminal display"""
        logger.info("Stopping Terminal Display")
        self._running = False
        if self._display_task:
            self._display_task.cancel()
            try:
                await self._display_task
            except asyncio.CancelledError:
                pass

    async def _display_loop(self) -> None:
        """Main display loop"""
        with Live(self.layout, console=self.console, refresh_per_second=1) as live:
            while self._running:
                try:
                    # Update all panels
                    self._update_header()
                    self._update_footer()
                    self._update_market_data()
                    self._update_signals()  # NEW
                    
                    live.update(self.layout)
                    await asyncio.sleep(1)
                    
                except Exception as e:
                    logger.error(f"Error in display loop: {e}")
                    await asyncio.sleep(1)

    def _update_header(self):
        """Update header panel"""
        header_text = Text(f"ARIA Trading System - {self.config.mode.upper()} Mode", style="bold blue")
        header_text.append(f" | Assets: {', '.join(self.config.assets)}", style="white")
        header_text.append(f" | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", style="dim")
        
        self.layout["header"].update(
            Panel(Align.center(header_text), style="bold black on blue")
        )

    def _update_footer(self):
        """Update footer panel"""
        health = self.health_check()
        
        footer_text = Text("Health: ", style="white")
        
        if health.get("spot_connected", False):
            footer_text.append("SPOT:ON ", style="green")
        else:
            footer_text.append("SPOT:OFF ", style="red")
            
        if health.get("perps_connected", False):
            footer_text.append("PERPS:ON ", style="green")
        else:
            footer_text.append("PERPS:OFF ", style="red")
        
        footer_text.append(f" | Messages: {health.get('total_messages_received', 0)}", style="white")
        
        # Add market engine status if available
        if self.market_engine:
            engine_status = self.market_engine.get_engine_status()
            footer_text.append(f" | Engine: {'ON' if engine_status['is_running'] else 'OFF'}", style="green" if engine_status['is_running'] else "red")
            footer_text.append(f" | Signals: {engine_status['valid_signals_count']}", style="yellow")
        
        self.layout["footer"].update(
            Panel(Align.center(footer_text), style="black on white")
        )

    def _update_market_data(self):
        """Update market data panels"""
        for asset in self.config.assets:
            if asset in self.orderbook_stores:
                self._update_orderbook(asset)
            if asset in self.candle_buffers:
                self._update_candles(asset)
            if asset in self.trade_flow_stores:
                self._update_trades(asset)

    def _update_orderbook(self, symbol: str):
        """Update orderbook panel"""
        store = self.orderbook_stores[symbol]
        
        table = Table(title=f"{symbol} Orderbook", show_header=True, header_style="bold magenta")
        table.add_column("Bid Price", justify="right", style="green")
        table.add_column("Bid Size", justify="right", style="green")
        table.add_column("Ask Price", justify="right", style="red")
        table.add_column("Ask Size", justify="right", style="red")
        
        # Get top 5 levels
        bids = list(store.bids.items())[:5]
        asks = list(store.asks.items())[:5]
        
        for i in range(5):
            bid_price = bids[i][0] if i < len(bids) else ""
            bid_size = bids[i][1] if i < len(bids) else ""
            ask_price = asks[i][0] if i < len(asks) else ""
            ask_size = asks[i][1] if i < len(asks) else ""
            
            table.add_row(
                f"{bid_price:.2f}" if bid_price else "",
                f"{bid_size:.2f}" if bid_size else "",
                f"{ask_price:.2f}" if ask_price else "",
                f"{ask_size:.2f}" if ask_size else ""
            )
        
        self.layout["orderbook"].update(Panel(table, title=f"{symbol} Orderbook"))

    def _update_candles(self, symbol: str):
        """Update candles panel"""
        candles_1m = self.candle_buffers[symbol].get("1m")
        
        if not candles_1m or not candles_1m.candles:
            self.layout["candles"].update(Panel(Text("No candle data", style="dim"), title=f"{symbol} Candles"))
            return
        
        table = Table(title=f"{symbol} 1m Candles", show_header=True, header_style="bold cyan")
        table.add_column("Time", justify="center")
        table.add_column("Open", justify="right")
        table.add_column("High", justify="right")
        table.add_column("Low", justify="right")
        table.add_column("Close", justify="right")
        table.add_column("Volume", justify="right")
        
        # Show last 5 candles
        for candle in candles_1m.candles[-5:]:
            time_str = datetime.fromtimestamp(candle.close_time / 1000).strftime("%H:%M")
            table.add_row(
                time_str,
                f"{candle.open:.2f}",
                f"{candle.high:.2f}",
                f"{candle.low:.2f}",
                f"{candle.close:.2f}",
                f"{candle.volume:.1f}"
            )
        
        self.layout["candles"].update(Panel(table, title=f"{symbol} Candles"))

    def _update_trades(self, symbol: str):
        """Update trades panel"""
        store = self.trade_flow_stores[symbol]
        
        if not store.trades:
            self.layout["trades"].update(Panel(Text("No trade data", style="dim"), title=f"{symbol} Trades"))
            return
        
        table = Table(title=f"{symbol} Recent Trades", show_header=True, header_style="bold yellow")
        table.add_column("Time", justify="center")
        table.add_column("Price", justify="right")
        table.add_column("Size", justify="right")
        table.add_column("Side", justify="center")
        
        # Show last 5 trades
        for trade in store.trades[-5:]:
            time_str = datetime.fromtimestamp(trade.timestamp_ms / 1000).strftime("%H:%M:%S")
            side_style = "green" if trade.side == "buy" else "red"
            
            table.add_row(
                time_str,
                f"{trade.price:.2f}",
                f"{trade.size:.2f}",
                Text(trade.side.upper(), style=side_style)
            )
        
        self.layout["trades"].update(Panel(table, title=f"{symbol} Trades"))

    def _update_signals(self):
        """Update signals panel (NEW)"""
        if not self.market_engine:
            self.layout["signals"].update(Panel(Text("Market Engine not available", style="dim"), title="Trading Signals"))
            return
        
        # Get all market states
        market_states = self.market_engine.get_all_market_states()
        
        if not market_states:
            self.layout["signals"].update(Panel(Text("No signals available", style="dim"), title="Trading Signals"))
            return
        
        # Create signals table
        table = Table(title="Trading Signals", show_header=True, header_style="bold green")
        table.add_column("Symbol", justify="center")
        table.add_column("Direction", justify="center")
        table.add_column("Coherence", justify="center")
        table.add_column("Size", justify="center")
        table.add_column("Macro", justify="center")
        table.add_column("Regime", justify="center")
        table.add_column("MAG", justify="center")
        
        for symbol, state in market_states.items():
            # Style based on signal validity
            if state.is_valid_signal():
                direction_style = "green" if state.trade_direction == "long" else "red"
                coherence_style = "green" if state.coherence_score >= 4 else "yellow"
            else:
                direction_style = "dim"
                coherence_style = "dim"
            
            # MAG indicator
            mag_indicator = "ON" if state.mag_active else "OFF"
            mag_style = "green" if state.mag_active else "dim"
            
            table.add_row(
                symbol,
                Text(state.trade_direction.upper(), style=direction_style),
                Text(f"{state.coherence_score}/6", style=coherence_style),
                Text(f"{state.size_multiplier:.1f}x", style=coherence_style),
                Text(state.macro_bias[:3].upper(), style="cyan"),
                Text(state.regime.replace("_", " ").title()[:8], style="blue"),
                Text(mag_indicator, style=mag_style)
            )
        
        # Add signal summary below
        valid_signals = self.market_engine.get_valid_signals()
        summary_text = f"Valid Signals: {len(valid_signals)} | "
        
        if valid_signals:
            long_signals = [s for s in valid_signals if s.trade_direction == "long"]
            short_signals = [s for s in valid_signals if s.trade_direction == "short"]
            summary_text += f"Long: {len(long_signals)} | Short: {len(short_signals)}"
        else:
            summary_text += "No active signals"
        
        # Create panel with table and summary
        panel_content = Table.grid()
        panel_content.add_row(table)
        panel_content.add_row("")
        panel_content.add_row(Align.center(Text(summary_text, style="bold yellow")))
        
        self.layout["signals"].update(Panel(panel_content, title="Trading Signals"))

    def display_alert(self, message: str, level: str = "info"):
        """Display an alert message"""
        styles = {
            "info": "blue",
            "warning": "yellow",
            "error": "red",
            "success": "green"
        }
        
        style = styles.get(level, "white")
        alert_text = Text(f"ALERT: {message}", style=style)
        
        # This could be integrated into the layout for persistent alerts
        self.console.print(Panel(alert_text, style=f"bold {style}"))
