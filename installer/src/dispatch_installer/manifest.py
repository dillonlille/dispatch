from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .layout import InstallerError


_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
_PLUGIN_ID = re.compile(r"[a-z][a-z0-9-]{0,63}")
_PLUGIN_PACKAGE = re.compile(r"dispatch-[a-z0-9][a-z0-9-]{0,63}")
_SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class ReleaseArtifact:
    url: str | None
    size: int | None
    sha256: str | None

    @property
    def complete(self) -> bool:
        return self.url is not None and self.size is not None and self.sha256 is not None


@dataclass(frozen=True, slots=True)
class BuiltinPlugin:
    id: str
    package: str
    version: str
    artifact: ReleaseArtifact
    capabilities: tuple[str, ...]
    requires_dist: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class InstallationManifest:
    ready: bool
    product_version: str
    installer_version: str
    installer_artifact: ReleaseArtifact
    core_version: str
    core_artifact: ReleaseArtifact
    core_package_files: tuple[tuple[str, str], ...]
    core_requires_dist: tuple[str, ...]
    builtin_plugins: tuple[BuiltinPlugin, ...]
    browser_ready: bool
    browser_install_phase: str
    setup_implemented: bool
    setup_command: str
    uninstall_user_scope_implemented: bool
    uninstall_administrative_command: str
    uninstall_future_user_command: str
    uninstall_default_mode: str
    uninstall_purge_requires_confirmation: bool
    uninstall_privileged_browser_removal_implemented: bool

    @property
    def core_artifact_url(self) -> str | None:
        return self.core_artifact.url

    @property
    def core_artifact_size(self) -> int | None:
        return self.core_artifact.size

    @property
    def core_artifact_sha256(self) -> str | None:
        return self.core_artifact.sha256


def _object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise InstallerError("manifest_json_duplicate", "installation manifest contains duplicate JSON keys")
        value[key] = item
    return value


