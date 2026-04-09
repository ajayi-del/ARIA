import structlog
from typing import Literal, Dict, Any, List, Tuple
from datetime import datetime, timedelta
import numpy as np
from core.market_state import MarketState

logger = structlog.get_logger(__name__)


class RegimeAnalyzer:
    """Tier 2 - Regime analysis (risk on/off, rotation, confusion)"""
    
    def __init__(self):
        self.asset_correlations = {}
        self.regime_history: List[Dict[str, Any]] = []
        
    def analyze_regime(
        self,
        symbol: str,
        asset_returns: Dict[str, List[float]],
        volatility_data: Dict[str, float],
        volume_data: Dict[str, float]
    ) -> tuple[Literal["risk_on", "risk_off", "rotational", "confused"], str, str]:
        """
        Analyze market regime
        
        Returns: (regime, leading_asset, lagging_asset)
        """
        # Calculate correlations
        correlations = self._calculate_correlations(asset_returns)
        
        # Determine regime type
        regime = self._determine_regime_type(correlations, volatility_data, volume_data)
        
        # Find leading and lagging assets
        leading, lagging = self._find_leading_lagging_assets(asset_returns, correlations)
        
        return regime, leading, lagging
    
    def _calculate_correlations(self, asset_returns: Dict[str, List[float]]) -> Dict[Tuple[str, str], float]:
        """Calculate rolling correlations between assets"""
        correlations = {}
        assets = list(asset_returns.keys())
        
        for i, asset1 in enumerate(assets):
            for j, asset2 in enumerate(assets[i+1:], i+1):
                if len(asset_returns[asset1]) >= 20 and len(asset_returns[asset2]) >= 20:
                    # Use last 20 returns for correlation
                    returns1 = np.array(asset_returns[asset1][-20:])
                    returns2 = np.array(asset_returns[asset2][-20:])
                    
                    if len(returns1) == len(returns2):
                        corr = np.corrcoef(returns1, returns2)[0, 1]
                        if not np.isnan(corr):
                            correlations[(asset1, asset2)] = corr
        
        return correlations
    
    def _determine_regime_type(
        self,
        correlations: Dict[Tuple[str, str], float],
        volatility_data: Dict[str, float],
        volume_data: Dict[str, float]
    ) -> Literal["risk_on", "risk_off", "rotational", "confused"]:
        """Determine the current market regime"""
        
        # Average correlation
        if correlations:
            avg_correlation = np.mean(list(correlations.values()))
        else:
            avg_correlation = 0.0
        
        # Average volatility
        if volatility_data:
            avg_volatility = np.mean(list(volatility_data.values()))
        else:
            avg_volatility = 0.0
        
        # Volume analysis
        if volume_data:
            avg_volume = np.mean(list(volume_data.values()))
            volume_ratio = avg_volume / 1_000_000  # Normalize to millions
        else:
            volume_ratio = 0.0
        
        # Regime determination logic
        if avg_correlation > 0.7:
            # High correlation - either risk on or risk off
            if avg_volatility > 0.03:  # High volatility
                return "risk_off"  # Flight to safety
            else:
                return "risk_on"   # Risk assets moving together
        elif avg_correlation < 0.3:
            # Low correlation - rotational market
            return "rotational"
        else:
            # Medium correlation - confused market
            if volume_ratio > 2.0:  # High volume but unclear direction
                return "confused"
            else:
                return "rotational"
    
    def _find_leading_lagging_assets(
        self,
        asset_returns: Dict[str, List[float]],
        correlations: Dict[Tuple[str, str], float]
    ) -> Tuple[str, str]:
        """Identify leading and lagging assets"""
        
        if not asset_returns or len(asset_returns) < 2:
            return "unknown", "unknown"
        
        # Calculate recent performance (last 5 periods)
        recent_performance = {}
        for asset, returns in asset_returns.items():
            if len(returns) >= 5:
                recent_performance[asset] = np.sum(returns[-5:])
            else:
                recent_performance[asset] = 0.0
        
        # Sort by performance
        sorted_assets = sorted(recent_performance.items(), key=lambda x: x[1], reverse=True)
        
        leading = sorted_assets[0][0] if sorted_assets else "unknown"
        lagging = sorted_assets[-1][0] if sorted_assets else "unknown"
        
        return leading, lagging
    
    def get_regime_strength(self, regime: str) -> float:
        """Get confidence score for regime detection (0.0-1.0)"""
        if not self.regime_history:
            return 0.0
        
        # Check consistency of recent regime calls
        recent_regimes = [r.get("regime") for r in self.regime_history[-10:]]
        if not recent_regimes:
            return 0.0
        
        current_regime_count = recent_regimes.count(regime)
        return current_regime_count / len(recent_regimes)
    
    def update_regime_history(self, regime: str, metadata: Dict[str, Any] = None):
        """Update regime history for tracking"""
        self.regime_history.append({
            "timestamp": datetime.now().isoformat(),
            "regime": regime,
            "metadata": metadata or {}
        })
        
        # Keep only last 100 entries
        if len(self.regime_history) > 100:
            self.regime_history = self.regime_history[-100:]
