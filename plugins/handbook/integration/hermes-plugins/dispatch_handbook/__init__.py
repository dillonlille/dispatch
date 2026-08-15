from __future__ import annotations

import json
from typing import Any

ACTIONS = {"lookup", "overview", "contents", "health"}
SCHEMA = {
    "name": "dispatch_handbook",
    "description": "Read a configured local handbook index. This tool never collects, authenticates, or sends messages.",
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": sorted(ACTIONS)},
            "question": {"type": "string", "minLength": 3, "maxLength": 500},
        },
        "required": ["action"],
        "allOf": [
            {
                "if": {"properties": {"action": {"const": "lookup"}}, "required": ["action"]},
                "then": {"required": ["question"]},
            },
            {
                "if": {
                    "properties": {"action": {"enum": ["overview", "contents", "health"]}},
                    "required": ["action"],
                },
                "then": {"not": {"required": ["question"]}},
            },
        ],
    },
}
PLANES = (
    "registration",
    "runtime_integrity",
    "configuration",
    "query",
    "data",
    "freshness",
    "collector",
    "authentication",
    "service",
    "delivery",
    "overall",
)


def _envelope(ok: bool, action: str | None, status: str, data: dict, error: dict | None = None) -> dict:
    return {
        "ok": ok,
        "action": action,
        "status": status,
        "data": data,
        "freshness": None,
        "delivery": None,
        "error": error,
    }


def _service():
    try:
        from dispatch_handbook import service
    except ImportError:
        return None
    return service


def _fallback_health() -> dict:
    planes = {name: "not_applicable" for name in PLANES}
    planes.update(
        registration="ready",
        runtime_integrity="unavailable",
        query="unavailable",
        data="unavailable",
        freshness="unavailable",
        overall="degraded",
    )
    return _envelope(True, "health", "degraded", planes)


def _handle_dispatch_handbook(args: Any) -> str:
    if type(args) is not dict:
        return json.dumps(_envelope(False, "invalid", "error", {}, {"code": "invalid_input", "message": "The request must be a JSON object."}), sort_keys=True)
    action = args.get("action")
    if type(action) is not str or action not in ACTIONS:
        return json.dumps(_envelope(False, action if isinstance(action, str) and action else "invalid", "error", {}, {"code": "invalid_input", "message": "The action is not supported."}), sort_keys=True)
    expected = {"action", "question"} if action == "lookup" else {"action"}
    if set(args) != expected:
        return json.dumps(_envelope(False, action, "error", {}, {"code": "invalid_input", "message": "The request contains missing or unknown fields."}), sort_keys=True)
    service = _service()
    if service is None:
        result = _fallback_health() if action == "health" else _envelope(
            False,
            action,
            "unavailable",
            {},
            {"code": "runtime_unavailable", "message": "The local handbook runtime is unavailable."},
        )
    else:
        result = service.handle(args)
    return json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _check_available() -> bool:
    service = _service()
    if service is None:
        return False
    try:
        result = service.health()
    except Exception:
        return False
    return result.get("ok") is True and result.get("status") == "ready"


def register(ctx) -> None:
    ctx.register_tool(
        name="dispatch_handbook",
        toolset="dispatch_handbook",
        description=SCHEMA["description"],
        schema=SCHEMA,
        handler=_handle_dispatch_handbook,
        check_fn=_check_available,
    )
