"""Logging setup (§15): structured JSON lines for journald, plain for dev.

JSON mode emits one object per line — ts/level/logger/msg (+exc when present)
— which journald stores verbatim and `journalctl -o json` then nests. The
interesting per-turn fields (trigger id, token counts, model, duration,
accept/reject counts) ride in the message text of dedicated log lines from
context.py, worker.py, and commit.py.
"""

import json
import logging
import time


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(level: str, fmt: str) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    handler = logging.StreamHandler()
    if fmt.lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
    root.handlers[:] = [handler]
