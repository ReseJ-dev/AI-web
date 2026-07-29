"""Structured logging configuration."""

import logging
import sys
from collections.abc import Mapping

import structlog
from structlog.typing import EventDict, WrappedLogger

from app.core.settings import get_settings

_SECRET_FIELDS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "brave_search_api_key",
        "token",
        "x_subscription_token",
    }
)


def _is_secret_field(field_name: str | None) -> bool:
    """Recognize common credential field names without inspecting values."""
    normalized = (field_name or "").lower().replace("-", "_")
    return (
        normalized in _SECRET_FIELDS
        or normalized.endswith("_api_key")
        or normalized.endswith("_token")
    )


def _redact_value(value: object, field_name: str | None = None) -> object:
    """Recursively redact credentials before structured log rendering."""
    if _is_secret_field(field_name):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {str(key): _redact_value(item, str(key)) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(item) for item in value]
    return value


def redact_secrets(
    logger: WrappedLogger,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Structlog processor that removes API keys and authorization values."""
    return {key: _redact_value(value, key) for key, value in event_dict.items()}


def configure_logging() -> None:
    """Configure standard-library and structured logging for the application."""
    log_level = get_settings().log_level.upper()
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=log_level,
        force=True,
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_secrets,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping().get(log_level, logging.INFO)
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
