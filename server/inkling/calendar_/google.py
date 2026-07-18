"""Google Calendar adapter (§12).

Code-complete but necessarily untested against the live API: OAuth client
credentials are a pending human decision (§19.2), and the system runs fine
with calendar off. Everything decision-shaped (diffing, gating, outbox) lives
in sync.py/outbox.py and is tested against StubProvider; this module is only
the HTTP shape of Google's v3 API.

Auth: per-user OAuth token file (calendar_sync_state.oauth_token_file, §16 —
`google-<user>.json` holding {"refresh_token": ...}); access tokens are
refreshed on demand with GOOGLE_OAUTH_CLIENT_ID/SECRET and cached in memory
only. Scopes: calendar.events (read + insert). Google cannot split delete out
of the write scope, so create-only is enforced here: no delete method exists.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from inkling.calendar_.adapter import CursorGone, NormalizedEvent, SyncPage

log = logging.getLogger(__name__)

API = "https://www.googleapis.com/calendar/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CHANNEL_TTL = timedelta(days=7)


def _parse_when(when: dict[str, Any] | None) -> datetime | None:
    if not when:
        return None
    if "dateTime" in when:
        return datetime.fromisoformat(when["dateTime"])
    if "date" in when:  # all-day: midnight UTC of that date
        return datetime.fromisoformat(when["date"]).replace(tzinfo=timezone.utc)
    return None


class GoogleCalendar:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_file_for: dict[str, str],  # user_id -> oauth token file path
        timeout: float = 15.0,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_file_for = token_file_for
        self._client = httpx.AsyncClient(timeout=timeout)
        self._access: dict[str, tuple[str, datetime]] = {}  # user -> (token, expiry)

    async def close(self) -> None:
        await self._client.aclose()

    async def _token(self, user_id: str) -> str:
        cached = self._access.get(user_id)
        if cached and cached[1] > datetime.now(timezone.utc) + timedelta(minutes=2):
            return cached[0]
        path = self.token_file_for.get(user_id)
        if not path:
            raise RuntimeError(f"no oauth token file configured for {user_id}")
        refresh_token = json.loads(Path(path).read_text())["refresh_token"]
        response = await self._client.post(TOKEN_URL, data={
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
        response.raise_for_status()
        body = response.json()
        token = body["access_token"]
        expiry = datetime.now(timezone.utc) + timedelta(
            seconds=int(body.get("expires_in", 3600))
        )
        self._access[user_id] = (token, expiry)
        return token

    async def _get(self, user_id: str, url: str, params: dict[str, Any]) -> httpx.Response:
        token = await self._token(user_id)
        return await self._client.get(
            url, params=params, headers={"Authorization": f"Bearer {token}"}
        )

    async def connect(self, *, user_id: str, calendar_id: str) -> None:
        await self._token(user_id)  # proves the refresh token works

    async def _page_events(
        self, *, user_id: str, calendar_id: str, params: dict[str, Any]
    ) -> SyncPage:
        events: list[NormalizedEvent] = []
        cursor: str | None = None
        while True:
            response = await self._get(
                user_id, f"{API}/calendars/{calendar_id}/events", params
            )
            if response.status_code == 410:
                raise CursorGone(params.get("syncToken", ""))
            response.raise_for_status()
            body = response.json()
            events.extend(
                self.normalize(item, calendar_id=calendar_id)
                for item in body.get("items", [])
            )
            if "nextPageToken" in body:
                params = {**params, "pageToken": body["nextPageToken"]}
                continue
            cursor = body.get("nextSyncToken")
            return SyncPage(events=events, next_cursor=cursor)

    async def initial_sync(
        self, *, user_id: str, calendar_id: str,
        window_start: datetime, window_end: datetime,
    ) -> SyncPage:
        return await self._page_events(
            user_id=user_id, calendar_id=calendar_id,
            params={
                "timeMin": window_start.isoformat(),
                "timeMax": window_end.isoformat(),
                "singleEvents": "true",
                "maxResults": "250",
            },
        )

    async def incremental_sync(
        self, *, user_id: str, calendar_id: str, cursor: str
    ) -> SyncPage:
        return await self._page_events(
            user_id=user_id, calendar_id=calendar_id,
            params={"syncToken": cursor, "maxResults": "250"},
        )

    async def renew_channel(
        self, *, user_id: str, calendar_id: str, webhook_url: str
    ) -> tuple[str, datetime]:
        token = await self._token(user_id)
        channel_id = str(uuid4())
        expires = datetime.now(timezone.utc) + CHANNEL_TTL
        response = await self._client.post(
            f"{API}/calendars/{calendar_id}/events/watch",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "id": channel_id,
                "type": "web_hook",
                "address": webhook_url,
                "expiration": str(int(expires.timestamp() * 1000)),
            },
        )
        response.raise_for_status()
        body = response.json()
        actual_expiry = datetime.fromtimestamp(
            int(body.get("expiration", expires.timestamp() * 1000)) / 1000,
            tz=timezone.utc,
        )
        return channel_id, actual_expiry

    async def create_event(
        self, *, user_id: str, calendar_id: str, summary: str,
        starts_at: datetime, ends_at: datetime, description: str | None,
    ) -> NormalizedEvent:
        token = await self._token(user_id)
        response = await self._client.post(
            f"{API}/calendars/{calendar_id}/events",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "summary": summary,
                "description": description,
                "start": {"dateTime": starts_at.isoformat()},
                "end": {"dateTime": ends_at.isoformat()},
            },
        )
        response.raise_for_status()
        return self.normalize(response.json(), calendar_id=calendar_id)

    def normalize(self, raw: dict[str, Any], *, calendar_id: str) -> NormalizedEvent:
        return NormalizedEvent(
            provider_event_id=raw["id"],
            calendar_id=calendar_id,
            revision=raw.get("etag"),
            summary=raw.get("summary"),
            starts_at=_parse_when(raw.get("start")),
            ends_at=_parse_when(raw.get("end")),
            status=raw.get("status", "confirmed"),
            raw=raw,
        )
