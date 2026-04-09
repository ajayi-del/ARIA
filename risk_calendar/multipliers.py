from typing import Dict, Any

# Mapping of event types to per-asset impact scores (0.0 = no impact, 1.0 = maximum)
EVENT_ASSET_IMPACT = {
    "FOMC": {
        "BTC-USD":    0.8,
        "ETH-USD":    0.8,
        "SOL-USD":    0.7,
        "XAUT-USD":   1.0,
        "BNB-USD":    0.6,
        "LINK-USD":   0.6,
        "AVAX-USD":   0.7,
        "USTECH100-USD": 0.9,
    },
    "CPI": {
        "BTC-USD":    0.6,
        "ETH-USD":    0.6,
        "SOL-USD":    0.5,
        "XAUT-USD":   1.0,
        "BNB-USD":    0.4,
        "LINK-USD":   0.4,
        "AVAX-USD":   0.5,
        "USTECH100-USD": 0.8,
    },
    "NFP": {
        "BTC-USD":    0.5,
        "ETH-USD":    0.5,
        "SOL-USD":    0.4,
        "XAUT-USD":   0.8,
        "BNB-USD":    0.3,
        "LINK-USD":   0.3,
        "AVAX-USD":   0.4,
        "USTECH100-USD": 0.7,
    },
    "PCE": {
        "BTC-USD":    0.4,
        "ETH-USD":    0.4,
        "SOL-USD":    0.3,
        "XAUT-USD":   0.9,
        "BNB-USD":    0.3,
        "LINK-USD":   0.3,
        "AVAX-USD":   0.3,
        "USTECH100-USD": 0.8,
    },
    "EARNINGS_MAG7": {
        "BTC-USD":    0.4,
        "ETH-USD":    0.4,
        "SOL-USD":    0.3,
        "XAUT-USD":   0.1,
        "BNB-USD":    0.3,
        "LINK-USD":   0.3,
        "AVAX-USD":   0.3,
        "USTECH100-USD": 1.0,
    },
}

DEFAULT_IMPACT = 0.3

def time_decay_multiplier(hours_to_event: float) -> float:
    """
    Returns base size multiplier (0.0-1.0) based on proximity to next high-impact event.
    Standard step function for risk decay.
    """
    if hours_to_event < 0:
        # Event is in the past — recovery handled by post_event_multiplier
        return 1.0

    if hours_to_event < 2.0:
        return 0.0   # BLOCK

    if hours_to_event < 6.0:
        return 0.25  # severe caution

    if hours_to_event < 12.0:
        return 0.50  # caution

    if hours_to_event < 24.0:
        return 0.75  # mild caution

    return 1.0       # clear

def post_event_multiplier(hours_since_event: float) -> float:
    """
    Returns size multiplier during the post-event volatility settlement period.
    """
    if hours_since_event < 0:
        # Event is in the future
        return 1.0
        
    if hours_since_event < 0.5:
        return 0.25   # first 30 min after

    if hours_since_event < 1.0:
        return 0.50   # 30-60 min after

    if hours_since_event < 2.0:
        return 0.75   # 60-120 min after

    return 1.0        # fully recovered

def asset_calendar_multiplier(
    base_multiplier: float,
    event_type: str,
    symbol: str
) -> float:
    """
    Scales the base time-decay multiplier by the asset-specific impact score.
    """
    impact = EVENT_ASSET_IMPACT.get(event_type, {}).get(symbol, DEFAULT_IMPACT)
    
    reduction = (1.0 - base_multiplier) * impact
    
    return max(0.0, min(1.0, 1.0 - reduction))

def stop_atr_multiplier(
    hours_to_event: float,
    event_type: str,
    symbol: str
) -> float:
    """
    Returns ATR multiplier for stop placement during calendar risk periods.
    Wider stops during uncertain periods prevent being stopped out on volatility spikes.
    """
    impact = EVENT_ASSET_IMPACT.get(event_type, {}).get(symbol, DEFAULT_IMPACT)
    
    if hours_to_event < 0:
        # If we are post-event, we don't apply the pre-event stop widening
        return 1.0
        
    if hours_to_event < 2.0:
        return 1.0  # BLOCK regime - stops are irrelevant
    
    if hours_to_event < 12.0:
        # Scale from 1.0 to 2.0 based on impact
        return 1.0 + (1.0 * impact)
    
    if hours_to_event < 24.0:
        # Scale from 1.0 to 1.5 based on impact
        return 1.0 + (0.5 * impact)
    
    return 1.0  # normal stops
