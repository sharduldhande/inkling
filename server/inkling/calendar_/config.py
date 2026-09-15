"""Per-user feed list: ~/.inkling/calendars-<user>.json (§12, §16).

The file holds the secret ICS addresses, so it lives beside the other per-user
credentials (chmod 600) and its URLs never enter the database — only the
feed ids and display names do.

    {"calendars": [
      {"id": "personal", "name": "Personal", "url": "https://calendar.google.com/calendar/ical/.../private-.../basic.ics"},
      {"id": "family",   "name": "Family",   "url": "webcal://p12-caldav.icloud.com/published/2/..."}
    ]}
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class FeedSpec:
    id: str
    name: str
    url: str


class FeedConfigError(ValueError):
    """The feed file is malformed; the user's calendar stays off."""


def _normalize_url(url: str) -> str:
    url = url.strip()
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.startswith("https://"):
        raise FeedConfigError(f"feed url must be https:// or webcal://, got {url[:24]!r}")
    return url


def parse_feeds(text: str) -> list[FeedSpec]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FeedConfigError(f"not valid JSON: {exc}") from exc
    entries = data.get("calendars") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise FeedConfigError('expected {"calendars": [...]}')
    feeds: list[FeedSpec] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise FeedConfigError(f"calendars[{i}] is not an object")
        feed_id = str(entry.get("id", "")).strip()
        name = str(entry.get("name", "")).strip()
        url = entry.get("url")
        if not _ID_RE.match(feed_id):
            raise FeedConfigError(
                f"calendars[{i}].id {feed_id!r} must be lowercase [a-z0-9_-]"
            )
        if feed_id in seen:
            raise FeedConfigError(f"duplicate calendar id {feed_id!r}")
        if not name:
            raise FeedConfigError(f"calendars[{i}] ({feed_id}) needs a name")
        if not isinstance(url, str) or not url.strip():
            raise FeedConfigError(f"calendars[{i}] ({feed_id}) needs a url")
        seen.add(feed_id)
        feeds.append(FeedSpec(id=feed_id, name=name, url=_normalize_url(url)))
    return feeds


def load_feeds(path: Path) -> list[FeedSpec]:
    return parse_feeds(path.read_text())


def feed_file(config_dir: str, user_id: str) -> Path:
    return Path(config_dir).expanduser() / f"calendars-{user_id}.json"
