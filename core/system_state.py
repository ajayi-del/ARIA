"""
System State Manager

Tracks per-symbol readiness and global system phase.
WARMING_UP -> READY -> TRADING
"""

from enum import Enum
import time
import structlog
from core.asset_classes import ASSET_CLASS as _FULL_ASSET_CLASS

logger = structlog.get_logger(__name__)

# If a symbol stays in WARMING_UP for longer than this without reaching min_candles,
# force it to READY (if it also meets the per-asset-class minimum below).
# Prevents thin/halted symbols from blocking global system readiness indefinitely.
_WARMUP_TIMEOUT_S: float = 300.0   # 5 minutes

# Per-asset-class minimum candles required before a timeout-forced-ready is allowed.
# If a symbol hasn't reached this minimum at timeout, it stays WARMING_UP (extended)
# and will not trade until either it reaches the minimum or market reopens.
MINIMUM_CANDLES_TO_TRADE: dict = {
    "crypto":       30,   # fast markets — relaxed
    "equity":       50,   # slow signals — strict
    "commodity":    40,   # medium
    "equity_index": 50,   # strict
}

# Asset class lookup — drives per-class minimum above.
# Symbols not listed default to "crypto".
ASSET_CLASS: dict = {
    "BTC-USD":   "crypto",
    "ETH-USD":   "crypto",
    "SOL-USD":   "crypto",
    "ARB-USD":   "crypto",
    "OP-USD":    "crypto",
    "LINK-USD":  "crypto",
    "BNB-USD":   "crypto",
    "XRP-USD":   "crypto",
    "TRUMP-USD": "crypto",
    "BASED-USD": "crypto",
    "XAUT-USD":  "commodity",
    "NVDA-USD":  "equity",
    "AAPL-USD":  "equity",
    "SPY-USD":   "equity_index",
    "QQQ-USD":   "equity_index",
}


class SystemPhase(Enum):
    WARMING_UP          = "warming_up"
    WARMUP_EXTENDED     = "warmup_extended"    # timeout fired but below per-class minimum
    WARMUP_INCOMPLETE   = "warmup_incomplete"  # equity closed before reaching minimum
    READY               = "ready"
    TRADING             = "trading"

class SystemStateManager:
    """
    Tracks per-symbol readiness.
    Single source of truth for system phase.
    """
    
    def __init__(self, min_candles: int = 50, assets: list[str] = None):
        self.min_candles = min_candles
        self.assets = assets or []
        
        self._symbol_phase: dict[str, SystemPhase] = {
            asset: SystemPhase.WARMING_UP for asset in self.assets
        }
        self._candle_counts: dict[str, int] = {
            asset: 0 for asset in self.assets
        }
        # Warmup start timestamps — used for timeout enforcement
        _now = time.monotonic()
        self._warmup_started: dict[str, float] = {
            asset: _now for asset in self.assets
        }
        self._global_phase: SystemPhase = SystemPhase.WARMING_UP
        
        logger.info("system_state_manager_initialized", 
                    min_candles=min_candles, 
                    assets=self.assets)

    def update(
        self,
        symbol: str, 
        candle_count: int, 
        ob_healthy: bool, 
        mark_healthy: bool,
        require_ob: bool = False
    ) -> SystemPhase:
        """
        Updates readiness state for a symbol.
        """
        if symbol not in self._symbol_phase:
            logger.warning("unknown_symbol_update", symbol=symbol)
            return SystemPhase.WARMING_UP

        # Ready condition: 50 candles + healthy mark price
        # ob_healthy gated by require_ob (SoDEX OB may lag during warmup)
        is_ready = (candle_count >= self.min_candles) and \
                   (not require_ob or ob_healthy) and \
                   mark_healthy

        current_phase = self._symbol_phase[symbol]

        # Warmup timeout: if symbol hasn't reached min_candles in 5 minutes,
        # force READY so thin-market/halted symbols don't block the whole system.
        # Typical cause: equity symbols during off-hours with zero candle closes.
        if (not is_ready
                and current_phase == SystemPhase.WARMING_UP
                and mark_healthy  # require at least a valid price
                and time.monotonic() - self._warmup_started.get(symbol, 0) > _WARMUP_TIMEOUT_S):
            # Use comprehensive asset class dict (covers all equity/commodity/crypto symbols)
            asset_class = _FULL_ASSET_CLASS.get(symbol, ASSET_CLASS.get(symbol, "crypto"))
            min_required = MINIMUM_CANDLES_TO_TRADE[asset_class]
            if candle_count < min_required:
                # Below per-class minimum — extend warmup rather than forcing ready.
                # Typical for equity symbols during off-hours with sparse candle closes.
                self._symbol_phase[symbol] = SystemPhase.WARMUP_EXTENDED
                self._candle_counts[symbol] = candle_count
                logger.info("warmup_extended", symbol=symbol,
                            candles=candle_count, required=min_required,
                            asset_class=asset_class,
                            note="timeout fired but below per-class minimum")
                return SystemPhase.WARMUP_EXTENDED
            is_ready = True
            logger.info("warmup_timeout_forced_ready", symbol=symbol,
                        candles=candle_count, min_candles=self.min_candles,
                        timeout_s=_WARMUP_TIMEOUT_S, asset_class=asset_class,
                        note="symbol forced ready after 5-min warmup timeout")

        if is_ready and current_phase == SystemPhase.WARMING_UP:
            self._symbol_phase[symbol] = SystemPhase.READY
            logger.info("symbol_ready", symbol=symbol, candles=candle_count)
        
        self._candle_counts[symbol] = candle_count
        
        # Update global phase
        all_ready = all(
            p in (SystemPhase.READY, SystemPhase.TRADING)
            for p in self._symbol_phase.values()
        )
        
        if all_ready and self._global_phase == SystemPhase.WARMING_UP:
            self._global_phase = SystemPhase.READY
            logger.info("system_ready_all_symbols")
            
        return self._symbol_phase[symbol]

    def can_signal(self, symbol: str) -> bool:
        """Determines if a symbol is mature enough to generate signals."""
        phase = self._symbol_phase.get(symbol, SystemPhase.WARMING_UP)
        return phase in (SystemPhase.READY, SystemPhase.TRADING)

    def can_trade(self, symbol: str) -> bool:
        """Determines if the system is in active trading phase for a symbol."""
        phase = self._symbol_phase.get(symbol, SystemPhase.WARMING_UP)
        return phase in (SystemPhase.READY, SystemPhase.TRADING)

    def mark_trading(self, symbol: str) -> None:
        """Moves a symbol from READY to TRADING."""
        if self._symbol_phase.get(symbol) == SystemPhase.READY:
            self._symbol_phase[symbol] = SystemPhase.TRADING
            logger.info("symbol_trading_active", symbol=symbol)

    def get_warmup_status(self) -> dict:
        """Returns per-symbol candle counts and phase for the display."""
        return {
            symbol: {
                "count": self._candle_counts[symbol],
                "phase": self._symbol_phase[symbol].value,
                "target": self.min_candles
            } for symbol in self.assets
        }

    def get_global_phase(self) -> SystemPhase:
        """Returns the aggregate system phase."""
        return self._global_phase
