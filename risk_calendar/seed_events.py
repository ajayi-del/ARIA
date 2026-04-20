"""
risk_calendar/seed_events.py

Seeds current dynamic events and fundamental biases into calendar.db.
Run once to load current market context; AUGUR will handle future insertions.

Usage:
    python risk_calendar/seed_events.py
    python risk_calendar/seed_events.py --db logs/calendar.db
"""

import asyncio
import json
import sys
import time
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from risk_calendar.events import EventStore


async def seed_current_events(db_path: str = "logs/calendar.db") -> None:
    store = EventStore(db_path)
    await store.init_db()
    conn = await store.connect()

    now_ms   = int(time.time() * 1000)
    days_2   = 2  * 24 * 3600 * 1000
    days_30  = 30 * 24 * 3600 * 1000

    # ── Dynamic Events ────────────────────────────────────────────────────────

    dynamic_events = [
        # Iran Hormuz — monitoring only, no trade block
        (
            "IRAN_HORMUZ_TALKS_ISLAMABAD",
            "GEOPOLITICAL",
            json.dumps(["CL-USD", "XAUT-USD", "BTC-USD", "USTECH-USD"]),
            0.85,
            json.dumps({
                "CL-USD":     "bearish_if_deal",
                "XAUT-USD":   "bearish_if_deal",
                "BTC-USD":    "bullish_if_deal",
                "USTECH-USD": "bullish_if_deal",
            }),
            "MONITOR",
            1.0,
            now_ms,
            now_ms + days_2,
            "sosovalue_manual",
            "Islamabad round — step-by-step memo. Re-evaluate in 48h.",
        ),
        # TSMC guidance — persistent AI-demand context, not a caution event
        (
            "TSMC_GUIDANCE_BULLISH_2026",
            "EARNINGS_SURPRISE",
            json.dumps(["TSM-USD", "NVDA-USD", "USTECH-USD", "MSFT-USD", "GOOGL-USD"]),
            0.70,
            json.dumps({
                "TSM-USD":    "bullish",
                "NVDA-USD":   "bullish",
                "USTECH-USD": "bullish",
            }),
            "BOOST",
            1.0,
            now_ms,
            now_ms + days_30,
            "sosovalue_manual",
            "TSMC 2026 guidance confirmed AI demand. Semiconductor cycle positive.",
        ),
        # MAG7 earnings window context
        (
            "MAG7_EARNINGS_WINDOW_APR_MAY_2026",
            "EARNINGS_MAG7",
            json.dumps(["META-USD", "MSFT-USD", "GOOGL-USD", "AMZN-USD", "AAPL-USD", "NVDA-USD"]),
            0.80,
            json.dumps({}),
            "MONITOR",
            1.0,
            now_ms,
            now_ms + days_30,
            "sosovalue_manual",
            "Q2 MAG7 earnings: META/MSFT/GOOGL Apr 28-29, AMZN/AAPL May 1, NVDA May 28.",
        ),
        # Current macro regime context
        (
            "GEOPOLITICAL_REGIME_CURRENT",
            "REGIME_CONTEXT",
            json.dumps(["BTC-USD", "USTECH-USD", "XAUT-USD", "CL-USD"]),
            0.65,
            json.dumps({
                "USTECH-USD": "bullish",
                "BTC-USD":    "bullish",
                "XAUT-USD":   "bearish",
                "CL-USD":     "bearish",
            }),
            "MONITOR",
            1.0,
            now_ms,
            now_ms + days_2,
            "sosovalue_manual",
            "S&P record highs, NASDAQ 12-day win streak, AI/MAG7 capital rotation, "
            "Hormuz thaw. Tech-led risk-on regime.",
        ),
    ]

    for row in dynamic_events:
        await conn.execute("""
            INSERT OR REPLACE INTO dynamic_events
            (event_name, event_type, affected_assets, impact_score,
             direction_bias, behaviour, size_mult, created_at, expires_at, source, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, row)
        print(f"  dynamic_event: {row[0]} ({row[1]}) expires +{(row[8]-now_ms)//(3600000)}h")

    # ── Fundamental Bias ──────────────────────────────────────────────────────

    fundamental_biases = [
        # TSMC guidance: +coherence for AI/semiconductor names
        ("USTECH-USD", "long", 0.10, "TSMC_2026_guidance_bullish_AI", now_ms, now_ms + days_30),
        ("TSM-USD",    "long", 0.15, "TSMC_2026_guidance_direct",     now_ms, now_ms + days_30),
        ("NVDA-USD",   "long", 0.10, "TSMC_2026_guidance_AI_demand",  now_ms, now_ms + days_30),
        ("MSFT-USD",   "long", 0.05, "TSMC_2026_AI_cloud_demand",     now_ms, now_ms + days_30),
        ("GOOGL-USD",  "long", 0.05, "TSMC_2026_AI_cloud_demand",     now_ms, now_ms + days_30),
    ]

    for symbol, direction, add, reason, created, expires in fundamental_biases:
        await conn.execute("""
            INSERT OR REPLACE INTO fundamental_bias
            (symbol, bias_direction, coherence_add, reason, created_at, expires_at, source)
            VALUES (?,?,?,?,?,?,?)
        """, (symbol, direction, add, reason, created, expires, "sosovalue_manual"))
        expires_days = (expires - now_ms) // (24 * 3600 * 1000)
        print(f"  fundamental_bias: {symbol} +{add:.2f} coherence ({reason}) expires +{expires_days}d")

    await conn.commit()
    print("\nSeeded successfully.")
    print(f"DB: {db_path}")


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else "logs/calendar.db"
    asyncio.run(seed_current_events(db))