def _version(value: object, code: str, label: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise InstallerError(code, f"installation manifest {label} version is invalid")
    return value


def _artifact(value: object, code: str, label: str) -> ReleaseArtifact:
    if not isinstance(value, dict) or set(value) != {"url", "size", "sha256"}:
        raise InstallerError(code, f"installation manifest {label} artifact is invalid")
    url = value["url"]
    size = value["size"]
    digest = value["sha256"]
    if url is None and size is None and digest is None:
        return ReleaseArtifact(None, None, None)
    if (
        not isinstance(url, str)
        or not url.startswith("https://")
        or not isinstance(size, int)
        or isinstance(size, bool)
        or size <= 0
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
    ):
        raise InstallerError("manifest_partial_artifact", f"installation manifest {label} artifact is incomplete")
    return ReleaseArtifact(url, size, digest)


def _component(value: object, *, name: str, code: str, label: str) -> tuple[str, ReleaseArtifact]:
    if not isinstance(value, dict) or set(value) != {"name", "version", "artifact"} or value["name"] != name:
        raise InstallerError(code, f"installation manifest {label} declaration is invalid")
    return _version(value["version"], f"{code}_version", label), _artifact(value["artifact"], f"{code}_artifact", label)


def _release_asset_url(product_version: str, package: str, version: str) -> str:
    filename = f"{package.replace('-', '_')}-{version}-py3-none-any.whl"
    return f"https://dispatch.dillonlille.com/releases/{product_version}/{filename}"


def _core_component(value: object) -> tuple[str, ReleaseArtifact, tuple[tuple[str, str], ...], tuple[str, ...]]:
    if not isinstance(value, dict) or set(value) != {"name", "version", "artifact", "package_files", "requires_dist"}:
        raise InstallerError("manifest_core", "installation manifest Core declaration is invalid")
    if value["name"] != "dispatch-core":
        raise InstallerError("manifest_core", "installation manifest Core declaration is invalid")
    package_files = value["package_files"]
    requires_dist = value["requires_dist"]
    if not isinstance(package_files, list) or not package_files or len(package_files) > 256:
        raise InstallerError("manifest_core_policy", "installation manifest Core package policy is invalid")
    parsed_files: list[tuple[str, str]] = []
    seen_paths: set[str] = set()
    for item in package_files:
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise InstallerError("manifest_core_policy", "installation manifest Core package policy is invalid")
        path = item["path"]
        digest = item["sha256"]
        if (
            not isinstance(path, str)
            or not path.startswith("dispatch_core/")
            or PurePosixPath(path).as_posix() != path
            or any(part in {"", ".", "..", "plugin", "plugins"} for part in PurePosixPath(path).parts)
            or path in seen_paths
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise InstallerError("manifest_core_policy", "installation manifest Core package policy is invalid")
        seen_paths.add(path)
        parsed_files.append((path, digest))
    if (
        not isinstance(requires_dist, list)
        or len(requires_dist) > 64
        or any(not isinstance(item, str) or not item or len(item) > 256 for item in requires_dist)
        or len(set(requires_dist)) != len(requires_dist)
    ):
        raise InstallerError("manifest_core_policy", "installation manifest Core dependency policy is invalid")
    return (
        _version(value["version"], "manifest_core_version", "Core"),
        _artifact(value["artifact"], "manifest_core_artifact", "Core"),
        tuple(parsed_files),
        tuple(requires_dist),
    )


def _builtin_plugins(value: object) -> tuple[BuiltinPlugin, ...]:
    if not isinstance(value, list) or len(value) > 32:
        raise InstallerError("manifest_builtin_plugins", "installation manifest built-in plugin catalog is invalid")
    plugins: list[BuiltinPlugin] = []
    seen_ids: set[str] = set()
    seen_packages: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "package",
            "version",
            "artifact",
            "capabilities",
            "requires_dist",
        }:
            raise InstallerError("manifest_builtin_plugin", "installation manifest built-in plugin declaration is invalid")
        plugin_id = item["id"]
        package = item["package"]
        capabilities = item["capabilities"]
        requires_dist = item["requires_dist"]
        if not isinstance(plugin_id, str) or _PLUGIN_ID.fullmatch(plugin_id) is None:
            raise InstallerError("manifest_builtin_plugin", "installation manifest built-in plugin id is invalid")
        if not isinstance(package, str) or _PLUGIN_PACKAGE.fullmatch(package) is None:
            raise InstallerError("manifest_builtin_plugin", "installation manifest built-in plugin package is invalid")
        if plugin_id in seen_ids or package in seen_packages:
            raise InstallerError("manifest_builtin_plugin_duplicate", "installation manifest built-in plugin is duplicated")
        if (
            not isinstance(capabilities, list)
            or len(capabilities) > 16
            or any(not isinstance(item, str) or _PLUGIN_ID.fullmatch(item) is None for item in capabilities)
            or len(set(capabilities)) != len(capabilities)
        ):
            raise InstallerError("manifest_builtin_plugin", "installation manifest built-in plugin capabilities are invalid")
        if (
            not isinstance(requires_dist, list)
            or not requires_dist
            or len(requires_dist) > 32
            or len(set(requires_dist)) != len(requires_dist)
            or any(not isinstance(value, str) or not value or len(value) > 256 for value in requires_dist)
        ):
            raise InstallerError("manifest_builtin_plugin", "installation manifest built-in plugin dependencies are invalid")
        core_requirement = next(
            (value for value in requires_dist if isinstance(value, str) and value.startswith("dispatch-core==")),
            None,
        )
        unsupported_runtime = [
            value
            for value in requires_dist
            if value != core_requirement and isinstance(value, str) and '; extra == "' not in value
        ]
        if core_requirement is None or unsupported_runtime:
            raise InstallerError(
                "manifest_builtin_plugin_dependency_closure",
                "built-in plugin declares an unprovisioned runtime dependency",
            )
        plugins.append(
            BuiltinPlugin(
                id=plugin_id,
                package=package,
                version=_version(item["version"], "manifest_builtin_plugin_version", "built-in plugin"),
                artifact=_artifact(item["artifact"], "manifest_builtin_plugin_artifact", "built-in plugin"),
                capabilities=tuple(capabilities),
                requires_dist=tuple(requires_dist),
            )
        )
        seen_ids.add(plugin_id)
        seen_packages.add(package)
    return tuple(plugins)


def load_manifest(path: Path, *, expected_sha256: str) -> InstallationManifest:
    if path.is_symlink() or not path.is_file():
        raise InstallerError("manifest_unsafe", "installation manifest must be a regular non-symlink file")
    if path.stat().st_size > 1024 * 1024:
        raise InstallerError("manifest_size", "installation manifest exceeds policy")
    if _SHA256.fullmatch(expected_sha256) is None:
        raise InstallerError("manifest_digest_invalid", "expected installation manifest SHA-256 is invalid")
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise InstallerError("manifest_digest_mismatch", "installation manifest SHA-256 mismatch")
    try:
        payload = json.loads(data, object_pairs_hook=_object_without_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallerError("manifest_json_invalid", "installation manifest JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise InstallerError("manifest_shape", "installation manifest shape is invalid")
    expected_keys = {
        "schema_version",
        "ready",
        "product",
        "installer",
        "core",
        "builtin_plugins",
        "browser_runtime",
        "post_install",
        "uninstall",
    }
    if set(payload) != expected_keys:
        raise InstallerError("manifest_shape", "installation manifest shape is invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1 or type(payload["ready"]) is not bool:
        raise InstallerError("manifest_version", "installation manifest version is unsupported")

    product = payload["product"]
    if not isinstance(product, dict) or set(product) != {"name", "version"} or product["name"] != "dispatch":
        raise InstallerError("manifest_product", "installation manifest product declaration is invalid")
    product_version = _version(product["version"], "manifest_product_version", "product")
    installer_version, installer_artifact = _component(
        payload["installer"], name="dispatch-installer", code="manifest_installer", label="installer"
    )
    core_version, core_artifact, core_package_files, core_requires_dist = _core_component(payload["core"])
    builtin_plugins = _builtin_plugins(payload["builtin_plugins"])
    if any(f"dispatch-core=={core_version}" not in plugin.requires_dist for plugin in builtin_plugins):
        raise InstallerError(
            "manifest_builtin_plugin_dependency_closure",
            "built-in plugin Core dependency differs from the product release",
        )

    artifacts = [installer_artifact, core_artifact, *(plugin.artifact for plugin in builtin_plugins)]
    if payload["ready"] and not all(artifact.complete for artifact in artifacts):
        raise InstallerError("manifest_partial_artifact", "ready manifest must publish every release artifact")
    if not payload["ready"] and any(artifact.complete for artifact in artifacts):
        raise InstallerError("manifest_partial_artifact", "incomplete manifest must not publish artifact authority")
    expected_urls = [
        _release_asset_url(product_version, "dispatch-installer", installer_version),
        _release_asset_url(product_version, "dispatch-core", core_version),
        *(_release_asset_url(product_version, plugin.package, plugin.version) for plugin in builtin_plugins),
    ]
    if any(artifact.complete and artifact.url != expected for artifact, expected in zip(artifacts, expected_urls)):
        raise InstallerError("manifest_artifact_url", "release artifact URL differs from product authority")

    browser = payload["browser_runtime"]
    if not isinstance(browser, dict) or set(browser) != {"ready", "install_phase", "selector", "generation_root"}:
        raise InstallerError("manifest_browser", "installation manifest browser declaration is invalid")
    if (
        browser["selector"] != "/etc/dispatch/browser-runtime-active.json"
        or browser["generation_root"] != "/opt/dispatch/browser-runtimes"
    ):
        raise InstallerError("manifest_browser_paths", "installation manifest browser authority paths differ")
    if (
        type(browser["ready"]) is not bool
        or browser["install_phase"] != "setup"
        or (not payload["ready"] and browser["ready"])
    ):
        raise InstallerError("manifest_browser_ready", "installation manifest browser readiness is invalid")

    post_install = payload["post_install"]
    if (
        not isinstance(post_install, dict)
        or set(post_install) != {"setup_implemented", "setup_command", "choices"}
        or type(post_install["setup_implemented"]) is not bool
        or post_install["setup_command"] != "dispatch setup"
        or post_install["choices"] != ["start_setup", "skip_for_now"]
        or (payload["ready"] and post_install["setup_implemented"] is not True)
    ):
        raise InstallerError("manifest_post_install", "installation manifest post-install declaration is invalid")

    uninstall = payload["uninstall"]
    expected_uninstall = {
        "user_scope_implemented": True,
        "administrative_command": "dispatch-installer uninstall",
        "future_user_command": "dispatch uninstall",
        "default_mode": "keep-data",
        "purge_requires_confirmation": True,
        "privileged_browser_removal_implemented": False,
    }
    if not isinstance(uninstall, dict) or uninstall != expected_uninstall:
        raise InstallerError("manifest_uninstall", "installation manifest uninstall declaration is invalid")

    return InstallationManifest(
        ready=payload["ready"],
        product_version=product_version,
        installer_version=installer_version,
        installer_artifact=installer_artifact,
        core_version=core_version,
        core_artifact=core_artifact,
        core_package_files=core_package_files,
        core_requires_dist=core_requires_dist,
        builtin_plugins=builtin_plugins,
        browser_ready=browser["ready"],
        browser_install_phase="setup",
        setup_implemented=post_install["setup_implemented"],
        setup_command="dispatch setup",
        uninstall_user_scope_implemented=True,
        uninstall_administrative_command="dispatch-installer uninstall",
        uninstall_future_user_command="dispatch uninstall",
        uninstall_default_mode="keep-data",
        uninstall_purge_requires_confirmation=True,
        uninstall_privileged_browser_removal_implemented=False,
    )
