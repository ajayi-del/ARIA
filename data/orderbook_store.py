import time
from collections import deque

class DataStaleError(Exception):
    pass

class OrderbookStore:
    __slots__ = ('symbol', 'bids', 'asks', 'last_update_ms', 'update_count',
                 '_level_ages_bid', '_level_ages_ask', '_cancel_events')

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bids: list[tuple[float, float]] = []
        self.asks: list[tuple[float, float]] = []
        self.last_update_ms: int | None = None
        self.update_count: int = 0
        # Level persistence tracking for queue-position proxy (P3)
        self._level_ages_bid: dict[float, int] = {}
        self._level_ages_ask: dict[float, int] = {}
        # Cancel-rate velocity tracking (P4) — rolling window of (timestamp_s, volume)
        self._cancel_events: deque = deque(maxlen=200)

    def update(self, bids: list[tuple[float, float]], asks: list[tuple[float, float]], timestamp_ms: int) -> None:
        self.bids = bids
        self.asks = asks
        self.last_update_ms = timestamp_ms
        self.update_count += 1
        # Reset persistence tracking on full snapshot — all levels are "new"
        self._level_ages_bid = {p: timestamp_ms for p, _ in bids}
        self._level_ages_ask = {p: timestamp_ms for p, _ in asks}
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
        Tracks level persistence ages for queue-position proxy weighting.
        """
        bid_map = {p: q for p, q in self.bids}
        ask_map = {p: q for p, q in self.asks}

        _now_s = timestamp_ms / 1000.0
        for price, qty in bid_diffs:
            if qty == 0:
                _removed = bid_map.pop(price, None)
                self._level_ages_bid.pop(price, None)
                if _removed:
                    self._cancel_events.append((_now_s, _removed))
            else:
                # Only stamp age on NEW levels (not existing ones)
                if price not in bid_map:
                    self._level_ages_bid[price] = timestamp_ms
                bid_map[price] = qty

        for price, qty in ask_diffs:
            if qty == 0:
                _removed = ask_map.pop(price, None)
                self._level_ages_ask.pop(price, None)
                if _removed:
                    self._cancel_events.append((_now_s, _removed))
            else:
                if price not in ask_map:
                    self._level_ages_ask[price] = timestamp_ms
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

    def weighted_imbalance(self, depth: int = 5, min_age_ms: int = 30_000) -> float:
        """
        Queue-position proxy imbalance.
        Levels that have persisted longer are weighted higher (treated as front-of-queue).
        New levels (< min_age_ms) are discounted — they are likely back-of-queue spoofing.
        """
        if self.last_update_ms is None or len(self.bids) == 0 or len(self.asks) == 0:
            return 0.0

        now_ms = self.last_update_ms
        sorted_bids = sorted(self.bids, key=lambda x: x[0], reverse=True)[:depth]
        sorted_asks = sorted(self.asks, key=lambda x: x[0])[:depth]

        bid_vol = 0.0
        for price, size in sorted_bids:
            age_ms = now_ms - self._level_ages_bid.get(price, now_ms)
            weight = min(1.0, age_ms / min_age_ms)
            bid_vol += size * weight

        ask_vol = 0.0
        for price, size in sorted_asks:
            age_ms = now_ms - self._level_ages_ask.get(price, now_ms)
            weight = min(1.0, age_ms / min_age_ms)
            ask_vol += size * weight

        if bid_vol + ask_vol == 0:
            return 0.0
        return (bid_vol - ask_vol) / (bid_vol + ask_vol)

    def cancel_velocity(self, window_sec: float = 1.0) -> float:
        """
        Cancel-rate velocity proxy: volume removed per second from the order book.
        High cancel velocity + low persistence-weighted depth = algorithmic cascade.
        Returns contracts/second removed in the last window.
        """
        if not self._cancel_events:
            return 0.0
        now_s = time.time()
        boundary = now_s - window_sec
        total = 0.0
        # Evict stale events from left side (deque is append-only)
        while self._cancel_events and self._cancel_events[0][0] < boundary:
            self._cancel_events.popleft()
        for _, vol in self._cancel_events:
            total += vol
        return total / window_sec

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
