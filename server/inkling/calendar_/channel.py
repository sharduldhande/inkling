"""The 'calendar' outbox channel (§12 write path).

A CreateCalendarEvent command becomes a deliveries row (channel='calendar',
destination=calendar_id, body=JSON) at commit; this channel is what the §11
drainer routes those rows to. The insert is recorded in calendar_events_cache
as created_by_us immediately, so the next sync pass skips our own event
instead of narrating it back to the model.
"""

import json
import logging
from datetime import datetime

from inkling.calendar_.adapter import CalendarProvider
from inkling.db.client import Database
from inkling.db.queries import calendar as calendar_q

log = logging.getLogger(__name__)


class CalendarChannel:
    def __init__(self, db: Database, provider: CalendarProvider):
        self.db = db
        self.provider = provider

    async def send(
        self, *, topic: str, body: str, title: str = "", user_id: str = ""
    ) -> str:
        payload = json.loads(body)
        event = await self.provider.create_event(
            user_id=user_id,
            calendar_id=topic,
            summary=payload["summary"],
            starts_at=datetime.fromisoformat(payload["starts_at"]),
            ends_at=datetime.fromisoformat(payload["ends_at"]),
            description=payload.get("description"),
        )
        async with self.db.with_tenant(user_id) as conn:
            await calendar_q.cache_upsert(
                conn,
                user_id=user_id,
                provider_event_id=event.provider_event_id,
                calendar_id=topic,
                revision=event.revision,
                starts_at=event.starts_at,
                ends_at=event.ends_at,
                summary=event.summary,
                created_by_us=True,
                raw=event.raw,
            )
        return event.provider_event_id
