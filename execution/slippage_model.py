import structlog

logger = structlog.get_logger(__name__)

def calculate_expected_slippage(
    symbol: str,
    size: float,
    bids: list[tuple[float, float]],
    asks: list[tuple[float, float]],
    side: int,  # 1 for long (buy), -1 for short (sell)
    vpin_score: float = 0.5
) -> float:
    """
    Quant Fix #1: Slippage Model.
    Calculates expected slippage based on depth and VPIN toxicity.
    
    Returns: expected_slippage_usd
    """
    if size <= 0:
        return 0.0
        
    book = asks if side == 1 else bids
    if not book:
        return 0.0
        
    # Sort: asks ascending, bids descending
    sorted_book = sorted(book, key=lambda x: x[0], reverse=(side == -1))
    
    accumulated_vol = 0.0
    weighted_price = 0.0
    remaining_size = size
    
    for entry_price, entry_vol in sorted_book:
        fill = min(remaining_size, entry_vol)
        weighted_price += fill * entry_price
        accumulated_vol += fill
        remaining_size -= fill
        
        if remaining_size <= 0:
            break
            
    if accumulated_vol == 0:
        return 0.0
        
    avg_fill_price = weighted_price / accumulated_vol
    best_price = sorted_book[0][0]
    
    # Base slippage
    base_slippage = abs(avg_fill_price - best_price)
    
    # Toxicity multiplier (VPIN 0-1)
    # If VPIN is high (>0.7), we expect more adverse movement during filling
    toxicity_mult = 1.0 + max(0, (vpin_score - 0.5) * 2.0)
    
    expected_slippage = base_slippage * toxicity_mult
    
    logger.debug("slippage_calculated", 
                 symbol=symbol, 
                 size=size, 
                 base=base_slippage, 
                 expected=expected_slippage,
                 vpin=vpin_score)
                 
    return expected_slippage
