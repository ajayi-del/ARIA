import structlog
import asyncio
from typing import Dict, Any, List, Optional
from datetime import datetime
from core.signal_generator import SignalGenerator
from core.data_processor import DataProcessor
from core.market_state import MarketState
from data.orderbook_store import OrderbookStore
from data.mark_price_store import MarkPriceStore
from data.candle_buffer import CandleBuffer
from data.trade_flow_store import TradeFlowStore

logger = structlog.get_logger(__name__)


class MarketEngine:
    """Main market analysis engine that coordinates all components"""
    
    def __init__(self, config):
        self.config = config
        self.signal_generator = SignalGenerator()
        self.data_processor = DataProcessor()
        self.signal_generator.microstructure_analyzer.price_history = self.signal_generator.structure_analyzer.price_history

        
        # Store market states for all symbols
        self.market_states: Dict[str, MarketState] = {}
        self.last_update_time: Dict[str, int] = {}
        
        # Engine state
        self.is_running = False
        self.analysis_task: Optional[asyncio.Task] = None
        
    async def start(self) -> None:
        """Start the market engine"""
        logger.info("Starting Market Engine")
        self.is_running = True
        
        # Start analysis loop
        self.analysis_task = asyncio.create_task(self._analysis_loop())
        
    async def stop(self) -> None:
        """Stop the market engine"""
        logger.info("Stopping Market Engine")
        self.is_running = False
        
        if self.analysis_task:
            self.analysis_task.cancel()
            try:
                await self.analysis_task
            except asyncio.CancelledError:
                pass
    
    async def _analysis_loop(self) -> None:
        """Main analysis loop"""
        while self.is_running:
            try:
                # Process all configured symbols
                for symbol in self.config.assets:
                    await self._analyze_symbol(symbol)
                
                # Wait for next iteration
                await asyncio.sleep(self.config.loop_interval_ms / 1000.0)
                
            except Exception as e:
                logger.error(f"Error in analysis loop: {e}")
                await asyncio.sleep(1.0)  # Wait before retrying
    
    async def _analyze_symbol(self, symbol: str) -> None:
        """Analyze a single symbol"""
        try:
            # This would normally get data from the actual stores
            # For now, we'll use mock data
            market_data = await self._get_market_data(symbol)
            
            # Process the data
            processed_data = self.data_processor.process_market_data(
                symbol,
                market_data["orderbook_store"],
                market_data["mark_price_store"],
                market_data["candle_buffers"],
                market_data["trade_flow_store"]
            )
            
            # Generate market state
            market_state = self.signal_generator.generate_market_state(symbol, processed_data)
            
            # Store the market state
            self.market_states[symbol] = market_state
            self.last_update_time[symbol] = market_state.timestamp_ms
            
            # Log significant signals
            if market_state.is_valid_signal():
                logger.info(
                    f"Valid signal generated for {symbol}",
                    direction=market_state.trade_direction,
                    coherence=market_state.coherence_score,
                    size_multiplier=market_state.size_multiplier
                )
            
        except Exception as e:
            logger.error(f"Error analyzing symbol {symbol}: {e}")
    
    async def _get_market_data(self, symbol: str) -> Dict[str, Any]:
        """Get market data for a symbol (mock implementation)"""
        # This would normally get real data from the stores
        # For now, create mock data
        
        # Mock orderbook store
        orderbook_store = OrderbookStore(symbol)
        base_price = 50000.0 if symbol == "BTC" else 3000.0 if symbol == "ETH" else 100.0
        
        # Add some mock orderbook data
        bids = []
        asks = []
        for i in range(10):
            bid_price = base_price - (i * 10)
            ask_price = base_price + (i * 10)
            bids.append((bid_price, 10.0 - i))
            asks.append((ask_price, 10.0 - i))
        
        orderbook_store.update(bids, asks, int(datetime.now().timestamp() * 1000))
        
        # Mock mark price store
        mark_price_store = MarkPriceStore(symbol)
        mark_price_store.update(base_price * 1.001, base_price, int(datetime.now().timestamp() * 1000))
        
        # Mock candle buffers
        candle_buffers = {
            "1m": CandleBuffer(symbol, "1m"),
            "15m": CandleBuffer(symbol, "15m")
        }
        
        # Mock trade flow store
        trade_flow_store = TradeFlowStore(symbol)
        
        return {
            "orderbook_store": orderbook_store,
            "mark_price_store": mark_price_store,
            "candle_buffers": candle_buffers,
            "trade_flow_store": trade_flow_store
        }
    
    def get_market_state(self, symbol: str) -> Optional[MarketState]:
        """Get current market state for a symbol"""
        return self.market_states.get(symbol)
    
    def get_all_market_states(self) -> Dict[str, MarketState]:
        """Get all current market states"""
        return self.market_states.copy()
    
    def get_valid_signals(self) -> List[MarketState]:
        """Get all valid trading signals"""
        return [state for state in self.market_states.values() if state.is_valid_signal()]
    
    def get_signal_summary(self) -> Dict[str, Any]:
        """Get summary of all signals"""
        return self.signal_generator.get_signal_summary()
    
    def get_performance_metrics(self) -> Dict[str, Any]:
        """Get performance metrics"""
        return self.signal_generator.get_performance_metrics()
    
    def is_signal_active(self, symbol: str) -> bool:
        """Check if there's an active signal for a symbol"""
        market_state = self.market_states.get(symbol)
        return market_state.is_valid_signal() if market_state else False
    
    def get_signal_strength(self, symbol: str) -> float:
        """Get signal strength for a symbol"""
        market_state = self.market_states.get(symbol)
        return market_state.get_signal_strength() if market_state else 0.0
    
    async def force_analysis(self, symbol: str = None) -> None:
        """Force analysis for a symbol or all symbols"""
        if symbol:
            await self._analyze_symbol(symbol)
        else:
            for s in self.config.assets:
                await self._analyze_symbol(s)
    
    def get_engine_status(self) -> Dict[str, Any]:
        """Get engine status"""
        return {
            "is_running": self.is_running,
            "symbols_tracked": list(self.market_states.keys()),
            "last_updates": self.last_update_time.copy(),
            "total_signals": len(self.market_states),
            "valid_signals_count": len(self.get_valid_signals())
        }
