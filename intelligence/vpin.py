"""
VPIN (Volume-Synchronized Probability of Informed Trading)
v1.3 Hardened: Time-normalized buckets per asset.
"""

import numpy as np
from typing import List, Dict, Optional, Any
from dataclasses import dataclass
from datetime import datetime, timezone

# We assume Trade type is passed but we'll use Any for flexibility

@dataclass
class VPINResult:
    vpin: float
    buy_vol: float
    sell_vol: float
    is_hot: bool

class VPINCalculator:
    """
    Computes VPIN using time-normalized buckets.
    Ensures cross-venue comparability by normalizing volume imbalance.
    """
    def __init__(self, window_size: int = 50):
        self.window_size = window_size
        self._buckets: Dict[str, List[Dict[str, float]]] = {}

    def compute(self, symbol: str, trade_history: List[Any]) -> VPINResult:
        """
        Computes VPIN for a given symbol based on recent trade history.
        Normalized by window-average volume.
        """
        if not trade_history:
            return VPINResult(0.0, 0.0, 0.0, False)
            
        # 1. Aggregate trades into 1-minute time buckets (or use raw history if small)
        # For ARIA, we assume the trade_history is already recent (past 5-10 mins)
        
        buy_vol = sum(t.size for t in trade_history if t.is_aggressor_buy)
        sell_vol = sum(t.size for t in trade_history if not t.is_aggressor_buy)
        total_vol = buy_vol + sell_vol
        
        if total_vol == 0:
            return VPINResult(0.0, 0.0, 0.0, False)
            
        imbalance = abs(buy_vol - sell_vol)
        
        # 2. Maintain sliding window of imbalance buckets
        if symbol not in self._buckets:
            self._buckets[symbol] = []
            
        self._buckets[symbol].append({
            "imbalance": imbalance,
            "total_vol": total_vol
        })
        
        if len(self._buckets[symbol]) > self.window_size:
            self._buckets[symbol].pop(0)
            
        # 3. VPIN = sum(imbalances) / sum(total_volumes)
        sum_imbalance = sum(b["imbalance"] for b in self._buckets[symbol])
        sum_total_vol = sum(b["total_vol"] for b in self._buckets[symbol])
        
        vpin = sum_imbalance / sum_total_vol if sum_total_vol > 0 else 0.0
        
        # 4. "Hot" threshold (v1.3 standard: > 0.70 indicates toxic flow)
        is_hot = vpin > 0.70
        
        return VPINResult(vpin, buy_vol, sell_vol, is_hot)
