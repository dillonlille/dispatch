from __future__ import annotations

import json
import sys
from collections.abc import Mapping, Sequence
from typing import TextIO

_TECHNICAL_KEYS = {
    "contains_secrets",
    "delivery",
    "error",
    "freshness",
    "schema_version",
}
_ENVELOPE_KEYS = {
    "action",
    "ok",
    "status",
} | _TECHNICAL_KEYS

_COMMANDS = (
    ("health", "Show Dispatch health"),
    ("verify", "Verify the installed Core"),
    ("setup", "Configure Dispatch plugins"),
    ("paths", "Show Dispatch paths"),
    ("plugin", "Inspect installed plugins"),
    ("auth", "Inspect authentication state"),
    ("collection", "Inspect collection state"),
    ("browser-doctor", "Inspect the browser runtime"),
    ("service", "Run the foreground collection service"),
    ("uninstall", "Safely remove Dispatch"),
)


def _label(value: object) -> str:
    return str(value).replace("_", " ").replace("-", " ").strip().title()


def _value(value: object) -> str:
    if value is True:
        return "Yes"
    if value is False:
        return "No"
    if value is None:
        return "Not available"
    return str(value)


def format_help() -> str:
    lines = [
        "Dispatch",
        "",
        "Usage:",
        "  dispatch <command> [options]",
        "",
        "Commands:",
    ]
    lines.extend(f"  {command:<16} {description}" for command, description in _COMMANDS)
    lines.extend(("", "Automation:", "  Add --json for machine-readable output."))
    return "\n".join(lines)


def _headline(payload: Mapping[str, object]) -> str:
    action = str(payload.get("action") or "dispatch")
    status = str(payload.get("status") or ("ready" if payload.get("ok") else "error"))
    error = payload.get("error")
    if isinstance(error, Mapping):
        if error.get("code") == "confirmation_required":
            return "✗ Confirmation required"
        return f"✗ {_label(action)} failed"

    if action == "uninstall":
        return {
            "planned": "✓ Uninstall plan is ready",
            "blocked": "✗ Uninstall is blocked",
            "uninstalled": "✓ Dispatch was uninstalled",
            "already-uninstalled": "✓ Dispatch is already uninstalled",
            "purged": "✓ Dispatch and its data were removed",
            "purged-with-preserved-files": "✓ Dispatch was removed; unowned files were kept",
            "already-absent": "✓ Dispatch is already absent",
        }.get(status, f"✓ Uninstall: {_label(status)}")
    if action == "health":
        return {
            "ready": "✓ Dispatch is healthy",
            "setup_incomplete": "○ Dispatch is installed — setup incomplete",
            "degraded": "✗ Dispatch needs attention",
        }.get(status, f"{'✓' if payload.get('ok') else '✗'} Dispatch health: {_label(status)}")
    if action == "setup":
        return {
            "available": "Dispatch setup options",
            "complete": "✓ Dispatch setup is complete",
        }.get(status, f"✓ Setup: {_label(status)}")
    return f"{'✓' if payload.get('ok') else '✗'} {_label(action)}: {_label(status)}"


def _append_value(lines: list[str], key: object, value: object, indent: int) -> None:
    if value is None:
        return
    prefix = "  " * indent
    label = _label(key)
    if isinstance(value, Mapping):
        visible = [
            (child, item)
            for child, item in value.items()
            if child not in _TECHNICAL_KEYS and item is not None
        ]
        if not visible:
            return
        lines.append(f"{prefix}{label}")
        for child, item in visible:
            _append_value(lines, child, item, indent + 1)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if not value:
            lines.append(f"{prefix}{label}: None")
            return
        lines.append(f"{prefix}{label}")
        for item in value:
            if isinstance(item, Mapping):
                visible = [
                    (child, child_value)
                    for child, child_value in item.items()
                    if child not in _TECHNICAL_KEYS and child_value is not None
                ]
                if not visible:
                    continue
                first_key, first_value = visible[0]
                if not isinstance(first_value, (Mapping, list, tuple)):
                    lines.append(f"{prefix}  • {_label(first_key)}: {_value(first_value)}")
                    for child, child_value in visible[1:]:
                        _append_value(lines, child, child_value, indent + 2)
                else:
                    lines.append(f"{prefix}  •")
                    for child, child_value in visible:
                        _append_value(lines, child, child_value, indent + 2)
            else:
                lines.append(f"{prefix}  • {_value(item)}")
        return
    displayed = _label(value) if str(key) == "status" else _value(value)
    lines.append(f"{prefix}{label}: {displayed}")


