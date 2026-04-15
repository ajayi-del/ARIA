"""
System State Manager

Tracks per-symbol readiness and global system phase.
WARMING_UP -> READY -> TRADING
"""

from enum import Enum
import time
import structlog

logger = structlog.get_logger(__name__)

# If a symbol stays in WARMING_UP for longer than this without reaching min_candles,
# force it to READY. Prevents no-trade symbols (e.g. thin markets, halted equities)
# from blocking global system readiness indefinitely.
_WARMUP_TIMEOUT_S: float = 300.0   # 5 minutes

class SystemPhase(Enum):
    WARMING_UP = "warming_up"
    READY      = "ready"
    TRADING    = "trading"

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
            is_ready = True
            logger.info("warmup_timeout_forced_ready", symbol=symbol,
                        candles=candle_count, min_candles=self.min_candles,
                        timeout_s=_WARMUP_TIMEOUT_S,
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
