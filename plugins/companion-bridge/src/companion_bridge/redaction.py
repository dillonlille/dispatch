from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

_SECRET_KEY_SUFFIXES = (
    "_token",
    "_secret",
    "_password",
    "_cookie",
    "_csrf",
    "_session",
    "_api_key",
    "_connection_string",
)
_SECRET_KEYS = {
    "authorization",
    "cookie",
    "csrf",
    "token",
    "session",
    "secret",
    "password",
    "api_key",
    "connection_string",
}

_PATTERNS = (
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]+"), "[REDACTED_SLACK_TOKEN]"),
    (re.compile(r"xapp-[A-Za-z0-9-]+"), "[REDACTED_SLACK_APP_TOKEN]"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"), r"\1[REDACTED_BEARER]"),
    (re.compile(r"(?i)(anti-csrftoken-a2z\s*[:=]\s*)[^\s,;]+"), r"\1[REDACTED_CSRF]"),
    (re.compile(r"(?i)((?:csrf|token|secret|api[_-]?key|connection[_-]?string)\s*[:=]\s*)[^\s,;]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)((?:set-)?cookie\s*[:=]\s*)[^\r\n]+"), r"\1[REDACTED_COOKIE]"),
    (re.compile(r"(?i)\b(session-id|session-token|ubid-main|at-main|sess-at-main)\s*=\s*[^\s,;]+"), r"\1=[REDACTED_COOKIE]"),
    (re.compile(r"(?i)(password\s*[:=]\s*)[^\s,;]+"), r"\1[REDACTED_PASSWORD]"),
    (re.compile(r'(?i)("(?:cookie|csrf|token|session|secret|password|api[_-]?key|connection[_-]?string)"\s*:\s*")[^"]+("?)'), r"\1[REDACTED_SECRET]\2"),
    (re.compile(r"(?i)('(?:cookie|csrf|token|session|secret|password|api[_-]?key|connection[_-]?string)'\s*:\s*')[^']+('?)"), r"\1[REDACTED_SECRET]\2"),
)


def redact_secrets(value: Any) -> str:
    rendered = str(_redact_structure(value))
    for pattern, replacement in _PATTERNS:
        rendered = pattern.sub(replacement, rendered)
    return rendered


def _redact_structure(value: Any, *, depth: int = 0) -> Any:
    if depth > 8:
        return "[REDACTED_DEPTH]"
    if isinstance(value, Mapping):
        return {
            key: "[REDACTED_SECRET]"
            if _is_secret_key(key)
            else _redact_structure(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_redact_structure(item, depth=depth + 1) for item in value]
    return value


def _is_secret_key(value: Any) -> bool:
    normalized = str(value).strip().lower().replace("-", "_")
    return normalized in _SECRET_KEYS or normalized.endswith(_SECRET_KEY_SUFFIXES)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_secrets(record.getMessage())
        record.args = ()
        return True
