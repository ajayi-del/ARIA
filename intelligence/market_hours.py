import calendar
from datetime import datetime, time, timedelta
import pytz

# Asset class routing — import from core.asset_classes (single source of truth)
# Falls back to inline dict if import fails (backwards compatibility)
try:
    from core.asset_classes import ASSET_CLASS as _AC_IMPORTED
    # Re-export as module-level ASSET_CLASS for external consumers
    ASSET_CLASS = _AC_IMPORTED
except ImportError:
    # Fallback to legacy mapping — keeps existing tests green during transition
    ASSET_CLASS = {
        "BTC-USD": "crypto", "ETH-USD": "crypto", "SOL-USD": "crypto",
        "BNB-USD": "crypto", "LINK-USD": "crypto", "AVAX-USD": "crypto",
        "SUI-USD": "crypto", "ARB-USD": "crypto", "OP-USD": "crypto",
        "NEAR-USD": "crypto", "MNT-USD": "crypto", "1000PEPE-USD": "crypto",
        "XAUT-USD": "commodity", "CL-USD": "commodity", "COPPER-USD": "commodity",
        "TSM-USD": "equity", "ORCL-USD": "equity", "NVDA-USD": "equity",
        "AAPL-USD": "equity", "TSLA-USD": "equity",
        "USTECH100-USD": "equity_index",
    }

# SoDEX perpetual contracts trade 24/7 regardless of underlying market hours.
# These instruments have equity/commodity underliers but the perp market never closes.
_SODEX_24H_OVERRIDE: frozenset = frozenset({
    "XAUT-USD", "CL-USD", "COPPER-USD", "SILVER-USD", "BRENT-USD", "WTI-USD",
    "TSM-USD", "ORCL-USD", "NVDA-USD", "MSFT-USD", "AAPL-USD",
    "AMZN-USD", "GOOGL-USD", "META-USD", "TSLA-USD",
    "USTECH-USD", "SPX-USD", "MAG7-USD",
})

# Bybit 8h funding reset hours (UTC). Rates update, longs/shorts reposition.
BYBIT_FUNDING_RESET_HOURS_UTC = (0, 8, 16)


