import math
import structlog

logger = structlog.get_logger(__name__)

def compute_freshness(
    signal_age_ms: int,
    current_atr: float,
    current_price: float
) -> float:
    """
    Mathematical model for signal decay.
    sigma = current_atr / current_price (normalised volatility)
    t = age in seconds
    lambda = 0.5 (decay constant)
    freshness = exp(-lambda * t * sigma * 100)
    """
    if signal_age_ms <= 0:
        return 1.0
        
    if current_price <= 0:
        return 0.3 # Floor
        
    sig = current_atr / current_price
    t = signal_age_ms / 1000.0
    lam = 0.5
    
    # formula: exp(-lam * t * sigma * 100)
    decay_val = -lam * t * sig * 100
    
    try:
        freshness = math.exp(decay_val)
    except OverflowError:
        freshness = 0.3
        
    # Clamp to [0.3, 1.0]
    return max(0.3, min(1.0, freshness))
