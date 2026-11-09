"""Structured logging setup, with secret redaction on every log event."""

from __future__ import annotations

import logging
import os
import re
import sys

import structlog

SECRET_ENV_SUFFIXES = ("KEY", "TOKEN", "SECRET", "PASSWORD")
# Credential shapes, redacted even if they're not in the environment: Google API keys,
# Google OAuth access tokens, GitHub tokens, and bearer headers.
SECRET_PATTERNS = re.compile(
    r"AIza[0-9A-Za-z_\-]{30,}|ya29\.[0-9A-Za-z_\-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{30,}|(?i:bearer\s+)[A-Za-z0-9._\-]{20,}"
)
REDACTED = "<redacted>"


def secret_values() -> list[str]:
    """Values of secret-looking environment variables, longest first (8+ characters)."""
    values = {
        v
        for k, v in os.environ.items()
        if k.upper().endswith(SECRET_ENV_SUFFIXES) and len(v.strip()) >= 8
    }
    return sorted(values, key=len, reverse=True)


def redact(text: str, secrets: list[str] | None = None) -> str:
    for secret in secret_values() if secrets is None else secrets:
        text = text.replace(secret, REDACTED)
    return SECRET_PATTERNS.sub(REDACTED, text)


def _redact_value(value, secrets: list[str]):
    if isinstance(value, str):
        return redact(value, secrets)
    if isinstance(value, dict):
        return {k: _redact_value(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(v, secrets) for v in value)
    if isinstance(value, BaseException):
        return redact(f"{type(value).__name__}: {value}", secrets)
    return value


def redact_processor(logger, method_name, event_dict):
    """structlog processor: no event field may carry a secret, whatever its type."""
    secrets = secret_values()
    return {key: _redact_value(value, secrets) for key, value in event_dict.items()}


def configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            redact_processor,
            structlog.dev.ConsoleRenderer(colors=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
