import aiosqlite
import os
import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class CalendarEvent:
    event_type: str
    name: str
    event_time: datetime
    impact: str
    description: str
    source: str
    id: Optional[int] = None

class EventStore:
    """
    Asynchronous event store using aiosqlite.
    WAL mode enabled for concurrent read/write safety.
    """
    def __init__(self, db_path: str = "logs/calendar.db"):
        self.db_path = db_path
        self._conn = None
        
        # Ensure directory exists
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(db_path), exist_ok=True)

    async def connect(self):
        """Initializes connection and WAL mode."""
        if self._conn is None:
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
        return self._conn

    async def init_db(self) -> None:
        """Creates events, dynamic_events, and fundamental_bias tables if not exists."""
        conn = await self.connect()
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                name TEXT NOT NULL,
                event_time TIMESTAMP NOT NULL,
                impact TEXT NOT NULL,
                description TEXT,
                source TEXT NOT NULL,
                UNIQUE(name, event_time)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS dynamic_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                affected_assets TEXT NOT NULL,
                impact_score REAL DEFAULT 0.5,
                direction_bias TEXT,
                behaviour TEXT DEFAULT 'MONITOR',
                size_mult REAL DEFAULT 1.0,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                source TEXT DEFAULT 'manual',
                notes TEXT,
                UNIQUE(event_name, created_at)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS fundamental_bias (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                bias_direction TEXT NOT NULL DEFAULT 'long',
                coherence_add REAL NOT NULL DEFAULT 0.0,
                reason TEXT,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                source TEXT DEFAULT 'manual',
                UNIQUE(symbol, reason)
            )
        """)
        await conn.commit()

    async def get_dynamic_events(self, now_ms: int) -> list:
        """Returns all non-expired dynamic events."""
        conn = await self.connect()
        async with conn.execute(
            "SELECT * FROM dynamic_events WHERE expires_at > ? ORDER BY impact_score DESC",
            (now_ms,),
        ) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def get_fundamental_bias(self, symbol: str, now_ms: int) -> float:
        """Returns summed coherence_add for a symbol from non-expired bias rows."""
        conn = await self.connect()
        async with conn.execute(
            "SELECT COALESCE(SUM(coherence_add), 0.0) FROM fundamental_bias "
            "WHERE symbol = ? AND expires_at > ?",
            (symbol, now_ms),
        ) as cursor:
            row = await cursor.fetchone()
            return float(row[0]) if row else 0.0

    async def add_event(self, event: CalendarEvent) -> None:
        """Inserts a new event manually."""
        conn = await self.connect()
        await conn.execute("""
            INSERT OR IGNORE INTO events 
            (event_type, name, event_time, impact, description, source)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            event.event_type,
            event.name,
            event.event_time.isoformat(),
            event.impact,
            event.description,
            event.source
        ))
        await conn.commit()

    async def seed_events(self) -> None:
        """Seeds known 2026 events and recurring structural events."""
        events = []

        # ── Recurring: Weekend market closures (structural, not economic) ────────
        # XAUT-USD gold and USTECH100-USD equity index markets close each weekend.
        # Seeding these as calendar events gives the display and calendar engine
        # visibility into upcoming closures and allows pre-weekend size reduction.
        # Seeds the next 52 weeks (1 year rolling).
        from datetime import timedelta
        now_utc = datetime.now(timezone.utc)
        for week_offset in range(52):
            # Find the Friday of the current week then add weeks
            days_to_friday = (4 - now_utc.weekday()) % 7
            base_friday_equity = (now_utc + timedelta(days=days_to_friday + week_offset * 7)).replace(
                hour=21, minute=0, second=0, microsecond=0
            )
            # Friday 21:00 UTC: USTECH100 regular session ends (NYSE/CME equity close)
            events.append(CalendarEvent(
                "WEEKEND_CLOSE",
                "Weekend Market Closure – USTECH100",
                base_friday_equity,
                "MEDIUM",
                "USTECH100-USD equity index closed until Sunday 23:00 UTC. "
                "Crypto continues with reduced weekend liquidity (0.75× sizing).",
                "seeded_recurring"
            ))
            # Friday 22:00 UTC: CME gold session ends — 1h after equity close
            base_friday_xaut = base_friday_equity.replace(hour=22)
            events.append(CalendarEvent(
                "WEEKEND_CLOSE",
                "Weekend Market Closure – XAUT",
                base_friday_xaut,
                "MEDIUM",
                "XAUT-USD (gold) CME session closes at 22:00 UTC Friday. "
                "Reopens Sunday 23:00 UTC.",
                "seeded_recurring"
            ))
            # Sunday 23:00 UTC: gold/USTECH re-open
            base_sunday = (base_friday_equity + timedelta(days=2)).replace(hour=23)
            events.append(CalendarEvent(
                "WEEKEND_REOPEN",
                "Market Reopen – XAUT/USTECH100",
                base_sunday,
                "LOW",
                "Gold and equity index markets reopen. Full sizing restored.",
                "seeded_recurring"
            ))
        
        # FOMC 2026 dates (UTC 18:00)
        fomc_dates = [
            "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-10",
            "2026-07-29", "2026-09-16", "2026-11-04", "2026-12-16"
        ]
        for d in fomc_dates:
            events.append(CalendarEvent(
                "FOMC", "FOMC Statement & Rate Decision",
                datetime.fromisoformat(f"{d}T18:00:00").replace(tzinfo=timezone.utc),
                "HIGH", "Federal Open Market Committee meeting results", "seeded"
            ))

        # CPI 2026 dates (UTC 13:30)
        cpi_dates = [
            "2026-01-15", "2026-02-12", "2026-03-12", "2026-04-10",
            "2026-05-13", "2026-06-11", "2026-07-15", "2026-08-13",
            "2026-09-11", "2026-10-13", "2026-11-12", "2026-12-10"
        ]
        for d in cpi_dates:
            events.append(CalendarEvent(
                "CPI", "Consumer Price Index (CPI)",
                datetime.fromisoformat(f"{d}T13:30:00").replace(tzinfo=timezone.utc),
                "HIGH", "Inflation data release", "seeded"
            ))

        # NFP 2026 dates (UTC 13:30, first Friday)
        nfp_dates = [
            "2026-01-09", "2026-02-06", "2026-03-06", "2026-04-03",
            "2026-05-01", "2026-06-05", "2026-07-02", "2026-08-07",
            "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04"
        ]
        for d in nfp_dates:
            events.append(CalendarEvent(
                "NFP", "Non-Farm Payrolls (NFP)",
                datetime.fromisoformat(f"{d}T13:30:00").replace(tzinfo=timezone.utc),
                "HIGH", "Employment report", "seeded"
            ))

        # MAG7 Earnings 2026 — Q2 (April/May) and Q3 (July/Aug)
        # Q1 dates (Jan/Feb) already past — omitted to keep calendar clean
        earnings = [
            # Q2 2026 earnings (reporting April–May)
            ("TSLA",  "2026-04-22T20:00:00"),
            ("META",  "2026-04-29T20:00:00"),
            ("MSFT",  "2026-04-29T20:00:00"),
            ("AAPL",  "2026-05-01T20:00:00"),
            ("AMZN",  "2026-05-01T20:00:00"),
            ("GOOGL", "2026-04-28T20:00:00"),
            ("NVDA",  "2026-05-28T20:00:00"),
            # Q3 2026 earnings (reporting July–Aug)
            ("TSLA",  "2026-07-22T20:00:00"),
            ("META",  "2026-07-28T20:00:00"),
            ("MSFT",  "2026-07-28T20:00:00"),
            ("AAPL",  "2026-07-31T20:00:00"),
            ("AMZN",  "2026-07-31T20:00:00"),
            ("GOOGL", "2026-07-28T20:00:00"),
            ("NVDA",  "2026-08-27T20:00:00"),
        ]
        for ticker, dt_str in earnings:
            events.append(CalendarEvent(
                "EARNINGS_MAG7", f"{ticker} Earnings",
                datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc),
                "HIGH", f"{ticker} Quarterly Earnings Release", "seeded"
            ))

        # ── XAUT / Gold structural themes (2026 macro regime) ─────────────────────
        # Gold has decoupled from its traditional USD-inverse correlation in 2026.
        # Central bank buying (China, India, Russia) + de-dollarization flows have
        # created a structural bid independent of DXY/real-rates. These are NOT
        # economic data releases — they are regime-awareness events that remind the
        # system that XAUT is in a structurally bullish cycle, reducing short-biased
        # sizing and increasing long-biased sizing above normal.
        gold_themes = [
            # Gold-USD decoupling regime awareness (quarterly reminders)
            ("2026-04-01T00:00:00", "XAUT Gold Decoupling Regime",
             "Gold has decoupled from USD correlation. CB buying + de-dollarization "
             "creates structural long bias. Reduce XAUT short sizing 25%. "
             "Long setups have higher conviction in this regime."),
            ("2026-07-01T00:00:00", "XAUT Gold Decoupling Regime Q3",
             "Gold decoupling regime continues. Central bank accumulation ongoing."),
            ("2026-10-01T00:00:00", "XAUT Gold Decoupling Regime Q4",
             "Year-end gold demand surge typical. CB balance sheet positioning."),
            # US Treasury market stress → gold safe haven bid
            ("2026-04-20T00:00:00", "US Treasury Volatility Window",
             "High US Treasury issuance + potential foreign selling. Gold benefits "
             "from safe-haven demand. XAUT longs supported; crypto muted."),
            # IMF/World Bank Spring Meetings — gold policy discussion
            ("2026-04-21T14:00:00", "IMF Spring Meetings 2026",
             "IMF/World Bank Spring Meetings. Gold & reserve discussions. "
             "XAUT volatility elevated; monitor for policy signals."),
        ]
        for dt_str, name, desc in gold_themes:
            events.append(CalendarEvent(
                "GOLD_MACRO", name,
                datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc),
                "MEDIUM", desc, "seeded_gold_macro"
            ))

        for event in events:
            await self.add_event(event)

    async def get_upcoming(self, hours_ahead: int = 48, now_utc: datetime = None) -> List[CalendarEvent]:
        """Returns events within next hours_ahead sorted by event_time ascending."""
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        
        conn = await self.connect()
        async with conn.execute("""
            SELECT * FROM events 
            WHERE event_time > ? 
            ORDER BY event_time ASC
        """, (now_utc.isoformat(),)) as cursor:
            rows = await cursor.fetchall()
            upcoming = []
            for row in rows:
                event_time = datetime.fromisoformat(row["event_time"])
                if (event_time - now_utc).total_seconds() / 3600.0 <= hours_ahead:
                    upcoming.append(CalendarEvent(
                        id=row["id"],
                        event_type=row["event_type"],
                        name=row["name"],
                        event_time=event_time,
                        impact=row["impact"],
                        description=row["description"],
                        source=row["source"]
                    ))
            return upcoming

    async def get_nearest(
        self,
        event_types: List[str] = None,
        exclude_types: List[str] = None,
        now_utc: datetime = None,
    ) -> Optional[CalendarEvent]:
        """Returns the next upcoming event matching any of the given types."""
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)

        query = "SELECT * FROM events WHERE event_time > ? "
        params = [now_utc.isoformat()]

        if event_types:
            placeholders = ",".join(["?"] * len(event_types))
            query += f"AND event_type IN ({placeholders}) "
            params.extend(event_types)

        if exclude_types:
            ex_placeholders = ",".join(["?"] * len(exclude_types))
            query += f"AND event_type NOT IN ({ex_placeholders}) "
            params.extend(exclude_types)

        query += "ORDER BY event_time ASC LIMIT 1"
        
        conn = await self.connect()
        async with conn.execute(query, params) as cursor:
            row = await cursor.fetchone()
            if row:
                return CalendarEvent(
                    id=row["id"],
                    event_type=row["event_type"],
                    name=row["name"],
                    event_time=datetime.fromisoformat(row["event_time"]),
                    impact=row["impact"],
                    description=row["description"],
                    source=row["source"]
                )
        return None

    async def get_last_past(self, hours_back: int = 24, now_utc: datetime = None) -> Optional[CalendarEvent]:
        """Returns the recently passed event."""
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
            
        conn = await self.connect()
        async with conn.execute("""
            SELECT * FROM events 
            WHERE event_time <= ? 
            ORDER BY event_time DESC LIMIT 1
        """, (now_utc.isoformat(),)) as cursor:
            row = await cursor.fetchone()
            if row:
                event_time = datetime.fromisoformat(row["event_time"])
                if (now_utc - event_time).total_seconds() / 3600.0 <= hours_back:
                    return CalendarEvent(
                        id=row["id"],
                        event_type=row["event_type"],
                        name=row["name"],
                        event_time=event_time,
                        impact=row["impact"],
                        description=row["description"],
                        source=row["source"]
                    )
        return None

    async def close(self):
        """Closes the aiosqlite connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
