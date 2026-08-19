from __future__ import annotations

import importlib.metadata
from pathlib import Path
from typing import Any

import pytest

import plugin_runtime
from paths import DispatchPaths
from plugin_runtime import (
    CONFIGURATOR_ENTRY_POINT_GROUP,
    SERVICE_ENTRY_POINT_GROUP,
    PluginRuntimeError,
    discover_configurator,
    discover_plugins,
    discover_service,
    invoke_configurator,
    invoke_plugin,
    invoke_service,
    plugin_health,
)


class _Distribution:
    def __init__(self, name: str = "dispatch-example", version: str = "1.2.3") -> None:
        self.metadata = {"Name": name}
        self.version = version


class _EntryPoint:
    def __init__(self, name: str, handler, distribution: _Distribution | None = None) -> None:
        self.name = name
        self.group = "dispatch.plugins"
        self.dist = distribution or _Distribution()
        self._handler = handler

    def load(self):
        return self._handler


class _GroupedEntryPoint(_EntryPoint):
    def __init__(self, name: str, group: str, handler, distribution: _Distribution | None = None) -> None:
        super().__init__(name, handler, distribution)
        self.group = group


def _envelope(action: str = "health", *, ok: bool = True) -> dict[str, Any]:
    return {
        "ok": ok,
        "action": action,
        "status": "ready" if ok else "error",
        "data": {},
        "freshness": None,
        "delivery": None,
        "error": None if ok else {"code": "invalid_input", "message": "invalid"},
    }


def _install_metadata(monkeypatch, *entry_points: _EntryPoint) -> None:
    monkeypatch.setattr(importlib.metadata, "entry_points", lambda **kwargs: list(entry_points))


def test_entry_points_are_filtered_by_active_ids(monkeypatch) -> None:
    def handle(request):
        return {**_envelope(request["action"]), "data": {"request": request}}

    _install_metadata(
        monkeypatch,
        _EntryPoint("example", handle),
        _EntryPoint("unselected", handle, _Distribution("dispatch-unselected")),
    )
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "example")
    monkeypatch.setenv("DISPATCH_PLUGIN_PATHS", "/obsolete/and/ignored")

    plugins = discover_plugins()
    response = invoke_plugin("example", {"action": "lookup", "question": "bounded example"})

    assert [plugin.safe_data() for plugin in plugins] == [
        {"id": "example", "distribution": "dispatch-example", "version": "1.2.3"}
    ]
    assert response["data"]["request"]["question"] == "bounded example"
    assert plugin_health(["example"])["ready"] is True


def test_missing_active_entry_point_fails_closed(monkeypatch) -> None:
    _install_metadata(monkeypatch)
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "example")

    with pytest.raises(PluginRuntimeError) as error:
        discover_plugins()

    assert error.value.code == "plugin_entry_point_invalid"


def test_duplicate_active_entry_points_are_rejected(monkeypatch) -> None:
    handler = lambda request: _envelope(request["action"])
    _install_metadata(monkeypatch, _EntryPoint("example", handler), _EntryPoint("example", handler))
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "example")

    with pytest.raises(PluginRuntimeError) as error:
        discover_plugins()

    assert error.value.code == "plugin_entry_point_invalid"


def test_paths_are_not_required_when_no_plugins_are_selected(monkeypatch) -> None:
    monkeypatch.delenv("DISPATCH_ACTIVE_PLUGINS", raising=False)
    monkeypatch.setenv("DISPATCH_PLUGIN_PATHS", "/obsolete/and/ignored")

    assert discover_plugins() == []


def test_invalid_response_envelope_is_rejected(monkeypatch) -> None:
    _install_metadata(monkeypatch, _EntryPoint("example", lambda request: {"ok": True}))
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "example")

    with pytest.raises(PluginRuntimeError) as error:
        invoke_plugin("example", {"action": "health"})

    assert error.value.code == "plugin_response_invalid"


def test_service_entry_point_is_active_and_receives_core_context(monkeypatch, tmp_path) -> None:
    home = tmp_path / "home"
    paths = DispatchPaths.from_environment({"HOME": str(home)}, code_root=Path(__file__).resolve().parents[2])
    observed = {}

    def service(context):
        observed["paths"] = context.paths
        observed["plugin_id"] = context.plugin_id
        observed["stop"] = context.stop_requested()

    _install_metadata(
        monkeypatch,
        _GroupedEntryPoint("companion", SERVICE_ENTRY_POINT_GROUP, service),
        _GroupedEntryPoint("companion", CONFIGURATOR_ENTRY_POINT_GROUP, lambda context: None),
    )
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "companion")

    invoke_service("companion", paths=paths, stop_requested=lambda: True)

    assert observed == {"paths": paths, "plugin_id": "companion", "stop": True}
    assert discover_service("companion").safe_data()["id"] == "companion"
    assert discover_configurator("companion").safe_data()["id"] == "companion"


def test_service_and_configurator_require_exactly_one_matching_active_entry_point(monkeypatch, tmp_path) -> None:
    paths = DispatchPaths.from_environment({"HOME": str(tmp_path / "home")}, code_root=Path(__file__).resolve().parents[2])
    _install_metadata(
        monkeypatch,
        _GroupedEntryPoint("companion", SERVICE_ENTRY_POINT_GROUP, lambda context: None),
        _GroupedEntryPoint("companion", SERVICE_ENTRY_POINT_GROUP, lambda context: None),
    )
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "companion")

    with pytest.raises(PluginRuntimeError) as service_error:
        invoke_service("companion", paths=paths)
    assert service_error.value.code == "plugin_entry_point_invalid"

    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "other")
    with pytest.raises(PluginRuntimeError) as inactive_error:
        invoke_configurator("companion", paths=paths)
    assert inactive_error.value.code == "plugin_not_active"


