"""The exact seven-field service boundary; no report-delivery action is exposed."""
from __future__ import annotations

from typing import Any, Mapping

from .contracts import ContractError, envelope, error_envelope, validate_request
from .health import health
from .query import PaycomQuery, QueryError
from .storage import StorageError


def _error_action(request: Any) -> str:
    value = request.get("action") if type(request) is dict else None
    return value if isinstance(value, str) and value else "invalid"


def handle(request: Any, *, paths: Any = None, clock: Any | None = None) -> dict[str, Any]:
    action = _error_action(request)
    try:
        values = validate_request(request)
        action = values["action"]
        if action == "health":
            result = health(paths)
            error = result.get("error")
            if error:
                return envelope(ok=False, action=action, status=result["status"], data=result["data"], error=error)
            return envelope(ok=True, action=action, status=result["status"], data=result["data"])
        with PaycomQuery(paths, clock) as query:
            raw = query.execute(values)
        data = {key: value for key, value in raw.items() if key not in {"ok", "status"}}
        freshness = data.pop("freshness", None)
        return envelope(ok=True, action=action, status=raw["status"], data=data, freshness=freshness)
    except (ContractError, QueryError, StorageError) as exc:
        code = getattr(exc, "code", "internal_error")
        message = getattr(exc, "message", str(exc))
        status = "error" if code == "invalid_input" else "unavailable"
        return error_envelope(code, message[:256], action, status=status)
    except Exception:
        return error_envelope("internal_error", "The Paycom service could not complete the operation.", action, status="unavailable")



def execute(request: Mapping[str, Any], *, paths: Any = None, clock: Any | None = None) -> dict[str, Any]:
    return handle(request, paths=paths, clock=clock)
