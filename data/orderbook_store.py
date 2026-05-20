import time

class DataStaleError(Exception):
    pass

class OrderbookStore:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bids: list[tuple[float, float]] = []
        self.asks: list[tuple[float, float]] = []
        self.last_update_ms: int | None = None
        self.update_count: int = 0

    def update(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]], timestamp_ms: int) -> None:
        self.bids = bids
        self.asks = asks
        self.last_update_ms = timestamp_ms
        self.update_count += 1
        # Event published by the feed (bybit_feed / sodex_feed) after calling update()
        # to avoid double-firing per OB message.

    def age_ms(self) -> int:
        if self.last_update_ms is None:
            return 999999
        return int(time.time() * 1000) - self.last_update_ms

    def is_healthy(self, max_age_ms: int) -> bool:
        return self.age_ms() <= max_age_ms

    def get_confirmed(self, max_age_ms: int) -> dict:
        if self.last_update_ms is None or self.age_ms() > max_age_ms:
            raise DataStaleError(f"Data stale or missing for {self.symbol}")
        return {
            "bids": self.bids,
            "asks": self.asks,
            "age_ms": self.age_ms(),
            "symbol": self.symbol
        }

    def top_of_book(self) -> tuple[float, float, float]:
        if self.last_update_ms is None or len(self.bids) == 0 or len(self.asks) == 0:
            raise DataStaleError("Stale or missing top of book")
        
        # Sort bids descending, asks ascending
        sorted_bids = sorted(self.bids, key=lambda x: x[0], reverse=True)
        sorted_asks = sorted(self.asks, key=lambda x: x[0])
        best_bid = sorted_bids[0][0]
        best_ask = sorted_asks[0][0]
        spread = best_ask - best_bid
        return best_bid, best_ask, spread

    def update_l4_diff(self, bid_diffs: list, ask_diffs: list, timestamp_ms: int) -> None:
        """
        Merge l4Book diff into existing book state.
        l4Book diffs: qty=0 means remove level; qty>0 means add or update.
        """
        bid_map = {p: q for p, q in self.bids}
        ask_map = {p: q for p, q in self.asks}

        for price, qty in bid_diffs:
            if qty == 0:
                bid_map.pop(price, None)
            else:
                bid_map[price] = qty

        for price, qty in ask_diffs:
            if qty == 0:
                ask_map.pop(price, None)
            else:
                ask_map[price] = qty

        self.bids = sorted(bid_map.items(), key=lambda x: x[0], reverse=True)
        self.asks = sorted(ask_map.items(), key=lambda x: x[0])
        self.last_update_ms = timestamp_ms
        self.update_count += 1

    def imbalance(self, depth: int = 5) -> float:
        if self.last_update_ms is None or len(self.bids) == 0 or len(self.asks) == 0:
            return 0.0

        sorted_bids = sorted(self.bids, key=lambda x: x[0], reverse=True)
        sorted_asks = sorted(self.asks, key=lambda x: x[0])

        bid_vol = sum(size for _, size in sorted_bids[:depth])
        ask_vol = sum(size for _, size in sorted_asks[:depth])

        if bid_vol + ask_vol == 0:
            return 0.0

        return (bid_vol - ask_vol) / (bid_vol + ask_vol)

    def depth_usd(self, side: str = "both", levels: int = 5) -> float:
        """Total USD depth at top N levels. side='bid'|'ask'|'both'."""
        total = 0.0
        if side in ("bid", "both"):
            sorted_bids = sorted(self.bids, key=lambda x: x[0], reverse=True)[:levels]
            total += sum(p * q for p, q in sorted_bids)
        if side in ("ask", "both"):
            sorted_asks = sorted(self.asks, key=lambda x: x[0])[:levels]
            total += sum(p * q for p, q in sorted_asks)
        return total

    def spread_bps(self) -> float:
        """Current spread in basis points. Returns 9999.0 if stale."""
        try:
            bid, ask, spread = self.top_of_book()
            mid = (bid + ask) / 2
            return (spread / mid) * 10_000 if mid > 0 else 9999.0
        except Exception:
            return 9999.0