class MarketHoursGate:
    """
    Unified market session intelligence for ARIA and PHANTOM.

    Answers three questions for each symbol:
      1. Is this asset tradeable right now? (hard gate — closed = no trade)
      2. What is the session quality multiplier? (soft — weekend / pre-mkt / off-hours)
      3. What macro temporal patterns apply? (weekly/monthly/funding-reset)

    Asset-class logic:
      - Crypto:        24/7, size_mult=0.75 on weekends (thin L2, higher slippage)
      - Gold/Commodity: Mon 23:00 – Fri 22:00 UTC, 22–23 UTC daily maintenance
      - Equity index:  Mon–Fri, pre-market 08–14:30 UTC, regular 14:30–21 UTC
    """

    def __init__(self):
        pass

    # ──────────────────────────────────────────────────────────────────────────
    # Hard gates — is the market open at all?
    # ──────────────────────────────────────────────────────────────────────────

    def is_gold_market_open(self, dt: datetime = None) -> bool:
        """True if XAUT gold market is open (Mon 23:00 – Fri 22:00 UTC, ±maint)."""
        dt = self._utc(dt)
        weekday = dt.weekday()  # 0=Mon, 6=Sun
        hour = dt.hour

        if weekday == 5:              # Saturday: always closed
            return False
        if weekday == 6:              # Sunday: opens 23:00 UTC
            return hour >= 23
        if weekday == 4 and hour >= 22:  # Friday: closes 22:00 UTC
            return False
        if hour == 22:                # Daily maintenance 22:00–23:00 UTC
            return False
        return True

    def get_ustech_session(self, dt: datetime = None) -> str:
        """
        Returns USTECH session: "regular", "pre_market", "after_hours", "closed".
          Regular:     14:30–21:00 UTC  (NYSE hours)
          Pre-market:  08:00–14:30 UTC
          After-hours: 21:00–00:00 UTC
          Closed:      00:00–08:00 UTC, all weekends
        """
        dt = self._utc(dt)
        if dt.weekday() >= 5:
            return "closed"

        td = dt.hour + dt.minute / 60.0
        if 14.5 <= td < 21.0:  return "regular"
        if 8.0 <= td < 14.5:   return "pre_market"
        if td >= 21.0:         return "after_hours"
        return "closed"  # 00:00–08:00 UTC

    def should_trade_symbol(self, symbol: str, dt: datetime = None) -> tuple[bool, str]:
        """
        Hard gate: (tradeable, reason).
        SoDEX perpetuals in _SODEX_24H_OVERRIDE trade 24/7 regardless of underlying hours.
        Returns False for non-SoDEX commodity symbols when CME session is closed.
        Returns False for non-SoDEX equity symbols outside US market hours.
        Crypto is always tradeable (weekend handled via soft multiplier).
        """
        if symbol in _SODEX_24H_OVERRIDE:
            return True, "sodex_perp_24_7"

        asset_class = ASSET_CLASS.get(symbol, "crypto")

        # "commodity" is the new canonical name; "gold" kept for backward compat
        if asset_class in ("commodity", "gold"):
            if not self.is_gold_market_open(dt):
                return False, "COMMODITY_MARKET_CLOSED"
            return True, "commodity_market_open"

        # "equity" stocks + "equity_index" all use US market hours
        if asset_class in ("equity", "equity_index"):
            session = self.get_ustech_session(dt)
            if session == "closed":
                return False, "EQUITY_MARKET_CLOSED"
            return True, f"equity_{session}"

        return True, "crypto_24_7"

    # ──────────────────────────────────────────────────────────────────────────
    # Soft multipliers — session quality, timing patterns
    # ──────────────────────────────────────────────────────────────────────────

    def get_session_context(self, symbol: str, dt: datetime = None) -> dict:
        """
        Full session context dict:
          active:     bool   — False = skip trade entirely
          session:    str    — human-readable session name
          size_mult:  float  — position size multiplier (1.0 = normal)
          reason:     str
        """
        dt = self._utc(dt)

        if symbol in _SODEX_24H_OVERRIDE:
            return {"active": True, "session": "sodex_perp", "size_mult": 1.0,
                    "reason": "sodex_perp_24_7"}

        asset_class = ASSET_CLASS.get(symbol, "crypto")

        if asset_class in ("commodity", "gold"):
            ok = self.is_gold_market_open(dt)
            if not ok:
                return {"active": False, "session": "closed", "size_mult": 0.0,
                        "reason": "COMMODITY_MARKET_CLOSED"}
            return {"active": True, "session": "commodity_open", "size_mult": 1.0,
                    "reason": "commodity_market_open"}

        if asset_class in ("equity", "equity_index"):
            session = self.get_ustech_session(dt)
            mult_map = {
                "regular":     1.0,
                "pre_market":  0.5,   # Lower liquidity, higher spread
                "after_hours": 0.4,
                "closed":      0.0,
            }
            return {
                "active": session != "closed",
                "session": session,
                "size_mult": mult_map.get(session, 0.0),
                "reason": f"equity_{session}"
            }

        # Crypto — 24/7, no weekend restriction on SoDEX perps
        weekday = dt.weekday()
        if weekday >= 5:
            return {"active": True, "session": "weekend",
                    "size_mult": 1.0, "reason": "crypto_24_7"}

        crypto_sess = self.get_crypto_session(dt)
        # US session highest volume → full size; Asian → slightly reduced
        sess_mult = {"us": 1.0, "us_overlap": 0.95, "european": 1.0, "asian": 0.90}
        return {
            "active": True,
            "session": crypto_sess,
            "size_mult": sess_mult.get(crypto_sess, 1.0),
            "reason": f"crypto_{crypto_sess}_session"
        }

    def get_crypto_session(self, dt: datetime = None) -> str:
        """Crypto trading session by dominant region (UTC clock)."""
        dt = self._utc(dt)
        hour = dt.hour
        if dt.weekday() >= 5:
            return "weekend"
        if 0 <= hour < 8:
            return "us_overlap"   # US closing + Asia opening — can be choppy
        if 8 <= hour < 14:
            return "european"
        if 14 <= hour < 21:
            return "us"
        return "us_overlap"       # 21–24 UTC: US late / Asia early

    def get_weekly_pattern_factor(self, symbol: str, dt: datetime = None) -> float:
        """
        Day-of-week and intra-month patterns for informed sizing.

        Rationale:
        - Monday first 2h: gap-risk settling for stocks; institutional warm-up for crypto
        - Friday close: end-of-week squaring — longs liquidated into close
        - Last 2 trading days of month: index/fund rebalancing → sharp, non-directional moves
        - First trading day of month: institutional re-allocation inflows
        - Triple witching (3rd Friday of each quarter): equity index volatility spike

        Returns a multiplier between 0.60 and 1.0.
        """
        dt = self._utc(dt)
        weekday = dt.weekday()  # 0=Mon, 6=Sun
        hour = dt.hour
        day = dt.day
        month = dt.month
        _, last_day = calendar.monthrange(dt.year, month)

        factor = 1.0
        asset_class = ASSET_CLASS.get(symbol, "crypto")

        # Monday caution (first 2 hours)
        if weekday == 0 and hour < 2:
            factor *= 0.85 if asset_class == "crypto" else 0.75

        # Friday squaring
        if weekday == 4:
            if asset_class in ("equity", "equity_index") and hour >= 19:
                factor *= 0.75   # Last 2h before NYSE close — heavy
            elif asset_class == "crypto" and hour >= 20:
                factor *= 0.88

        # End of month: last 2 calendar days
        if last_day - day <= 1 and weekday < 5:
            if asset_class in ("equity", "equity_index"):
                factor *= 0.70   # Rebalancing causes whipsaw
            elif asset_class in ("commodity", "gold"):
                factor *= 0.82
            else:
                factor *= 0.88   # Crypto follows institutional flows

        # First trading day of month: small opportunity bias
        if day <= 2 and weekday < 5:
            factor = min(factor * 1.05, 1.0)

        # Triple witching: 3rd Friday of March/June/September/December
        if weekday == 4 and month in (3, 6, 9, 12):
            # Find 3rd Friday: day must be between 15–21
            if 15 <= day <= 21:
                if asset_class in ("equity", "equity_index"):
                    factor *= 0.60   # Very high equity volatility — dangerous
                elif asset_class == "crypto":
                    factor *= 0.85   # Correlated via BTC ETF mechanics

        # Monthly OPEX: 3rd Friday of every month (equity options expiry)
        # Equities see pinning, whipsaws, and gamma-driven volatility every month —
        # not just quarterly. Crypto follows via ETF options correlation since 2024.
        elif weekday == 4 and 15 <= day <= 21:
            if asset_class in ("equity", "equity_index"):
                factor *= 0.80   # Options pinning + expiry gamma — moderate reduction
            elif asset_class == "crypto":
                factor *= 0.92   # Mild correlation via BTC options (Deribit/CME)

        return max(factor, 0.50)  # Floor at 50%

    def get_funding_proximity_mult(self, dt: datetime = None) -> float:
        """
        Soft multiplier near Bybit 8h funding resets (00:00, 08:00, 16:00 UTC).

        Funding payments cause shorts/longs to close positions minutes before reset
        and reopen after. This creates false microstructure signals (sweep/VPIN)
        that aren't real directional moves — reduce size during these windows.

        Returns 0.85–1.0.
        """
        dt = self._utc(dt)
        mins_to_reset = self._minutes_to_funding_reset(dt)
        if mins_to_reset <= 15:
            return 0.85   # Very close — positioning whipsaw risk
        if mins_to_reset <= 30:
            return 0.90
        return 1.0

    def get_combined_multiplier(self, symbol: str, dt: datetime = None) -> float:
        """
        Convenience: session_mult × weekly_pattern_mult × funding_proximity_mult.
        Use this as a size adjuster before position sizing.
        """
        dt = self._utc(dt)
        ctx = self.get_session_context(symbol, dt)
        if not ctx["active"]:
            return 0.0

        session_m = ctx["size_mult"]
        weekly_m  = self.get_weekly_pattern_factor(symbol, dt)
        funding_m = self.get_funding_proximity_mult(dt)

        return session_m * weekly_m * funding_m

    def get_next_gold_open(self, dt: datetime = None) -> datetime:
        """Returns UTC datetime of next gold market open."""
        dt = self._utc(dt)
        if self.is_gold_market_open(dt):
            return dt
        weekday = dt.weekday()
        if weekday == 5 or (weekday == 4 and dt.hour >= 22):
            # Friday night or Saturday → Sunday 23:00
            days_to_sunday = (6 - weekday) % 7
            return (dt + timedelta(days=days_to_sunday)).replace(
                hour=23, minute=0, second=0, microsecond=0)
        if weekday == 6 and dt.hour < 23:
            return dt.replace(hour=23, minute=0, second=0, microsecond=0)
        # Daily maintenance gap — next hour
        return dt.replace(hour=23, minute=0, second=0, microsecond=0)

    # ──────────────────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────────────────

    def _utc(self, dt: datetime = None) -> datetime:
        if dt is None:
            return datetime.now(pytz.UTC)
        if dt.tzinfo is None:
            return pytz.UTC.localize(dt)
        return dt

    def _minutes_to_funding_reset(self, dt: datetime) -> int:
        """Minutes to the nearest upcoming Bybit 8h funding reset."""
        hour = dt.hour
        minute = dt.minute
        # Find next reset hour
        for reset_h in sorted(BYBIT_FUNDING_RESET_HOURS_UTC):
            if reset_h * 60 > hour * 60 + minute:
                return reset_h * 60 - hour * 60 - minute
        # No reset remaining today → first reset next day
        first_reset_h = min(BYBIT_FUNDING_RESET_HOURS_UTC)
        return (24 * 60 - hour * 60 - minute) + first_reset_h * 60


