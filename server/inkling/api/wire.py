"""Render persisted turn_events as v1 SSE frames (§5.5).

One renderer for both the live path and the Last-Event-ID replay path, so a
client that reconnects mid-turn sees the same bytes it would have seen live.
That is the whole reason this is a function and not inline string building in
the route.

v1 emitted exactly three frame shapes (server.js):
    data: {"text":"..."}      per text delta
    data: {"done":true}       once, on the model's result event
    data: {"exit":<code>}     once, on child close — always sent, then end

v2 adds an `error` turn_event kind, which has no v1 equivalent. It renders as
v1's own failure shape: the error text, then done, then a non-zero exit. v1 did
the same thing when the model died without output (`code === 1 && fullReply
=== ''`), so the app already handles it.
"""

import json
from collections.abc import Iterator
from typing import Any

EXIT_ERROR = 1


def frame(payload: dict[str, Any]) -> str:
    """One SSE frame. v1: `res.write('data: ' + JSON.stringify(x) + '\\n\\n')`."""
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def render(kind: str, data: dict[str, Any]) -> Iterator[str]:
    """Turn one persisted event into the frames v1 would have written."""
    if kind == "text_delta":
        yield frame({"text": data["text"]})
    elif kind == "done":
        yield frame({"done": True})
    elif kind == "exit":
        yield frame({"exit": data.get("exit", 0)})
    elif kind == "error":
        yield frame({"text": data.get("error", "the assistant hit an error")})
        yield frame({"done": True})
        yield frame({"exit": EXIT_ERROR})
    else:
        raise ValueError(f"unknown turn_event kind {kind!r}")


def is_terminal(kind: str) -> bool:
    """Kinds after which no more frames follow and the socket closes."""
    return kind in ("exit", "error")
