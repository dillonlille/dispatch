from __future__ import annotations

import json
from pathlib import Path

import command_interface as command_interface
from collection_manager import CollectionDisposition, CollectionReceipt, CollectorRegistration
from command_interface import main

ENVELOPE = {"ok", "action", "status", "data", "freshness", "delivery", "error"}


def test_parser_can_use_the_public_dispatch_program_name() -> None:
    assert command_interface.parser(prog="dispatch").prog == "dispatch"
    assert command_interface.parser().prog == "dispatch-core"


def test_health_command_serializes_the_standard_envelope(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))

    assert main(["health"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == ENVELOPE
    assert payload["action"] == "health"
    assert payload["status"] == "setup_incomplete"


def test_plugin_commands_use_the_generic_runtime_contract(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        command_interface,
        "list_plugins",
        lambda: [{"id": "example", "distribution": "dispatch-example", "version": "1.2.3"}],
    )
    monkeypatch.setattr(
        command_interface,
        "invoke_plugin",
        lambda plugin_id, request: {
            "ok": True,
            "action": request["action"],
            "status": "ready",
            "data": {"plugin": plugin_id, "request": request},
            "freshness": None,
            "delivery": None,
            "error": None,
        },
    )

    assert main(["plugin", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["data"]["plugins"][0]["id"] == "example"

    assert main(["plugin", "invoke", "example", "--request", '{"action":"lookup","question":"hello"}']) == 0
    invoked = json.loads(capsys.readouterr().out)
    assert invoked["data"]["response"]["data"]["request"]["question"] == "hello"


def test_plugin_service_command_runs_in_foreground_and_restores_stop_boundary(monkeypatch, tmp_path, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))
    observed = {}

    def service(plugin_id, *, paths, stop_requested):
        observed.update(plugin_id=plugin_id, paths=paths)
        observed["stop_requested"] = stop_requested()

    monkeypatch.setattr(command_interface, "serve_plugin", service)

    assert main(["plugin", "serve", "companion"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["action"] == "plugin-serve"
    assert payload["status"] == "stopped"
    assert payload["data"]["plugin"] == "companion"
    assert observed["plugin_id"] == "companion"
    assert observed["stop_requested"] is False


def test_plugin_configurator_command_is_interactive_entry_point(monkeypatch, tmp_path, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))
    observed = {}

    def configurator(plugin_id, *, paths):
        observed.update(plugin_id=plugin_id, paths=paths)
        return {
            "ok": True,
            "action": "configure",
            "status": "configured",
            "data": {},
            "freshness": None,
            "delivery": None,
            "error": None,
        }

    monkeypatch.setattr(command_interface, "configure_plugin", configurator)

    assert main(["plugin", "configure", "companion"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["action"] == "plugin-configure"
    assert payload["status"] == "configured"
    assert payload["data"]["plugin"] == "companion"
    assert observed["plugin_id"] == "companion"


def test_browser_doctor_is_bounded_and_does_not_launch_a_browser(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))

    assert main(["browser-doctor"]) in {0, 1}
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == ENVELOPE
    assert payload["action"] == "browser-doctor"
    assert payload["data"]["operational"] is False
    browser = payload["data"]["browser_manager"]
    assert browser["browser_family"] == "chromium"
    assert browser["ready"] is False
    assert "selector" not in browser
    assert "receipt" not in browser
    assert "profiles" not in payload["data"]["browser_manager"]


def test_browser_commands_expose_bounded_runtime_and_reserved_providers(monkeypatch, tmp_path, capsys) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))

    assert main(["browser", "providers"]) == 0
    providers = json.loads(capsys.readouterr().out)
    assert set(providers) == ENVELOPE
    assert providers["action"] == "browser-providers"
    assert [item["id"] for item in providers["data"]["providers"]] == [
        "managed-playwright",
        "persistent-cdp",
        "external-cdp",
    ]
    assert providers["data"]["providers"][0]["implemented"] is True
    assert providers["data"]["providers"][1]["implemented"] is False
    assert providers["data"]["contains_secrets"] is False

    assert main(["browser", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["action"] == "browser-status"
    assert status["status"] == "not_ready"
    assert status["data"]["runtime"]["ready"] is False
    assert status["data"]["contains_secrets"] is False
    assert not home.exists()


def test_invalid_arguments_return_one_closed_json_error(capsys) -> None:
    for values in (["browser"], ["browser", "invalid"], ["invalid"]):
        assert main(values) == 1
        captured = capsys.readouterr()
        payload = json.loads(captured.out)
        assert captured.err == ""
        assert set(payload) == ENVELOPE
        assert payload["ok"] is False
        assert payload["error"]["code"] == "arguments_invalid"


def test_browser_reconcile_does_not_require_playwright(monkeypatch, tmp_path, capsys) -> None:
    import browser_manager.manager as manager_module

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(
        manager_module,
        "PlaywrightRuntime",
        lambda: (_ for _ in ()).throw(ImportError("playwright unavailable")),
    )
    assert main(["browser", "reconcile"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["action"] == "browser-reconcile"
    assert payload["data"]["outcomes"] == []


def test_browser_status_invalid_root_returns_closed_json_error(monkeypatch, capsys) -> None:
    monkeypatch.setenv("DISPATCH_HOME", "relative-dispatch")
    assert main(["browser", "status"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == ENVELOPE
    assert payload["ok"] is False
    assert payload["action"] == "browser"
    assert payload["error"]["code"] == "browser_cache_path_invalid"


def test_auth_status_is_ready_without_creating_private_state(monkeypatch, tmp_path, capsys) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))

    assert main(["auth", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "auth-status"
    assert payload["data"]["configured"] is False
    assert not home.exists()


def test_auth_enroll_uses_hidden_prompts_and_never_outputs_values(monkeypatch, tmp_path, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))
    answers = iter(("synthetic-user", "synthetic-password-not-a-secret"))
    monkeypatch.setattr(
        "command_interface.getpass.getpass",
        lambda prompt: next(answers),
    )

    assert main(["auth", "enroll", "amazon-operations"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["data"]["status"] == "configured"
    assert "synthetic-user" not in output
    assert "synthetic-password-not-a-secret" not in output


def test_collection_submit_queues_a_discovered_collector(monkeypatch, tmp_path, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))
    registration = CollectorRegistration(
        "example-collector",
        "example",
        "1.0.0",
        lambda context: CollectionReceipt(CollectionDisposition.NO_DATA, None, 0, True),
    )
    monkeypatch.setattr(command_interface, "discover_collector_registrations", lambda: (registration,))

    assert main(
        [
            "collection",
            "submit",
            "example-collector",
            "--parameters",
            '{"date":"2026-08-19"}',
            "--idempotency-key",
            "example-submit-1",
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["action"] == "collection-submit"
    assert payload["data"]["collector_id"] == "example-collector"
    assert payload["data"]["state"] == "queued"


def test_collection_status_is_read_only_with_no_queue(monkeypatch, tmp_path, capsys) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))

    assert main(["collection", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["action"] == "collection-status"
    assert payload["data"]["status"] == "empty"
    assert payload["data"]["workers"] == 0
    assert not home.exists()


def test_collection_worker_once_is_bounded_and_idle_without_collectors(monkeypatch, tmp_path, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))

    assert main(["collection", "worker-once"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["action"] == "collection-worker-once"
    assert payload["data"]["status"] == "idle"
    assert payload["data"]["process_cleaned"] is True


def test_service_runs_setup_independent_idle_tick(monkeypatch, tmp_path, capsys) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("DISPATCH_CODE_ROOT", str(Path(__file__).resolve().parents[3]))

    assert main(["service", "--idle-seconds", "0.05", "--max-ticks", "1"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["action"] == "service"
    assert payload["status"] == "stopped"
    assert payload["data"]["ticks"] == 1
    assert payload["data"]["last_tick"]["worker"]["status"] == "idle"