# ── Module-level helpers ──────────────────────────────────────────────────────
# Canonical session name → max allowed personality in that session.
# "COIL" means: only arb/funding trades; no directional entries.
# Used by equity_coil_outside_hours and commodity_daily_break tests.

SESSION_PERSONALITY_MAX: dict = {
    "always_open":   "APEX",      # crypto 24/7 — no session restriction
    "regular":       "APEX",      # US equity regular hours — full personality
    "pre_market":    "FLOW",      # reduced liquidity — cautious directional ok
    "after_hours":   "COIL",      # after-hours: too thin for directional, arb only
    "closed":        "COIL",      # market closed — arb only, no directional
    "break":         "COIL",      # daily maintenance window — no directional
    "commodity_open":"APEX",      # commodity open — full personality
    "weekend":       "FLOW",      # crypto weekend — reduced but not arb-only
}

_gate = MarketHoursGate()


def get_asset_session(symbol: str, dt: datetime = None) -> str:
    """
    Return the canonical session string for a symbol at a given UTC datetime.

    Session strings:
      "always_open"    — crypto 24/7
      "regular"        — US equity/index regular hours (14:30–21:00 UTC)
      "pre_market"     — US equity pre-market (08:00–14:30 UTC)
      "after_hours"    — US equity after-hours (21:00–00:00 UTC)
      "closed"         — US equity closed (weekends or 00:00–08:00 UTC)
      "commodity_open" — commodity market open (Mon 23:00–Fri 22:00 UTC)
      "break"          — commodity daily maintenance (22:00–23:00 UTC)
      "weekend"        — crypto weekend session (soft restriction)
    """
    asset_class = ASSET_CLASS.get(symbol, "crypto")
    dt_utc = _gate._utc(dt)

    if asset_class == "crypto":
        # Weekend = reduced but never closed
        if dt_utc.weekday() >= 5:
            return "weekend"
        return "always_open"

    if asset_class in ("commodity", "gold"):
        # Daily maintenance 22:00–23:00 UTC
        if dt_utc.hour == 22:
            return "break"
        if _gate.is_gold_market_open(dt_utc):
            return "commodity_open"
        return "closed"

    if asset_class in ("equity", "equity_index"):
        sess = _gate.get_ustech_session(dt_utc)
        return sess  # "regular" | "pre_market" | "after_hours" | "closed"

    return "always_open"
