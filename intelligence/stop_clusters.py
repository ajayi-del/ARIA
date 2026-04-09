from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any
import time
import numpy as np

@dataclass
class StopCluster:
    price: float
    side: str  # "long_stops" or "short_stops"
    strength: float  # 0.0 to 1.0
    source: str  # "round_number", "structure", "oi_level"
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))

class StopClusterMap:
    """
    Maintains a map of price levels where stop orders are likely concentrated.
    A real sweep must hit a real stop cluster.
    """
    
    def __init__(self):
        self._clusters: Dict[str, List[StopCluster]] = {}
        
    def build_map(
        self,
        symbol: str,
        current_price: float,
        candles: List[Any] = None, # List of Candle objects
        recent_liquidations: List[Dict[str, Any]] = None
    ) -> List[StopCluster]:
        """Scans for round numbers, structure levels, and liqs to build the map."""
        clusters = []
        
        # 1. Round Number Clusters
        increments = {
            "BTC": 500,
            "ETH": 50,
            "SOL": 2,
            "XAUT": 20,
            "BNB": 5,
            "LINK": 0.5,
            "AVAX": 1
        }
        inc = increments.get(symbol, 1.0)
        
        # Scan within 2% of current price
        lower_bound = current_price * 0.98
        upper_bound = current_price * 1.02
        
        start_price = (lower_bound // inc) * inc
        while start_price <= upper_bound:
            clusters.append(StopCluster(
                price=start_price,
                side="long_stops" if start_price < current_price else "short_stops",
                strength=0.6,
                source="round_number"
            ))
            start_price += inc
            
        # 2. Structure Clusters (Last 20 candles)
        if candles and len(candles) >= 20:
            highs = [c.high for c in candles[-20:]]
            lows = [c.low for c in candles[-20:]]
            
            recent_high = max(highs)
            recent_low = min(lows)
            
            # Short stops just above recent high
            clusters.append(StopCluster(
                price=recent_high * 1.001,
                side="short_stops",
                strength=0.8,
                source="structure"
            ))
            
            # Long stops just below recent low
            clusters.append(StopCluster(
                price=recent_low * 0.999,
                side="long_stops",
                strength=0.8,
                source="structure"
            ))
            
        # 3. Liquidation Clusters
        if recent_liquidations:
            # Group liqs within 0.2%
            for liq in recent_liquidations:
                price = liq.get("price", 0)
                size = liq.get("size", 0)
                if price == 0: continue
                
                # Check for existing liq cluster nearby
                found = False
                for c in clusters:
                    if c.source == "oi_level" and abs(c.price - price) / price < 0.002:
                         c.strength = min(1.0, c.strength + 0.1)
                         found = True
                         break
                
                if not found:
                    clusters.append(StopCluster(
                        price=price,
                        side="long_stops" if price < current_price else "short_stops",
                        strength=0.7,
                        source="oi_level"
                    ))
                    
        self._clusters[symbol] = clusters
        return clusters

    def nearest_cluster(
        self,
        symbol: str,
        price: float,
        side: str,
        tolerance_pct: float = 0.3
    ) -> Optional[StopCluster]:
        """Returns closest cluster of correct side within tolerance_pct."""
        if symbol not in self._clusters:
            return None
            
        best_cluster = None
        min_distance = float('inf')
        
        for c in self._clusters[symbol]:
            if c.side != side:
                continue
                
            dist_pct = (abs(c.price - price) / price) * 100
            if dist_pct <= tolerance_pct:
                if dist_pct < min_distance:
                    min_distance = dist_pct
                    best_cluster = c
                    
        return best_cluster

    def validate_sweep(
        self,
        symbol: str,
        sweep_price: float,
        sweep_side: str
    ) -> tuple[bool, float]:
        """
        Validates a microstructure sweep.
        sweep_side: "long_stops" (for bullish sweep) or "short_stops" (for bearish sweep)
        """
        cluster = self.nearest_cluster(symbol, sweep_price, sweep_side)
        if cluster:
            return True, cluster.strength
        return False, 0.0

# Mock candle wrapper if needed by build_map
from typing import Any
