"""structlog JSON logging (plan §21). Token-like values are redacted (§20)."""

from __future__ import annotations

import logging
import re
from collections.abc import MutableMapping
from typing import Any

import structlog

_SECRET_KEYS = re.compile(r"(token|secret|password|api_key|authorization|dsn)", re.I)
_SECRET_VALUES = re.compile(r"(EAA[A-Za-z0-9]{20,}|ya29\.[A-Za-z0-9_\-]+|1//[A-Za-z0-9_\-]{20,})")


def redact(_: Any, __: str, event: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key, value in list(event.items()):
        if _SECRET_KEYS.search(key):
            event[key] = "***"
        elif isinstance(value, str):
            event[key] = _SECRET_VALUES.sub("***", value)
    return event


def configure_logging(level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
    )
