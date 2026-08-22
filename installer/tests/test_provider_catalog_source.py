"""The installer must read provider policy from Core's catalog, not its own copy."""
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CORE_ROOT = REPOSITORY_ROOT / "dispatch-core"
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))
if str(REPOSITORY_ROOT / "installer" / "src") not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT / "installer" / "src"))

import dispatch_installer.setup as setup_runtime
import provider_catalog


def _write_plugin(root: Path, *, plugin_id: str, provider: str | None) -> Path:
    plugin = root / "plugins" / plugin_id
    package = plugin / "src" / f"dispatch_{plugin_id.replace('-', '_')}"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("def handle(request):\n    return request\n", encoding="utf-8")
    auth = ""
    if provider is not None:
        auth = f"[tool.dispatch.authentication]\nrequired_profiles=[{{provider='{provider}'}}]\n"
    capabilities = "['read_local_data','authentication']" if provider else "['read_local_data']"
    (plugin / "pyproject.toml").write_text(
        "[build-system]\n"
        "requires=['setuptools==83.0.0']\n"
        "build-backend='setuptools.build_meta'\n"
        "[project]\n"
        f"name='dispatch-{plugin_id}'\n"
        "version='1.0.0'\n"
        "dependencies=[]\n"
        "[project.entry-points.\"dispatch.plugins\"]\n"
        f"{plugin_id}='dispatch_{plugin_id.replace('-', '_')}:handle'\n"
        "[tool.dispatch]\n"
        f"id='{plugin_id}'\n"
        f"capabilities={capabilities}\n"
        + auth,
        encoding="utf-8",
    )
    return plugin


def _stage_clone(tmp_path: Path, *, providers: tuple[str, ...], builtin: dict[str, str]) -> Path:
    """Create a minimal staged clone whose catalog declares exactly `providers`."""
    clone = tmp_path / "clone"
    core = clone / "dispatch-core"
    core.mkdir(parents=True)
    builtin_entries = ", ".join(f'"{k}": "{v}"' for k, v in builtin.items())
    (core / "provider_catalog.py").write_text(
        "PROVIDER_CATALOG = (\n"
        + "".join(
            "type('P', (), {'id': %r, 'public_id': %r})(),\n" % (name, name) for name in providers
        )
        + ")\n"
        "PROVIDERS_BY_ID = {item.id: item for item in PROVIDER_CATALOG}\n"
        f"BUILTIN_PLUGIN_PROVIDERS = {{{builtin_entries}}}\n",
        encoding="utf-8",
    )
    return clone


def test_builtin_catalog_declares_known_providers() -> None:
    """Core's real catalog is the authority both consumers claim to mirror."""
    assert set(provider_catalog.PROVIDERS_BY_ID) == {"amazon-operations", "paycom-client"}
    assert provider_catalog.BUILTIN_PLUGIN_PROVIDERS == {
        "companion-bridge": "amazon-operations",
        "paycom": "paycom-client",
    }


def test_installer_accepts_plugin_with_core_provider(tmp_path: Path) -> None:
    """A staged catalog declaring 'acme-portal' admits a plugin requiring it."""
    clone = _stage_clone(tmp_path, providers=("acme-portal",), builtin={})
    plugin = _write_plugin(clone, plugin_id="acme", provider="acme-portal")
    metadata = setup_runtime.plugin_metadata(plugin, expected_id="acme")
    assert metadata["id"] == "acme"


def test_installer_rejects_provider_absent_from_core_catalog(tmp_path: Path) -> None:
    """A provider unknown to CORE's catalog fails even though no other copy exists."""
    clone = _stage_clone(tmp_path, providers=("acme-portal",), builtin={})
    plugin = _write_plugin(clone, plugin_id="acme", provider="not-in-catalog")
    from dispatch_installer.layout import InstallerError

    with pytest.raises(InstallerError) as error:
        setup_runtime.plugin_metadata(plugin, expected_id="acme")
    assert error.value.code == "plugin_authentication_invalid"


def test_installer_enforces_core_builtin_binding(tmp_path: Path) -> None:
    """When Core binds a built-in plugin id to a provider, installer enforces it."""
    clone = _stage_clone(tmp_path, providers=("one", "two"), builtin={"bound2": "two"})
    mismatched = _write_plugin(clone, plugin_id="bound2", provider="one")
    from dispatch_installer.layout import InstallerError

    with pytest.raises(InstallerError) as error:
        setup_runtime.plugin_metadata(mismatched, expected_id="bound2")
    assert error.value.code == "plugin_authentication_invalid"

    # Same plugin id, corrected binding: rewrite pyproject.toml in place.
    manifest = mismatched / "pyproject.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("provider='one'", "provider='two'"),
        encoding="utf-8",
    )
    metadata = setup_runtime.plugin_metadata(mismatched, expected_id="bound2")
    assert metadata["id"] == "bound2"
    assert metadata["required_profiles"] == [{"provider": "two"}]


def test_repository_plugins_validate_against_real_catalog() -> None:
    """Every shipped authenticated plugin matches the canonical catalog binding."""
    for plugin_id, provider in provider_catalog.BUILTIN_PLUGIN_PROVIDERS.items():
        manifest = REPOSITORY_ROOT / "plugins" / plugin_id / "pyproject.toml"
        project = tomllib.loads(manifest.read_text(encoding="utf-8"))
        profiles = project["tool"]["dispatch"]["authentication"]["required_profiles"]
        assert profiles == [{"provider": provider}]
