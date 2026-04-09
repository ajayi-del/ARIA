import sqlite3
import os
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
    def __init__(self, db_path: str = "logs/calendar.db"):
        self.db_path = db_path
        # Ensure directory exists
        if db_path != ":memory:":
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
        
        # Use a single connection for the lifetime of the store
        # check_same_thread=False allows background loops to query the store
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row

    def init_db(self) -> None:
        """Creates events table if not exists."""
        with self.conn:
            self.conn.execute("""
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

    def add_event(self, event: CalendarEvent) -> None:
        """Inserts a new event manually."""
        with self.conn:
            self.conn.execute("""
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

    def seed_events(self) -> None:
        """Seeds known 2026 events."""
        events = []
        
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

        # PCE 2026 dates (UTC 13:30, last Friday)
        pce_dates = [
            "2026-01-30", "2026-02-27", "2026-03-27", "2026-04-30",
            "2026-05-29", "2026-06-26", "2026-07-31", "2026-08-28",
            "2026-09-25", "2026-10-30", "2026-11-25", "2026-12-23"
        ]
        for d in pce_dates:
            events.append(CalendarEvent(
                "PCE", "Personal Consumption Expenditures (PCE)",
                datetime.fromisoformat(f"{d}T13:30:00").replace(tzinfo=timezone.utc),
                "HIGH", "Core inflation gauge", "seeded"
            ))

        # MAG7 Earnings 2026 Q1
        earnings = [
            ("NVDA", "2026-02-26T21:00:00"),
            ("AAPL", "2026-01-30T21:00:00"),
            ("MSFT", "2026-01-29T21:00:00"),
            ("META", "2026-01-29T21:00:00"),
            ("GOOGL", "2026-02-04T21:00:00"),
            ("AMZN", "2026-02-05T21:00:00"),
            ("TSLA", "2026-01-29T21:00:00")
        ]
        for ticker, dt_str in earnings:
            events.append(CalendarEvent(
                "EARNINGS_MAG7", f"{ticker} Earnings",
                datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc),
                "HIGH", f"{ticker} Quarterly Earnings Release", "seeded"
            ))

        for event in events:
            self.add_event(event)

    def get_upcoming(self, hours_ahead: int = 48, now_utc: datetime = None) -> List[CalendarEvent]:
        """Returns events within next hours_ahead sorted by event_time ascending."""
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
        
        cursor = self.conn.execute("""
            SELECT * FROM events 
            WHERE event_time > ? 
            ORDER BY event_time ASC
        """, (now_utc.isoformat(),))
        
        rows = cursor.fetchall()
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

    def get_nearest(self, event_types: List[str] = None, now_utc: datetime = None) -> Optional[CalendarEvent]:
        """Returns the next upcoming event matching any of the given types."""
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
            
        query = "SELECT * FROM events WHERE event_time > ? "
        params = [now_utc.isoformat()]
        
        if event_types:
            placeholders = ",".join(["?"] * len(event_types))
            query += f"AND event_type IN ({placeholders}) "
            params.extend(event_types)
            
        query += "ORDER BY event_time ASC LIMIT 1"
        
        cursor = self.conn.execute(query, params)
        row = cursor.fetchone()
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

    def get_last_past(self, hours_back: int = 24, now_utc: datetime = None) -> Optional[CalendarEvent]:
        """Returns the recently passed event."""
        if now_utc is None:
            now_utc = datetime.now(timezone.utc)
            
        cursor = self.conn.execute("""
            SELECT * FROM events 
            WHERE event_time <= ? 
            ORDER BY event_time DESC LIMIT 1
        """, (now_utc.isoformat(),))
        
        row = cursor.fetchone()
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
