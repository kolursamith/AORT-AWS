"""Structured JSON logging with secret redaction.

Every log line is one JSON object so CloudWatch Logs Insights can query it.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_REDACT_KEYS = re.compile(
    r"(secret|password|token|signature|authorization|api[_-]?key|credential)", re.I
)
_REDACT_INLINE = re.compile(r"(postgresql(?:\+\w+)?://[^:/@\s]+:)([^@\s]+)(@)")


def redact(value):
    """Recursively redact secret-looking keys and inline DSN passwords."""
    if isinstance(value, dict):
        return {k: ("***" if _REDACT_KEYS.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _REDACT_INLINE.sub(r"\1***\3", value)
    return value


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str):
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(service: str, level: str = "INFO") -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_aorts", False):
            root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service))
    handler._aorts = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    root.setLevel(level.upper())
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def log_event(logger: logging.Logger, msg: str, level: int = logging.INFO, **fields) -> None:
    logger.log(level, msg, extra={"fields": fields})