def _uninstall_lines(payload: Mapping[str, object], data: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    error = payload.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        if message:
            lines.append(str(message).rstrip(".") + ".")
        if error.get("code") == "confirmation_required":
            lines.extend(
                (
                    "",
                    "Review what will change:",
                    "  dispatch uninstall --plan",
                    "",
                    "Uninstall and keep configuration and data:",
                    "  dispatch uninstall --yes",
                    "",
                    "Remove Dispatch, configuration, and data:",
                    "  dispatch uninstall --purge --yes",
                )
            )
        return lines

    mode = data.get("mode")
    if mode == "keep-data":
        lines.append("Mode: Keep configuration and data")
    elif mode == "purge":
        lines.append("Mode: Remove Dispatch, configuration, and data")

    if payload.get("status") in {"planned", "blocked"}:
        sections = (
            ("blockers", "Needs attention"),
            ("remove", "Will remove"),
            ("preserve", "Will keep"),
        )
        for key, title in sections:
            values = data.get(key)
            if (
                not isinstance(values, Sequence)
                or isinstance(values, (str, bytes, bytearray))
                or not values
            ):
                continue
            lines.extend(("", title))
            lines.extend(f"  • {_value(item)}" for item in values)
    elif mode == "keep-data":
        lines.extend(("", "Configuration and data were kept."))

    if data.get("system_dependencies") == "preserved-shared":
        lines.extend(("", "Shared system dependencies will not be removed."))
    if data.get("hermes") == "untouched":
        lines.append("Hermes will not be changed.")
    return lines


def _health_lines(data: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    if "installed" in data:
        lines.append(f"Core installed: {_value(data['installed'])}")
    if "operational" in data:
        lines.append(f"Core operational: {_value(data['operational'])}")

    setup = data.get("setup")
    if isinstance(setup, Mapping):
        lines.append(f"Setup complete: {_value(setup.get('complete'))}")
        plugins = setup.get("selected_plugins")
        if isinstance(plugins, Sequence) and not isinstance(plugins, (str, bytes, bytearray)):
            selected = ", ".join(str(plugin) for plugin in plugins) if plugins else "None selected"
            lines.append(f"Plugins: {selected}")

    collection = data.get("collection_manager")
    if isinstance(collection, Mapping) and collection.get("status") is not None:
        lines.append(f"Collection: {_label(collection['status'])}")

    authentication = data.get("authentication")
    if isinstance(authentication, Mapping) and "configured" in authentication:
        lines.append(f"Authentication configured: {_value(authentication['configured'])}")

    browser = data.get("browser_manager")
    if isinstance(browser, Mapping) and "configured" in browser:
        lines.append(f"Browser configured: {_value(browser['configured'])}")
        if browser.get("configured") and browser.get("error_message"):
            lines.append(f"Browser issue: {browser['error_message']}")
    return lines


def format_human(payload: Mapping[str, object]) -> str:
    lines = [_headline(payload)]
    error = payload.get("error")
    data = payload.get("data")
    details: Mapping[str, object]
    if isinstance(data, Mapping):
        details = data
    else:
        details = {
            key: value
            for key, value in payload.items()
            if key not in _ENVELOPE_KEYS and key != "data"
        }

    if payload.get("action") == "uninstall":
        rendered = _uninstall_lines(payload, details)
        if rendered:
            lines.extend(("", *rendered))
        return "\n".join(lines)

    if payload.get("action") == "health" and isinstance(data, Mapping):
        rendered = _health_lines(data)
        if rendered:
            lines.extend(("", *rendered))
        return "\n".join(lines)

    if isinstance(error, Mapping):
        message = error.get("message")
        if message:
            lines.extend(("", str(message).rstrip(".") + "."))
        code = error.get("code")
        if code:
            lines.append(f"Code: {code}")
        return "\n".join(lines)

    hidden = _TECHNICAL_KEYS if isinstance(data, Mapping) else _ENVELOPE_KEYS
    visible = [
        (key, value)
        for key, value in details.items()
        if key not in hidden and value is not None
    ]
    if visible:
        lines.append("")
        for key, value in visible:
            _append_value(lines, key, value, 0)
    return "\n".join(lines)


def emit(payload: Mapping[str, object], *, json_output: bool = False, stream: TextIO | None = None) -> None:
    destination = stream or sys.stdout
    if json_output:
        print(json.dumps(dict(payload), sort_keys=True), file=destination)
    else:
        print(format_human(payload), file=destination)
