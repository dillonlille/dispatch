from __future__ import annotations

from typing import Any, Mapping

ENVELOPE_KEYS = frozenset({"ok", "action", "status", "data", "freshness", "delivery", "error"})
HEALTH_PLANES = (
    "registration",
    "runtime_integrity",
    "query",
    "data",
    "freshness",
    "collector",
    "authentication",
    "delivery",
    "overall",
)


def envelope(
    *,
    ok: bool,
    action: str,
    status: str,
    data: Mapping[str, Any] | None = None,
    freshness: Mapping[str, Any] | None = None,
    delivery: Mapping[str, Any] | None = None,
    error: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": bool(ok),
        "action": action,
        "status": status,
        "data": dict(data or {}),
        "freshness": dict(freshness) if freshness is not None else None,
        "delivery": dict(delivery) if delivery is not None else None,
        "error": dict(error) if error is not None else None,
    }


def failure(action: str, code: str, message: str, *, status: str = "error") -> dict[str, Any]:
    return envelope(
        ok=False,
        action=action,
        status=status,
        error={"code": code[:64], "message": message[:512]},
    )


def request_action(request: Any) -> tuple[str | None, dict[str, Any] | None]:
    if type(request) is not dict:
        return None, None
    action = request.get("action")
    if not isinstance(action, str) or not action:
        return None, request
    return action, request
