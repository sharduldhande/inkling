"""Bearer auth and the user roster (§2).

v1 had one token; v2 gives each user their own and the token identifies the
user. CHAT_TOKENS_FILE holds `{"<user_id>": "<token>"}`.

That file is also the worker roster. It has to be: RLS scopes `users` to
`id = current_setting('app.user_id')`, so runtime_role cannot enumerate users,
and the alternatives (loosening that policy, or handing the app a BYPASSRLS
connection) both spend isolation the §4.11 suite exists to protect. The
trade-off is that a user with no chat token gets no worker.
"""

import json
from dataclasses import dataclass
from pathlib import Path
from secrets import compare_digest

from inkling.api.v1_contract import AUTH_SCHEME


@dataclass(frozen=True)
class Roster:
    tokens: dict[str, str]  # user_id -> bearer token

    @property
    def user_ids(self) -> list[str]:
        return list(self.tokens)

    def user_for_header(self, header: str | None) -> str | None:
        """Resolve an Authorization header to a user id, or None.

        Compared with compare_digest against every entry — no early exit on the
        first mismatching byte, and no dict lookup keyed by the secret.
        """
        if not header:
            return None
        match = None
        for user_id, token in self.tokens.items():
            if compare_digest(header, f"{AUTH_SCHEME} {token}"):
                match = user_id
        return match


def load_roster(path: str) -> Roster:
    if not path:
        raise ValueError("CHAT_TOKENS_FILE is not configured")
    raw = json.loads(Path(path).read_text())
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in raw.items()
    ):
        raise ValueError(f"{path}: expected an object of {{user_id: token}} strings")
    if not raw:
        raise ValueError(f"{path}: no users configured; nothing would run")
    return Roster(dict(raw))
