from __future__ import annotations

from pathlib import Path

import pytest

from dispatch_core.plugin_runtime import PluginRuntimeError, discover_plugins, invoke_plugin, plugin_health


def _plugin_distribution(tmp_path: Path) -> Path:
    site_packages = tmp_path / "site-packages"
    package = site_packages / "dispatch_example"
    metadata = site_packages / "dispatch_example-1.2.3.dist-info"
    package.mkdir(parents=True)
    metadata.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "service.py").write_text(
        "def handle(request):\n"
        "    action = request.get('action')\n"
        "    return {\n"
        "        'ok': True, 'action': action, 'status': 'ready',\n"
        "        'data': {'request': request}, 'freshness': None,\n"
        "        'delivery': None, 'error': None,\n"
        "    }\n",
        encoding="utf-8",
    )
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: dispatch-example\nVersion: 1.2.3\n",
        encoding="utf-8",
    )
    (metadata / "entry_points.txt").write_text(
        "[dispatch.plugins]\nexample = dispatch_example.service:handle\n",
        encoding="utf-8",
    )
    return site_packages


def test_entry_point_is_discovered_and_invoked(monkeypatch, tmp_path: Path) -> None:
    site_packages = _plugin_distribution(tmp_path)
    monkeypatch.syspath_prepend(str(site_packages))
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "example")
    monkeypatch.setenv("DISPATCH_PLUGIN_PATHS", str(site_packages))

    plugins = discover_plugins()
    response = invoke_plugin("example", {"action": "lookup", "question": "bounded example"})

    assert [plugin.safe_data() for plugin in plugins] == [
        {"id": "example", "distribution": "dispatch-example", "version": "1.2.3"}
    ]
    assert response["data"]["request"]["question"] == "bounded example"
    assert plugin_health(["example"])["ready"] is True


def test_discovery_rejects_unpaired_plugin_configuration(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DISPATCH_ACTIVE_PLUGINS", "example")
    monkeypatch.delenv("DISPATCH_PLUGIN_PATHS", raising=False)

    with pytest.raises(PluginRuntimeError) as error:
        discover_plugins()

    assert error.value.code == "plugin_configuration_invalid"
