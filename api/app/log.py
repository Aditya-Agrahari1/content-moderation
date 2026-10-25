"""Structured JSON logs: one event per line, IDs as fields so a media item can be grepped end to end."""

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)

_logger = logging.getLogger("moderation")
_service = "api"


def configure(service: str) -> None:
    global _service
    _service = service
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.handlers[:] = [handler]
    _logger.setLevel(logging.INFO)
    _logger.propagate = False


def _emit(level: int, event: str, fields: dict) -> None:
    record = {"ts": datetime.now(UTC).isoformat(timespec="milliseconds"), "level": logging.getLevelName(level),
              "service": _service, "event": event}
    request_id = request_id_var.get()
    if request_id is not None:
        record["request_id"] = request_id
    record.update({k: (str(v) if not isinstance(v, (int, float, bool, list, dict, type(None))) else v)
                   for k, v in fields.items()})
    _logger.log(level, json.dumps(record))


def info(event: str, **fields) -> None:
    _emit(logging.INFO, event, fields)


def warning(event: str, **fields) -> None:
    _emit(logging.WARNING, event, fields)


def error(event: str, **fields) -> None:
    _emit(logging.ERROR, event, fields)
