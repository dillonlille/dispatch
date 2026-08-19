"""Closed request and response contracts for the read-only Paycom boundary."""
from __future__ import annotations

from datetime import date as _date
import json
import re
from typing import Any

ACTIONS = frozenset({"health", "meal_comparison", "audit"})
MEAL_ACTIONS = frozenset({"meal_comparison"})
ENVELOPE_FIELDS = frozenset({"ok", "action", "status", "data", "freshness", "delivery", "error"})
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_MAX_ERROR_TEXT = 256
_MAX_RESULT_BYTES = 64 * 1024


class ContractError(ValueError):
    """A request, response, or stored value violates the bounded contract."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        self.message = message or code
        super().__init__(self.message)


def validate_date(value: Any) -> str:
    if type(value) is not str or len(value) != 10 or _DATE_RE.fullmatch(value) is None:
        raise ContractError("invalid_input", "Dates must use YYYY-MM-DD.")
    try:
        parsed = _date.fromisoformat(value)
    except ValueError as exc:
        raise ContractError("invalid_input", "Dates must be real calendar dates.") from exc
    if parsed.isoformat() != value:
        raise ContractError("invalid_input", "Dates must use canonical YYYY-MM-DD form.")
    return value


def validate_request(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise ContractError("invalid_input", "The request must be a JSON object.")
    action = value.get("action")
    if type(action) is not str or action not in ACTIONS:
        raise ContractError("invalid_input", "The action is not supported.")

    allowed = {
        "health": {"action"},
        "meal_comparison": {"action", "work_date", "relative_scope"},
        "audit": {"action", "period_end", "work_date"},
    }[action]
    if set(value) - allowed:
        raise ContractError("invalid_input", "The request contains unknown fields.")

    result: dict[str, Any] = {"action": action}
    if "work_date" in value:
        result["work_date"] = validate_date(value["work_date"])
    if "period_end" in value:
        result["period_end"] = validate_date(value["period_end"])
    if "relative_scope" in value:
        if type(value["relative_scope"]) is not str or value["relative_scope"] not in {"today", "yesterday"}:
            raise ContractError("invalid_input", "relative_scope must be today or yesterday.")
        result["relative_scope"] = value["relative_scope"]
    if "work_date" in result and "relative_scope" in result:
        raise ContractError("invalid_input", "work_date and relative_scope are mutually exclusive.")
    if action == "audit" and "period_end" in result and "work_date" in result:
        raise ContractError("invalid_input", "period_end and work_date are mutually exclusive.")
    return result


def _bounded_error(error: dict[str, Any] | None) -> dict[str, str] | None:
    if error is None:
        return None
    if type(error) is not dict or set(error) != {"code", "message"}:
        raise ContractError("invalid_output", "The error object is invalid.")
    if type(error["code"]) is not str or type(error["message"]) is not str:
        raise ContractError("invalid_output", "The error object is invalid.")
    code = error["code"].strip()
    message = error["message"].strip()
    if not code or len(code) > 64 or not re.fullmatch(r"[a-z][a-z0-9_]*", code):
        raise ContractError("invalid_output", "The error code is invalid.")
    if not message or len(message) > _MAX_ERROR_TEXT or any(ord(c) < 32 or ord(c) == 127 for c in message):
        raise ContractError("invalid_output", "The error message is invalid.")
    return {"code": code, "message": message}


def envelope(
    *,
    ok: bool,
    action: str,
    status: str,
    data: dict[str, Any],
    freshness: dict[str, Any] | None = None,
    delivery: dict[str, Any] | None = None,
    error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if type(ok) is not bool or type(action) is not str or not action:
        raise ContractError("invalid_output", "The response envelope is invalid.")
    if type(status) is not str or not status or len(status) > 64:
        raise ContractError("invalid_output", "The response status is invalid.")
    if type(data) is not dict:
        raise ContractError("invalid_output", "Response data must be an object.")
    result = {
        "ok": ok,
        "action": action,
        "status": status,
        "data": data,
        "freshness": freshness,
        "delivery": delivery,
        "error": _bounded_error(error),
    }
    if (ok and result["error"] is not None) or (not ok and result["error"] is None):
        raise ContractError("invalid_output", "Response success and error state disagree.")
    return result


def error_envelope(code: str, message: str, action: str = "invalid", *, status: str = "error") -> dict[str, Any]:
    return envelope(ok=False, action=action, status=status, data={}, error={"code": code, "message": message})


def bound_result(value: dict[str, Any], *, maximum: int = _MAX_RESULT_BYTES) -> dict[str, Any]:
    if type(value) is not dict:
        raise ContractError("result_too_large", "Response data must be an object.")
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError("invalid_output", "Response data is not JSON-safe.") from exc
    if len(encoded) > maximum:
        raise ContractError("result_too_large", "The bounded Paycom response limit was exceeded.")
    return value
