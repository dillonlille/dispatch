from __future__ import annotations

from typing import Any

from .config import load_settings
from .contracts import envelope, failure, request_action
from .driver_names import DriverNameResolver
from .managed_session import validate_companion_config
from .store import validate_conversation_database


def health() -> dict[str, Any]:
    """Read-only health; it never launches a browser or contacts Slack/Amazon."""
    settings = load_settings(require_tokens=False)
    integrity_errors = list(settings.config_errors)
    try:
        validate_companion_config(settings.config.amazon)
        validate_conversation_database(settings.database_path)
        if settings.config.driver_names.enabled:
            DriverNameResolver.from_sqlite(
                str(settings.database_path.with_name("driver_names.sqlite3")),
                id_regex=settings.config.driver_names.id_regex,
                fallback_to_id=settings.config.driver_names.fallback_to_id,
            )
    except (OSError, RuntimeError, ValueError) as exc:
        integrity_errors.append(type(exc).__name__)
    delivery = "configured" if settings.secrets.slack_bot_token_present and settings.secrets.slack_app_token_present and not settings.security_errors and not settings.config_errors else "not_configured"
    authentication = "unavailable"
    try:
        from authentication import AuthenticationManager
        from paths import DispatchPaths

        manager = AuthenticationManager(DispatchPaths.from_environment())
        if hasattr(manager, "profile_for_plugin"):
            try:
                profile = manager.profile_for_plugin("companion-bridge", "amazon-operations")
            except Exception:
                profile = ""
        else:
            profile = getattr(settings.config.amazon, "auth_account_alias", "default")
        if hasattr(manager, "profile_status"):
            authentication = "not_configured"
            if profile:
                status = manager.profile_status(profile)
                authentication = "configured" if status.get("status") == "enrolled" else "not_configured"
        else:
            status = manager.status("amazon-operations", profile)
            authentication = "configured" if status.get("configured") is True else "not_configured"
    except Exception:
        authentication = "unavailable"
    overall = "configured" if delivery == "configured" and authentication == "configured" and not integrity_errors else "degraded"
    planes = {
        "registration": "ready",
        "runtime_integrity": "ready" if not integrity_errors else "degraded",
        "query": "not_applicable",
        "data": "ready" if not integrity_errors else "degraded",
        "freshness": "not_applicable",
        "collector": "not_checked",
        "authentication": authentication,
        "delivery": delivery,
        "overall": overall,
    }
    error = None
    if integrity_errors:
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
