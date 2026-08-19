from __future__ import annotations

import importlib.metadata
from pathlib import Path
from typing import Any

import pytest

import plugin_runtime
from collection_manager import CollectionDisposition, CollectionReceipt, CollectorRegistration
from paths import DispatchPaths
from plugin_runtime import (
    COLLECTOR_ENTRY_POINT_GROUP,
    CONFIGURATOR_ENTRY_POINT_GROUP,
    SERVICE_ENTRY_POINT_GROUP,
    PluginRuntimeError,
    discover_collector_providers,
    discover_collector_registrations,
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


def _collector_receipt(_context):
    return CollectionReceipt(CollectionDisposition.NO_DATA, None, 0, True)


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


def test_collector_provider_is_optional_and_returns_trusted_registrations(monkeypatch) -> None:
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "example")
    _install_metadata(monkeypatch)
    assert discover_collector_providers() == []

    registration = CollectorRegistration(
        "example-collector",
        "example",
        "1.2.3",
        _collector_receipt,
        execution_timeout_seconds=12.5,
    )
    _install_metadata(
        monkeypatch,
        _EntryPoint("example", lambda request: _envelope(request["action"])),
        _GroupedEntryPoint(
            "example",
            COLLECTOR_ENTRY_POINT_GROUP,
            lambda: (registration,),
        ),
    )

    providers = discover_collector_providers()
    assert providers[0].safe_data()["collectors"][0]["collector_id"] == "example-collector"
    assert discover_collector_registrations() == (registration,)


def test_collector_provider_rejects_wrong_plugin_registration(monkeypatch) -> None:
    _install_metadata(
        monkeypatch,
        _EntryPoint("example", lambda request: _envelope(request["action"])),
        _GroupedEntryPoint(
            "example",
            COLLECTOR_ENTRY_POINT_GROUP,
            lambda: (CollectorRegistration("other-collector", "other", "1.0.0", _collector_receipt),),
        ),
    )
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "example")

    with pytest.raises(PluginRuntimeError) as error:
        discover_collector_registrations()

    assert error.value.code == "plugin_collector_registration_invalid"


def test_collector_provider_rejects_empty_or_unpicklable_registrations(monkeypatch) -> None:
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "example")
    plugin = _EntryPoint("example", lambda request: _envelope(request["action"]))
    _install_metadata(
        monkeypatch,
        plugin,
        _GroupedEntryPoint("example", COLLECTOR_ENTRY_POINT_GROUP, lambda: ()),
    )
    with pytest.raises(PluginRuntimeError) as empty_error:
        discover_collector_providers()
    assert empty_error.value.code == "plugin_collector_registration_invalid"

    registration = CollectorRegistration(
        "example-collector",
        "example",
        "1.2.3",
        lambda context: _collector_receipt(context),
    )
    _install_metadata(
        monkeypatch,
        plugin,
        _GroupedEntryPoint("example", COLLECTOR_ENTRY_POINT_GROUP, lambda: (registration,)),
    )
    with pytest.raises(PluginRuntimeError) as pickle_error:
        discover_collector_providers()
    assert pickle_error.value.code == "plugin_collector_registration_invalid"


def test_collector_provider_is_bound_to_plugin_distribution_and_release(monkeypatch) -> None:
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "example")
    registration = CollectorRegistration("example-collector", "example", "wrong", _collector_receipt)
    plugin_distribution = _Distribution("dispatch-example", "1.2.3")
    provider_distribution = _Distribution("dispatch-other", "9.9.9")
    _install_metadata(
        monkeypatch,
        _EntryPoint("example", lambda request: _envelope(request["action"]), plugin_distribution),
        _GroupedEntryPoint(
            "example",
            COLLECTOR_ENTRY_POINT_GROUP,
            lambda: (registration,),
            provider_distribution,
        ),
    )

    with pytest.raises(PluginRuntimeError) as error:
        discover_collector_providers()

    assert error.value.code == "plugin_collector_provider_invalid"


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


def test_deeply_nested_plugin_request_is_rejected_as_bounded_json(monkeypatch) -> None:
    request: dict[str, Any] = {"leaf": True}
    for _ in range(2000):
        request = {"nested": request}
    monkeypatch.setattr(
        plugin_runtime.json,
        "dumps",
        lambda *args, **kwargs: (_ for _ in ()).throw(RecursionError()),
    )

    with pytest.raises(PluginRuntimeError) as error:
        invoke_plugin("example", request)

    assert error.value.code == "plugin_request_invalid"
    assert str(error.value) == "plugin request is not valid bounded JSON"


def test_deeply_nested_plugin_response_is_rejected_as_bounded_json(monkeypatch) -> None:
    response: dict[str, Any] = {}

    def handle(request):
        data: dict[str, Any] = {"leaf": True}
        for _ in range(2000):
            data = {"nested": data}
        result = {**_envelope(request["action"]), "data": data}
        response["value"] = result
        return result

    _install_metadata(monkeypatch, _EntryPoint("example", handle))
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "example")
    original_dumps = plugin_runtime.json.dumps

    def dumps(value, *args, **kwargs):
        if response.get("value") is value:
            raise RecursionError
        return original_dumps(value, *args, **kwargs)

    monkeypatch.setattr(plugin_runtime.json, "dumps", dumps)

    with pytest.raises(PluginRuntimeError) as error:
        invoke_plugin("example", {"action": "health"})

    assert error.value.code == "plugin_response_invalid"
    assert str(error.value) == "plugin example response is not valid bounded JSON"


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


def test_entry_point_load_base_exception_is_bounded(monkeypatch) -> None:
    entry_point = _GroupedEntryPoint(
        "companion",
        SERVICE_ENTRY_POINT_GROUP,
        lambda context: None,
    )
    entry_point.load = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    _install_metadata(monkeypatch, entry_point)
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "companion")

    with pytest.raises(PluginRuntimeError) as error:
        discover_service("companion")
    assert error.value.code == "plugin_load_failed"


def test_service_and_cleanup_base_exceptions_are_bounded(monkeypatch, tmp_path) -> None:
    paths = DispatchPaths.from_environment(
        {"HOME": str(tmp_path / "home")},
        code_root=Path(__file__).resolve().parents[2],
    )

    class Browser:
        def __init__(self, received): pass
        def shutdown(self): raise KeyboardInterrupt

    def service(context):
        context.acquire_browser_manager()
        raise SystemExit

    monkeypatch.setattr(plugin_runtime, "BrowserManager", Browser)
    _install_metadata(
        monkeypatch,
        _GroupedEntryPoint("companion", SERVICE_ENTRY_POINT_GROUP, service),
    )
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "companion")

    with pytest.raises(PluginRuntimeError) as error:
        invoke_service("companion", paths=paths)
    assert error.value.code == "plugin_service_cleanup_failed"


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
