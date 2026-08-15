from __future__ import annotations

import json

from dispatch_installer.output import emit, format_human


def test_uninstall_confirmation_is_simple_and_actionable() -> None:
    output = format_human(
        {
            "ok": False,
            "action": "uninstall",
            "status": "error",
            "data": {},
            "error": {
                "code": "confirmation_required",
                "message": "uninstall requires --yes or --plan",
            },
        }
    )

    assert output.startswith("✗ Confirmation required")
    assert "dispatch uninstall --plan" in output
    assert "dispatch uninstall --yes" in output
    assert "dispatch uninstall --purge --yes" in output
    assert "{" not in output


def test_uninstall_plan_has_readable_sections() -> None:
    output = format_human(
        {
            "ok": True,
            "action": "uninstall",
            "status": "planned",
            "data": {
                "schema_version": 1,
                "mode": "keep-data",
                "status": "planned",
                "remove": ["/users/example/.dispatch/bin"],
                "preserve": ["/users/example/.dispatch/data"],
                "blockers": [],
                "system_dependencies": "preserved-shared",
                "hermes": "untouched",
            },
            "error": None,
        }
    )

    assert output.startswith("✓ Uninstall plan is ready")
    assert "Mode: Keep configuration and data" in output
    assert "Will remove\n  • /users/example/.dispatch/bin" in output
    assert "Will keep\n  • /users/example/.dispatch/data" in output
    assert "Hermes will not be changed." in output
    assert "{" not in output


def test_completed_uninstall_does_not_show_a_future_removal_plan() -> None:
    output = format_human(
        {
            "ok": True,
            "action": "uninstall",
            "status": "uninstalled",
            "data": {
                "mode": "keep-data",
                "remove": ["/internal/state/<operational-state>"],
                "preserve": ["/users/example/.dispatch/data"],
                "blockers": [],
                "system_dependencies": "preserved-shared",
                "hermes": "untouched",
            },
            "error": None,
        }
    )

    assert output.startswith("✓ Dispatch was uninstalled")
    assert "Configuration and data were kept." in output
    assert "Will remove" not in output


def test_json_output_remains_available_for_automation(capsys) -> None:
    payload = {
        "ok": True,
        "action": "health",
        "status": "ready",
        "data": {"operational": True},
        "error": None,
    }

    emit(payload, json_output=True)

    assert json.loads(capsys.readouterr().out) == payload


def test_nested_health_statuses_remain_visible() -> None:
    output = format_human(
        {
            "ok": True,
            "action": "health",
            "status": "setup_incomplete",
            "data": {
                "installed": True,
                "operational": True,
                "checks": {
                    "core": {"status": "ready"},
                    "setup": {"status": "incomplete"},
                },
                "setup": {"complete": False, "selected_plugins": []},
                "collection_manager": {"status": "no_collectors"},
                "browser_manager": {"configured": False, "realms": [{"id": "hidden"}]},
            },
            "error": None,
        }
    )

    assert output.startswith("○ Dispatch is installed — setup incomplete")
    assert "Core operational: Yes" in output
    assert "Setup complete: No" in output
    assert "Collection: No Collectors" in output
    assert "realms" not in output.casefold()


def test_verify_is_concise_and_omits_internal_browser_detail() -> None:
    output = format_human(
        {
            "ok": True,
            "action": "verify",
            "status": "ready",
            "data": {
                "ready": True,
                "package": "dispatch-core",
                "version": "1.0.0",
                "setup": {"complete": True, "selected_plugins": []},
                "collection_manager": {"status": "no_collectors", "ready": True},
                "authentication": {"configured": False, "dependency": "not_installed"},
                "browser_manager": {
                    "configured": False,
                    "realms": [{"id": "internal-browser-realm"}],
                    "required_chromium_revision": "1234",
                },
            },
        }
    )

    assert "Core: dispatch-core 1.0.0" in output
    assert "Installation: Verified" in output
    assert "Collection: No Collectors" in output
    assert "Authentication: Not configured" in output
    assert "Browser: Not configured" in output
    assert "internal-browser-realm" not in output
    assert "1234" not in output


def test_paths_and_collection_status_are_concise() -> None:
    paths = format_human(
        {
            "ok": True,
            "action": "paths",
            "status": "ready",
            "data": {
                "paths": {
                    "DISPATCH_CODE_ROOT": "/users/example/.dispatch/releases/core",
                    "DISPATCH_DATA_ROOT": "/users/example/.dispatch/data",
                }
            },
        }
    )
    collection = format_human(
        {
            "ok": True,
            "action": "collection-status",
            "status": "ready",
            "data": {
                "tasks": {"queued": 2, "running": 1, "failed": 0},
                "workers": 1,
                "schedules": 0,
                "overdue_workers": 0,
            },
        }
    )

    assert paths == (
        "✓ Dispatch paths are ready\n\n"
        "Code: /users/example/.dispatch/releases/core\n"
        "Data: /users/example/.dispatch/data"
    )
    assert "Tasks: 3" in collection
    assert "Queued: 2" in collection
    assert "Running: 1" in collection
    assert "Failed" not in collection


def test_health_error_keeps_its_actionable_message() -> None:
    output = format_human(
        {
            "ok": False,
            "action": "health",
            "status": "degraded",
            "data": {"installed": True, "operational": False},
            "error": {"code": "collection_invalid", "message": "collection database is invalid"},
        }
    )

    assert output.startswith("✗ Dispatch needs attention")
    assert "Core operational: No" in output
    assert "collection database is invalid." in output


def test_verify_does_not_claim_success_when_not_ready() -> None:
    output = format_human(
        {
            "ok": True,
            "action": "verify",
            "status": "setup_incomplete",
            "data": {
                "ready": False,
                "package": "dispatch-core",
                "version": "1.0.0",
                "setup": {"complete": False, "selected_plugins": []},
            },
            "error": None,
        }
    )

    assert output.startswith("○ Dispatch verification needs attention")
    assert "Installation: Needs attention" in output
    assert "verification passed" not in output


def test_collection_status_does_not_claim_readiness_during_reconciliation() -> None:
    output = format_human(
        {
            "ok": True,
            "action": "collection-status",
            "status": "reconciliation_required",
            "data": {
                "ready": False,
                "status": "reconciliation_required",
                "tasks": {"queued": 2},
                "workers": 0,
                "overdue_workers": 1,
            },
            "error": None,
        }
    )

    assert output.startswith("○ Collection queue needs attention")
    assert "State: Reconciliation Required" in output
    assert "Ready: No" in output