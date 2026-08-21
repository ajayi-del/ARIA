import time
import structlog
from core.event_bus import event_bus, Event, EventType

logger = structlog.get_logger(__name__)

class MarkPriceStore:
    # Rebase quarantine (2026-08-21): SoDEX synthetics rebase mid-session
    # (SPCX jumped 5.7x in one tick). The new mark is REAL, but any stop
    # trigger or PnL computed against the pre-rebase entry is phantom — the
    # 08-21 SPCX stop fired on the rebased mark and journaled a phantom
    # -$649.78 while the balance was untouched. A >15% single-tick jump
    # quarantines trigger consumers for 60s while reconciliation re-anchors
    # entry/stop/size from the exchange (mark_rebase_reanchored).
    DISCONTINUITY_JUMP_PCT = 15.0
    QUARANTINE_MS = 60_000

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.mark_price: float | None = None
        self.last_price: float | None = None
        self.last_update_ms: int | None = None
        self.quarantined_until_ms: int = 0
        self.quarantine_factor: float = 0.0

    def update(self, mark_price: float, last_price: float, timestamp_ms: int) -> None:
        prev = self.mark_price
        if prev and prev > 0 and mark_price > 0:
            jump_pct = abs(mark_price - prev) / prev * 100.0
            if jump_pct > self.DISCONTINUITY_JUMP_PCT:
                self.quarantined_until_ms = timestamp_ms + self.QUARANTINE_MS
                self.quarantine_factor = mark_price / prev
                logger.warning("mark_discontinuity_quarantined", symbol=self.symbol,
                               prev_mark=prev, new_mark=mark_price,
                               jump_pct=round(jump_pct, 2),
                               factor=round(self.quarantine_factor, 4))
        self.mark_price = mark_price
        self.last_price = last_price
        self.last_update_ms = timestamp_ms

        # Publish update event — consumers expect "mark_price" and "last_price" keys
        event_bus.publish(Event(
            EventType.MARK_PRICE_UPDATED,
            self.symbol,
            timestamp_ms,
            {"mark_price": mark_price, "last_price": last_price}
        ))

    def age_ms(self) -> int:
        if self.last_update_ms is None:
            return 999999
        return int(time.time() * 1000) - self.last_update_ms

    def is_healthy(self, max_age_ms: int) -> bool:
        return self.age_ms() <= max_age_ms

    def is_quarantined(self) -> bool:
        return int(time.time() * 1000) < self.quarantined_until_ms

    def get(self) -> dict:
        if self.last_price is None or self.mark_price is None or self.last_update_ms is None:
            return {
                "mark_price": 0.0,
                "last_price": 0.0,
                "divergence_pct": 0.0,
                "divergence_abs": 0.0,
                "timestamp_ms": 0,
                "age_ms": self.age_ms()
            }
            
        divergence_abs = abs(self.mark_price - self.last_price)
        divergence_pct = (divergence_abs / self.last_price * 100) if self.last_price != 0 else 0.0
        
        return {
            "mark_price": self.mark_price,
            "last_price": self.last_price,
            "divergence_pct": divergence_pct,
            "divergence_abs": divergence_abs,
            "timestamp_ms": self.last_update_ms,
            "age_ms": self.age_ms()
        }

    def is_diverging(self, threshold_pct: float = 0.05) -> bool:
        data = self.get()
        return data["divergence_pct"] > threshold_pct
