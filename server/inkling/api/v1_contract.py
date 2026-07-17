"""The v1 wire contract, copied verbatim — do not edit to taste.

D8 preserves the v1 iOS app contract at cutover: the app talks to v2
unmodified, so these bytes are load-bearing. §19.6 says copy them from the live
server.js rather than guess, because a plausible-but-wrong string passes our
tests and fails against the real client.

Source: sharduldhande/habit-enforcer-server @ 81ca873, server.js (v1 is the
Habit Enforcer server that Inkling replaces).

Deliberate fidelity notes:
- v1 compares the Authorization header with `=== \"Bearer \" + TOKEN`, an exact
  byte match: a lowercase \"bearer\" scheme fails there and must fail here too.
- 401 and 400 bodies are bare strings with no Content-Type; 429 sets
  text/plain. Reproduced exactly rather than normalized.
- v1's `busy` is one global boolean because v1 had one user. §6 keeps the
  response shape and redefines it per-token.
"""

# POST /chat while a turn is running (§5.1).
BUSY_STATUS = 429
BUSY_BODY = "brain is busy, try again in a minute"
BUSY_MEDIA_TYPE = "text/plain"

# Failed `authed(req)` on /chat and /history.
UNAUTHORIZED_BODY = "unauthorized"

# Missing or non-string `message` in the /chat body.
BAD_REQUEST_BODY = "bad request"

# v1: `if (body.length > 65536) req.destroy()`.
MAX_BODY_BYTES = 65536

# v1: `if (lines.length > 200) lines = lines.slice(-200)`.
HISTORY_MAX = 200

# v1 SSE response headers.
SSE_HEADERS = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
}

AUTH_SCHEME = "Bearer"
