import time

class MarkPriceStore:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.mark_price: float | None = None
        self.last_price: float | None = None
        self.last_update_ms: int | None = None

    def update(self, mark_price: float, last_price: float, timestamp_ms: int) -> None:
        self.mark_price = mark_price
        self.last_price = last_price
        self.last_update_ms = timestamp_ms

    def age_ms(self) -> int:
        if self.last_update_ms is None:
            return 999999
        return int(time.time() * 1000) - self.last_update_ms

    def is_healthy(self, max_age_ms: int) -> bool:
        return self.age_ms() <= max_age_ms

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
