import structlog
from typing import Dict, Any, List, Optional
import numpy as np
from datetime import datetime, timedelta
from data.orderbook_store import OrderbookStore
from data.mark_price_store import MarkPriceStore
from data.candle_buffer import CandleBuffer
from data.trade_flow_store import TradeFlowStore

logger = structlog.get_logger(__name__)


class DataProcessor:
    """Processes raw market data into structured format for signal generation"""
    
    def __init__(self):
        self.processed_data: Dict[str, Dict[str, Any]] = {}
        self.data_windows = {
            "price": 100,      # Keep last 100 price points
            "volume": 100,     # Keep last 100 volume points
            "trades": 50,      # Keep last 50 trades
            "candles": 200     # Keep last 200 candles
        }
        
    def process_market_data(
        self,
        symbol: str,
        orderbook_store: OrderbookStore,
        mark_price_store: MarkPriceStore,
        candle_buffers: Dict[str, CandleBuffer],
        trade_flow_store: TradeFlowStore
    ) -> Dict[str, Any]:
        """
        Process raw market data into structured format for signal generation
        
        Returns: Dictionary containing all processed market data
        """
        
        # Initialize symbol data if not exists
        if symbol not in self.processed_data:
            self.processed_data[symbol] = {}
        
        # Process orderbook data
        orderbook_data = self._process_orderbook_data(orderbook_store)
        
        # Process mark price data
        mark_price_data = self._process_mark_price_data(mark_price_store)
        
        # Process candle data
        candle_data = self._process_candle_data(candle_buffers)
        
        # Process trade flow data
        trade_data = self._process_trade_data(trade_flow_store)
        
        # Calculate derived metrics
        derived_metrics = self._calculate_derived_metrics(
            orderbook_data, mark_price_data, candle_data, trade_data
        )
        
        # Combine all data
        processed_data = {
            "symbol": symbol,
            "timestamp": datetime.now().isoformat(),
            "orderbook_data": orderbook_data,
            "mark_price_data": mark_price_data,
            "candle_data": candle_data,
            "trade_data": trade_data,
            "derived_metrics": derived_metrics,
            # Additional data for specific analyzers
            "price_data": derived_metrics.get("price_history", []),
            "volume_data": derived_metrics.get("volume_history", []),
            "high_data": derived_metrics.get("high_history", []),
            "low_data": derived_metrics.get("low_history", []),
            "funding_rate": 0.0,  # Placeholder - would come from funding data
            "funding_history": [],
            "mark_price": mark_price_data.get("mark_price", 0.0),
            "index_price": mark_price_data.get("index_price", 0.0),
            "price_action": self._create_price_action_data(derived_metrics),
            "volume_profile": self._create_volume_profile_data(derived_metrics),
            # Mock data for demo purposes (would be replaced with real data sources)
            "economic_data": {},
            "news_sentiment": {},
            "institutional_flow": {},
            "geopolitical_risk": 0.5,
            "market_breadth": {},
            "asset_returns": self._calculate_asset_returns(symbol),
            "volatility_data": self._calculate_volatility_data(symbol),
        }
        
        # Store processed data
        self.processed_data[symbol] = processed_data
        
        return processed_data
    
    def _process_orderbook_data(self, orderbook_store: OrderbookStore) -> Dict[str, Any]:
        """Process orderbook data"""
        if not orderbook_store or not orderbook_store.bids or not orderbook_store.asks:
            return {"bids": [], "asks": [], "spread": 0.0, "mid_price": 0.0}
        
        bids = [(price, size) for price, size in orderbook_store.bids]
        asks = [(price, size) for price, size in orderbook_store.asks]
        
        # Sort by price
        bids.sort(key=lambda x: x[0], reverse=True)  # Highest first
        asks.sort(key=lambda x: x[0])  # Lowest first
        
        # Calculate spread and mid price
        if bids and asks:
            best_bid = bids[0][0]
            best_ask = asks[0][0]
            spread = best_ask - best_bid
            mid_price = (best_bid + best_ask) / 2
        else:
            spread = 0.0
            mid_price = 0.0
        
        return {
            "bids": bids[:10],  # Top 10 levels
            "asks": asks[:10],  # Top 10 levels
            "spread": spread,
            "mid_price": mid_price,
            "bid_volume": sum(size for _, size in bids[:5]),
            "ask_volume": sum(size for _, size in asks[:5]),
            "total_volume": sum(size for _, size in bids[:5]) + sum(size for _, size in asks[:5])
        }
    
    def _process_mark_price_data(self, mark_price_store: MarkPriceStore) -> Dict[str, Any]:
        """Process mark price data"""
        if not mark_price_store or not mark_price_store.mark_price:
            return {"mark_price": 0.0, "index_price": 0.0, "premium": 0.0}
        
        mark_price = mark_price_store.mark_price
        # MarkPriceStore has last_price instead of index_price
        index_price = mark_price_store.last_price if mark_price_store.last_price else mark_price
        
        premium = ((mark_price - index_price) / index_price * 100) if index_price > 0 else 0.0
        
        return {
            "mark_price": mark_price,
            "index_price": index_price,
            "premium": premium,
            "timestamp": mark_price_store.last_update_ms
        }
    
    def _process_candle_data(self, candle_buffers: Dict[str, CandleBuffer]) -> Dict[str, Any]:
        """Process candle data from multiple timeframes"""
        candle_data = {}
        
        for interval, buffer in candle_buffers.items():
            if buffer and buffer.candles:
                candles = []
                # Use .latest(n) as deques don't support slicing
                for candle in buffer.latest(20):  # Last 20 candles
                    candles.append({
                        "open_time": candle.open_time,
                        "close_time": candle.close_time,
                        "open": candle.open,
                        "high": candle.high,
                        "low": candle.low,
                        "close": candle.close,
                        "volume": candle.volume
                    })
                
                candle_data[interval] = candles
        
        return candle_data
    
    def _process_trade_data(self, trade_flow_store: TradeFlowStore) -> List[Dict[str, Any]]:
        """Process trade flow data"""
        if not trade_flow_store or not trade_flow_store.trades:
            return []
        
        trades = []
        # Deques don't support slicing, convert to list first
        for trade in list(trade_flow_store.trades)[-50:]:  # Last 50 trades
            trades.append({
                "timestamp_ms": trade.timestamp_ms,
                "price": trade.price,
                "size": trade.size,
                "side": trade.side,
                "is_aggressor_buy": trade.is_aggressor_buy
            })
        
        return trades
    
    def _calculate_derived_metrics(
        self,
        orderbook_data: Dict[str, Any],
        mark_price_data: Dict[str, Any],
        candle_data: Dict[str, Any],
        trade_data: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Calculate derived metrics from raw data"""
        
        derived = {}
        
        # Price history from candles
        if "1m" in candle_data and candle_data["1m"]:
            closes = [c["close"] for c in candle_data["1m"]]
            highs = [c["high"] for c in candle_data["1m"]]
            lows = [c["low"] for c in candle_data["1m"]]
            volumes = [c["volume"] for c in candle_data["1m"]]
            
            derived["price_history"] = closes
            derived["high_history"] = highs
            derived["low_history"] = lows
            derived["volume_history"] = volumes
            
            # Calculate returns
            if len(closes) > 1:
                returns = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]
                derived["returns"] = returns
                derived["volatility"] = np.std(returns) if returns else 0.0
            else:
                derived["returns"] = []
                derived["volatility"] = 0.0
        
        # Current price
        derived["current_price"] = mark_price_data.get("mark_price", 0.0)
        
        # Support and resistance levels
        if derived.get("high_history") and derived.get("low_history"):
            derived["support_levels"] = self._find_support_levels(derived["low_history"])
            derived["resistance_levels"] = self._find_resistance_levels(derived["high_history"])
        
        # Volume metrics
        if derived.get("volume_history"):
            derived["avg_volume"] = np.mean(derived["volume_history"])
            derived["recent_volume"] = derived["volume_history"][-1] if derived["volume_history"] else 0
            derived["volume_trend"] = self._calculate_volume_trend(derived["volume_history"])
        
        # Orderbook metrics
        derived["orderbook_imbalance"] = self._calculate_orderbook_imbalance(orderbook_data)
        derived["spread_pct"] = (orderbook_data.get("spread", 0) / orderbook_data.get("mid_price", 1)) * 100
        
        return derived
    
    def _find_support_levels(self, low_prices: List[float], lookback: int = 20) -> List[float]:
        """Find support levels from low prices"""
        if len(low_prices) < lookback:
            return []
        
        support_levels = []
        
        # Find local minima
        for i in range(lookback, len(low_prices) - lookback):
            current_low = low_prices[i]
            is_local_min = True
            
            for j in range(i - lookback, i + lookback + 1):
                if j != i and low_prices[j] <= current_low:
                    is_local_min = False
                    break
            
            if is_local_min:
                support_levels.append(current_low)
        
        # Return unique levels (within 0.5% tolerance)
        unique_levels = []
        for level in support_levels:
            is_unique = True
            for unique in unique_levels:
                if abs(level - unique) / unique < 0.005:  # 0.5% tolerance
                    is_unique = False
                    break
            if is_unique:
                unique_levels.append(level)
        
        return sorted(unique_levels)[:5]  # Return top 5 levels
    
    def _find_resistance_levels(self, high_prices: List[float], lookback: int = 20) -> List[float]:
        """Find resistance levels from high prices"""
        if len(high_prices) < lookback:
            return []
        
        resistance_levels = []
        
        # Find local maxima
        for i in range(lookback, len(high_prices) - lookback):
            current_high = high_prices[i]
            is_local_max = True
            
            for j in range(i - lookback, i + lookback + 1):
                if j != i and high_prices[j] >= current_high:
                    is_local_max = False
                    break
            
            if is_local_max:
                resistance_levels.append(current_high)
        
        # Return unique levels (within 0.5% tolerance)
        unique_levels = []
        for level in resistance_levels:
            is_unique = True
            for unique in unique_levels:
                if abs(level - unique) / unique < 0.005:  # 0.5% tolerance
                    is_unique = False
                    break
            if is_unique:
                unique_levels.append(level)
        
        return sorted(unique_levels, reverse=True)[:5]  # Return top 5 levels
    
    def _calculate_volume_trend(self, volume_history: List[float]) -> float:
        """Calculate volume trend"""
        if len(volume_history) < 10:
            return 0.0
        
        recent_avg = np.mean(volume_history[-5:])
        older_avg = np.mean(volume_history[-10:-5])
        
        if older_avg == 0:
            return 0.0
        
        return (recent_avg - older_avg) / older_avg
    
    def _calculate_orderbook_imbalance(self, orderbook_data: Dict[str, Any]) -> float:
        """Calculate orderbook imbalance"""
        bid_volume = orderbook_data.get("bid_volume", 0)
        ask_volume = orderbook_data.get("ask_volume", 0)
        total_volume = bid_volume + ask_volume
        
        if total_volume == 0:
            return 0.0
        
        return (bid_volume - ask_volume) / total_volume
    
    def _create_price_action_data(self, derived_metrics: Dict[str, Any]) -> Dict[str, Any]:
        """Create price action data for MAG analyzer"""
        return {
            "prices": derived_metrics.get("price_history", []),
            "current_price": derived_metrics.get("current_price", 0.0),
            "support_levels": derived_metrics.get("support_levels", []),
            "resistance_levels": derived_metrics.get("resistance_levels", []),
            "volatility": derived_metrics.get("volatility", 0.0)
        }
    
    def _create_volume_profile_data(self, derived_metrics: Dict[str, Any]) -> Dict[str, Any]:
        """Create volume profile data for MAG analyzer"""
        return {
            "recent_volume": derived_metrics.get("recent_volume", 0.0),
            "avg_volume": derived_metrics.get("avg_volume", 1.0),
            "volume_trend": derived_metrics.get("volume_trend", 0.0)
        }
    
    def _calculate_asset_returns(self, symbol: str) -> Dict[str, List[float]]:
        """Calculate asset returns (mock data for now)"""
        # This would normally calculate returns across multiple assets
        # For now, return mock data
        return {
            symbol: [0.001, -0.002, 0.003, -0.001, 0.002]  # Mock returns
        }
    
    def _calculate_volatility_data(self, symbol: str) -> Dict[str, float]:
        """Calculate volatility data (mock data for now)"""
        # This would normally calculate volatility across multiple assets
        # For now, return mock data
        return {
            symbol: 0.02  # 2% volatility
        }
    
    def get_processed_data(self, symbol: str) -> Optional[Dict[str, Any]]:
        """Get processed data for a symbol"""
        return self.processed_data.get(symbol)
    
    def clear_old_data(self):
        """Clear old processed data to save memory"""
        for symbol in self.processed_data:
            # Keep only recent data
            pass  # Implementation depends on specific needs
