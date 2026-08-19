from __future__ import annotations

from typing import Any

from .config import load_settings
from .contracts import HEALTH_PLANES, envelope, failure, request_action


def health() -> dict[str, Any]:
    """Read-only health; it never launches a browser or contacts Slack/Amazon."""
    settings = load_settings(require_tokens=False)
    delivery = "configured" if settings.secrets.slack_bot_token_present and settings.secrets.slack_app_token_present and not settings.security_errors and not settings.config_errors else "not_configured"
    authentication = "unavailable"
    try:
        from authentication import AuthenticationManager
        from paths import DispatchPaths

        status = AuthenticationManager(DispatchPaths.from_environment()).status(
            "amazon-operations",
            settings.config.amazon.auth_account_alias,
        )
        authentication = "configured" if status.get("configured") is True else "not_configured"
    except Exception:
        authentication = "unavailable"
    overall = "configured" if delivery == "configured" and authentication == "configured" else "degraded"
    planes = {
        "registration": "ready",
        "runtime_integrity": "ready",
        "query": "not_applicable",
        "data": "ready" if settings.config_errors == [] else "degraded",
        "freshness": "not_applicable",
        "collector": "not_checked",
        "authentication": authentication,
        "delivery": delivery,
        "overall": overall,
    }
    error = None
    if settings.config_errors:
        error = {"code": "configuration_invalid", "message": "Companion Bridge configuration is invalid."}
    elif settings.security_errors:
        error = {"code": "allowlists_required", "message": "Slack allowlists must be configured before service start."}
    elif not settings.secrets.slack_bot_token_present or not settings.secrets.slack_app_token_present:
        error = {"code": "service_not_configured", "message": "Slack service credentials are not configured."}
    elif authentication != "configured":
        error = {
            "code": "authentication_not_configured",
            "message": "Amazon authentication credentials are not configured for the selected account.",
        }
    return envelope(
        ok=overall == "configured",
        action="health",
        status=overall,
        data=planes,
        error=error,
    )


def handle(request: Any) -> dict[str, Any]:
    action, payload = request_action(request)
    if action != "health" or payload is None or set(payload) != {"action"}:
        return failure(action or "invalid", "invalid_input", "Only the health action is supported.")
    return health()


def available() -> bool:
    result = health()
    return result["ok"] is True and result["status"] == "configured"


__all__ = ["available", "handle", "health"]
