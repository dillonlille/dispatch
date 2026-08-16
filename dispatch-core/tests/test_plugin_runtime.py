from __future__ import annotations

import importlib.metadata
from typing import Any

import pytest

from plugin_runtime import PluginRuntimeError, discover_plugins, invoke_plugin, plugin_health


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