def test_configurator_context_bounds_interactive_io(monkeypatch, tmp_path) -> None:
    paths = DispatchPaths.from_environment(
        {"HOME": str(tmp_path / "home")},
        code_root=Path(__file__).resolve().parents[2],
    )
    observed: list[str] = []
    _install_metadata(
        monkeypatch,
        _GroupedEntryPoint(
            "companion",
            CONFIGURATOR_ENTRY_POINT_GROUP,
            lambda context: {
                **_envelope("configure"),
                "data": {
                    "plain": context.prompt("Channel: "),
                    "secret_present": bool(context.prompt_secret("Token: ")),
                    "output": context.output("configured"),
                },
            },
        ),
    )
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "companion")

    result = invoke_configurator(
        "companion",
        paths=paths,
        prompt_fn=lambda message: "plain-value",
        secret_prompt_fn=lambda message: "secret-value",
        output_fn=observed.append,
    )

    assert result["ok"] is True
    assert result["action"] == "configure"
    assert result["data"] == {"plain": "plain-value", "secret_present": True, "output": None}
    assert observed == ["configured"]

    with pytest.raises(PluginRuntimeError) as error:
        invoke_configurator(
            "companion",
            paths=paths,
            prompt_fn=lambda message: "x" * 4097,
        )
    assert error.value.code == "plugin_configuration_failed"


def test_configurator_rejects_prompted_secret_in_result(monkeypatch, tmp_path) -> None:
    paths = DispatchPaths.from_environment(
        {"HOME": str(tmp_path / "home")},
        code_root=Path(__file__).resolve().parents[2],
    )
    _install_metadata(
        monkeypatch,
        _GroupedEntryPoint(
            "companion",
            CONFIGURATOR_ENTRY_POINT_GROUP,
            lambda context: {
                **_envelope("configure"),
                "data": {"leak": context.prompt_secret("Token: ")},
            },
        ),
    )
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "companion")

    with pytest.raises(PluginRuntimeError) as error:
        invoke_configurator(
            "companion",
            paths=paths,
            secret_prompt_fn=lambda message: "secret-value",
        )
    assert error.value.code == "plugin_configuration_secret_exposure"


def test_default_configurator_output_uses_stderr(monkeypatch, tmp_path, capsys) -> None:
    paths = DispatchPaths.from_environment(
        {"HOME": str(tmp_path / "home")},
        code_root=Path(__file__).resolve().parents[2],
    )

    def configure(context):
        context.output("progress")
        return _envelope("configure")

    _install_metadata(
        monkeypatch,
        _GroupedEntryPoint("companion", CONFIGURATOR_ENTRY_POINT_GROUP, configure),
    )
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "companion")

    invoke_configurator("companion", paths=paths)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "progress\n"


def test_plugin_runtime_errors_from_service_are_sanitized(monkeypatch, tmp_path) -> None:
    paths = DispatchPaths.from_environment(
        {"HOME": str(tmp_path / "home")},
        code_root=Path(__file__).resolve().parents[2],
    )

    def service(context):
        raise PluginRuntimeError("BAD CODE!", "secret-value")

    _install_metadata(
        monkeypatch,
        _GroupedEntryPoint("companion", SERVICE_ENTRY_POINT_GROUP, service),
    )
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "companion")

    with pytest.raises(PluginRuntimeError) as error:
        invoke_service("companion", paths=paths)
    assert error.value.code == "plugin_service_failed"
    assert str(error.value) == "plugin service failed"


def test_context_entry_point_must_accept_context(monkeypatch) -> None:
    _install_metadata(
        monkeypatch,
        _GroupedEntryPoint("companion", SERVICE_ENTRY_POINT_GROUP, lambda: None),
    )
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "companion")

    with pytest.raises(PluginRuntimeError) as error:
        discover_service("companion")
    assert error.value.code == "plugin_entry_point_invalid"


def test_plugin_invocation_discards_direct_stdout(monkeypatch, capsys) -> None:
    def handler(request):
        print("DIRECT_STDOUT_CONTAMINATION")
        return _envelope(request["action"])

    _install_metadata(monkeypatch, _EntryPoint("example", handler))
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "example")

    result = invoke_plugin("example", {"action": "summary"})
    assert result["ok"] is True
    assert capsys.readouterr().out == ""


def test_plugin_base_exceptions_are_bounded(monkeypatch) -> None:
    def handler(request):
        raise KeyboardInterrupt

    _install_metadata(monkeypatch, _EntryPoint("example", handler))
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "example")

    with pytest.raises(PluginRuntimeError) as error:
        invoke_plugin("example", {"action": "summary"})
    assert error.value.code == "plugin_invocation_failed"


def test_service_context_factories_keep_managers_in_process(monkeypatch, tmp_path) -> None:
    paths = DispatchPaths.from_environment({"HOME": str(tmp_path / "home")}, code_root=Path(__file__).resolve().parents[2])
    managers = []

    class Browser:
        def __init__(self, received):
            self.paths = received

        def shutdown(self):
            managers.append("browser-shutdown")

    class Authentication:
        def __init__(self, received):
            self.paths = received

    monkeypatch.setattr(plugin_runtime, "BrowserManager", Browser)
    monkeypatch.setattr(plugin_runtime, "AuthenticationManager", Authentication)
    _install_metadata(monkeypatch, _GroupedEntryPoint("companion", SERVICE_ENTRY_POINT_GROUP, lambda context: (
        context.acquire_browser_manager(), context.acquire_authentication_manager()
    )))
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "companion")

    result = invoke_service("companion", paths=paths)

    assert result[0].paths is paths
    assert result[1].paths is paths
    assert managers == ["browser-shutdown"]
