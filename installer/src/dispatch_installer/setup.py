"""Explicit built-in plugin setup from the checked-out source tree."""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

# Dispatch profile-name slug rule, mirrored from Core authentication
# (_require_slug) so the setup wizard can validate before enrollment.
_PROFILE_NAME = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")

from . import interactive, ui
from .layout import (
    InstallLayout,
    InstallerError,
    assert_directory_ancestors,
    assert_user_owned_directory,
    atomic_json,
    read_json,
)
from .stage_rail import StageRail
from .service import (
    enable_plugin_service,
    inspect_plugin_services,
    plugin_service_ids,
    prepare_plugin_service,
    remove_plugin_service,
    restore_plugin_service_states,
    restore_systemd_service_state,
    service_unit_is_owned,
    stop_plugin_services_for_activation,
    systemd_service_state,
)

RunCommand = Callable[[Sequence[str], Path | None], subprocess.CompletedProcess[str]]

_PLUGIN_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_PINNED_DEPENDENCY = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9_,.-]+\])?==[A-Za-z0-9][A-Za-z0-9._+-]*$"
)
_PLUGIN_CAPABILITIES = {
    "read_local_data",
    "mutate_data",
    "collect",
    "network",
    "authentication",
    "direct_delivery",
    "long_running",
}
_AUTH_PROVIDERS = {"amazon-operations", "paycom-client"}
_BUILTIN_PLUGIN_PROVIDERS = {
    "companion-bridge": "amazon-operations",
    "paycom": "paycom-client",
}


def _run(command: Sequence[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)


def _generated_package_metadata(source: Path) -> set[Path]:
    roots = (source, source / "src")
    patterns = ("*.egg-info", "*.dist-info")
    return {
        candidate
        for root in roots
        if root.is_dir() and not root.is_symlink()
        for pattern in patterns
        for candidate in root.glob(pattern)
    }


def assert_source_project_safe(source: Path) -> Path:
    absolute = Path(os.path.abspath(source))
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise InstallerError("source_project_unsafe", "source project is missing or unsafe") from exc
    if resolved != absolute or source.is_symlink() or not source.is_dir():
        raise InstallerError("source_project_unsafe", "source project aliases are not allowed")
    for candidate in (source, *source.rglob("*")):
        details = candidate.lstat()
        if details.st_uid != os.geteuid() or candidate.is_symlink() or details.st_mode & 0o022:
            raise InstallerError("source_project_unsafe", "source project entries are unsafe")
        if candidate != source and not (
            stat.S_ISDIR(details.st_mode)
            or (stat.S_ISREG(details.st_mode) and details.st_nlink == 1)
        ):
            raise InstallerError("source_project_unsafe", "source project entries are unsafe")
    return absolute


def _direct_source_roots(source: Path, project: dict[str, object]) -> list[Path]:
    tool = project.get("tool", {})
    setuptools = tool.get("setuptools", {}) if isinstance(tool, dict) else {}
    if not isinstance(setuptools, dict):
        raise InstallerError("source_manifest_invalid", "setuptools metadata is invalid")
    package_dir = setuptools.get("package-dir", {})
    roots: set[Path] = set()
    if isinstance(package_dir, dict) and "" in package_dir:
        value = package_dir[""]
        if not isinstance(value, str):
            raise InstallerError("source_manifest_invalid", "package-dir is invalid")
        roots.add(source / value)
    elif isinstance(package_dir, dict) and package_dir:
        for package, value in package_dir.items():
            if not isinstance(package, str) or not isinstance(value, str) or not package:
                raise InstallerError("source_manifest_invalid", "package-dir is invalid")
            root = source / value
            for _ in package.split("."):
                root = root.parent
            roots.add(root)
    else:
        find = setuptools.get("packages", {})
        if isinstance(find, dict):
            values = find.get("find", {})
            where = values.get("where", ["."]) if isinstance(values, dict) else ["."]
            if not isinstance(where, list) or any(not isinstance(value, str) for value in where):
                raise InstallerError("source_manifest_invalid", "package discovery roots are invalid")
            roots.update(source / value for value in where)
        else:
            roots.add(source)
    result = sorted(roots, key=str)
    for root in result:
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise InstallerError("source_manifest_invalid", "package source root is missing") from exc
        if not resolved.is_relative_to(source) or root.is_symlink() or not root.is_dir():
            raise InstallerError("source_manifest_invalid", "package source root is unsafe")
    return result


_ENTRY_POINT_TARGET = re.compile(
    r"^(?P<module>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*):"
    r"(?P<attribute>[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)$"
)


def _source_callable_target(
    source: Path,
    configuration: dict[str, object],
    target: str,
    plugin_id: str,
    *,
    argument_count: int | None = None,
) -> None:
    """Require an entry-point target that resolves to a callable in source.

    Setup must not complete merely because a manifest contains a plausible
    entry-point string.  Resolve the module through the declared source roots
    and inspect its source without importing plugin code (which can have
    optional dependencies or side effects during installation).
    """

    match = _ENTRY_POINT_TARGET.fullmatch(target)
    if match is None:
        raise InstallerError(
            "plugin_entry_point_invalid",
            f"plugin {plugin_id} dispatch.plugins target is invalid",
        )
    module_parts = match.group("module").split(".")
    attribute_parts = match.group("attribute").split(".")
    module_path: Path | None = None
    module_text: str | None = None
    source_roots = _direct_source_roots(source, configuration)
    if (source / "src").is_dir() and source / "src" not in source_roots:
        source_roots = [*source_roots, source / "src"]
    for root in source_roots:
        candidate = root.joinpath(*module_parts).with_suffix(".py")
        package_candidate = root.joinpath(*module_parts, "__init__.py")
        for possible in (candidate, package_candidate):
            if possible.is_symlink() or not possible.is_file():
                continue
            try:
                module_text = possible.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise InstallerError(
                    "plugin_entry_point_invalid",
                    f"plugin {plugin_id} dispatch.plugins target cannot be read",
                ) from exc
            module_path = possible
            break
        if module_path is not None:
            break
    if module_path is None or module_text is None:
        raise InstallerError(
            "plugin_entry_point_invalid",
            f"plugin {plugin_id} dispatch.plugins target is not in source",
        )
    try:
        tree = ast.parse(module_text, filename=str(module_path))
    except SyntaxError as exc:
        raise InstallerError(
            "plugin_entry_point_invalid",
            f"plugin {plugin_id} dispatch.plugins target source is invalid",
        ) from exc

    callable_names: set[str] = set()
    class_members: dict[str, set[str]] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            callable_names.add(node.name)
            if isinstance(node, ast.ClassDef):
                class_members[node.name] = {
                    member.name
                    for member in node.body
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                }
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Lambda):
            callable_names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )
    if attribute_parts[0] not in callable_names:
        for node in tree.body:
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            for alias in node.names:
                exported = alias.asname or alias.name
                if exported != attribute_parts[0]:
                    continue
                if node.level == 1:
                    imported_module = ".".join([*module_parts[:-1], node.module])
                elif node.level == 0:
                    imported_module = node.module
                else:
                    continue
                _source_callable_target(
                    source,
                    configuration,
                    f"{imported_module}:{alias.name}",
                    plugin_id,
                    argument_count=argument_count,
                )
                return
        raise InstallerError(
            "plugin_entry_point_invalid",
            f"plugin {plugin_id} dispatch.plugins target is not callable in source",
        )
    if len(attribute_parts) > 1 and (
        attribute_parts[0] not in class_members
        or attribute_parts[-1] not in class_members[attribute_parts[0]]
    ):
        raise InstallerError(
            "plugin_entry_point_invalid",
            f"plugin {plugin_id} dispatch.plugins target is not callable in source",
        )
    if argument_count is not None:
        callable_node = next(
            (
                node
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda))
                and getattr(node, "name", attribute_parts[0]) == attribute_parts[0]
            ),
            None,
        )
        if callable_node is None or len(attribute_parts) > 1:
            raise InstallerError(
                "plugin_entry_point_invalid",
                f"plugin {plugin_id} entry point signature cannot be verified",
            )
        arguments = callable_node.args
        positional = [*arguments.posonlyargs, *arguments.args]
        required = len(positional) - len(arguments.defaults)
        maximum = None if arguments.vararg is not None else len(positional)
        required_keywords = sum(default is None for default in arguments.kw_defaults)
        if (
            argument_count < required
            or (maximum is not None and argument_count > maximum)
            or required_keywords
        ):
            raise InstallerError(
                "plugin_entry_point_invalid",
                f"plugin {plugin_id} entry point does not accept {argument_count} positional arguments",
            )


def _plugin_project(source: Path, *, expected_id: str | None = None) -> dict[str, object]:
    """Read the trusted built-in plugin metadata used by installer projections.

    The checkout has already passed the repository authority checks when this is
    called from lifecycle code.  We still validate the source boundary and the
    small metadata subset that is allowed to influence pip or systemd.  In
    particular, dependencies are returned as individual, bounded arguments and
    never as shell text.
    """

    source = assert_source_project_safe(source)
    manifest = source / "pyproject.toml"
    try:
        configuration = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallerError("plugin_manifest_invalid", "built-in plugin metadata cannot be read") from exc
    if not isinstance(configuration, dict):
        raise InstallerError("plugin_manifest_invalid", "built-in plugin metadata is invalid")
    project = configuration.get("project")
    tool = configuration.get("tool")
    dispatch = tool.get("dispatch") if isinstance(tool, dict) else None
    if not isinstance(project, dict) or not isinstance(dispatch, dict):
        raise InstallerError("plugin_manifest_invalid", "built-in plugin metadata is incomplete")
    plugin_id = dispatch.get("id")
    if not isinstance(plugin_id, str) or not _PLUGIN_ID.fullmatch(plugin_id):
        raise InstallerError("plugin_manifest_invalid", "built-in plugin ID is invalid")
    if expected_id is not None and plugin_id != expected_id:
        raise InstallerError("plugin_manifest_invalid", "built-in plugin ID does not match its source directory")
    capabilities = dispatch.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or not capabilities
        or any(not isinstance(value, str) or value not in _PLUGIN_CAPABILITIES for value in capabilities)
        or len(capabilities) != len(set(capabilities))
    ):
        raise InstallerError("plugin_manifest_invalid", f"plugin capabilities are invalid: {plugin_id}")
    dependencies = project.get("dependencies", [])
    if not isinstance(dependencies, list) or any(not isinstance(value, str) for value in dependencies):
        raise InstallerError("plugin_manifest_invalid", f"plugin dependencies are invalid: {plugin_id}")
    safe_dependencies: list[str] = []
    for dependency in dependencies:
        if (
            not dependency
            or len(dependency) > 512
            or dependency[0] in "-_"
            or not _PINNED_DEPENDENCY.fullmatch(dependency)
            or any(ord(character) < 32 or ord(character) == 127 for character in dependency)
        ):
            raise InstallerError("plugin_dependency_invalid", f"plugin dependency is invalid: {plugin_id}")
        safe_dependencies.append(dependency)
    entry_points = project.get("entry-points", {})
    if not isinstance(entry_points, dict):
        raise InstallerError("plugin_manifest_invalid", f"plugin entry points are invalid: {plugin_id}")
    plugin_points = entry_points.get("dispatch.plugins")
    if (
        not isinstance(plugin_points, dict)
        or set(plugin_points) != {plugin_id}
        or not isinstance(plugin_points.get(plugin_id), str)
    ):
        raise InstallerError(
            "plugin_entry_point_invalid",
            f"plugin {plugin_id} must publish exactly one same-ID dispatch.plugins entry point",
        )
    service_points = entry_points.get("dispatch.services", {})
    collector_points = entry_points.get("dispatch.collectors", {})
    configurator_points = entry_points.get("dispatch.configurators", {})
    for role, points in (
        ("service", service_points),
        ("collector", collector_points),
        ("configurator", configurator_points),
    ):
        if not isinstance(points, dict) or any(
            not isinstance(name, str) or not isinstance(value, str)
            for name, value in points.items()
        ):
            raise InstallerError("plugin_manifest_invalid", f"plugin {role} entry points are invalid: {plugin_id}")
    long_running = "long_running" in capabilities
    if long_running and set(service_points) != {plugin_id}:
        raise InstallerError("plugin_service_missing", f"long-running plugin has no unique service entry point: {plugin_id}")
    if not long_running and service_points:
        raise InstallerError("plugin_service_unexpected", f"non-service plugin publishes a service entry point: {plugin_id}")
    collects = "collect" in capabilities
    if collects and set(collector_points) != {plugin_id}:
        raise InstallerError("plugin_collector_missing", f"collecting plugin has no unique collector entry point: {plugin_id}")
    if not collects and collector_points:
        raise InstallerError("plugin_collector_unexpected", f"non-collecting plugin publishes a collector entry point: {plugin_id}")
    if set(configurator_points) not in (set(), {plugin_id}):
        raise InstallerError("plugin_configurator_invalid", f"plugin configurator entry point is invalid: {plugin_id}")
    authentication = dispatch.get("authentication")
    if authentication is None:
        authentication = {"required_profiles": []}
    if not isinstance(authentication, dict) or set(authentication) != {"required_profiles"}:
        raise InstallerError("plugin_authentication_invalid", f"plugin authentication metadata is invalid: {plugin_id}")
    required_profiles = authentication["required_profiles"]
    if (
        not isinstance(required_profiles, list)
        or len(required_profiles) > 1
        or any(
            not isinstance(item, dict)
            or set(item) != {"provider"}
            or not isinstance(item.get("provider"), str)
            or item["provider"] not in _AUTH_PROVIDERS
            for item in required_profiles
        )
        or ("authentication" in capabilities and len(required_profiles) != 1)
        or ("authentication" not in capabilities and required_profiles)
        or (
            plugin_id in _BUILTIN_PLUGIN_PROVIDERS
            and required_profiles != [{"provider": _BUILTIN_PLUGIN_PROVIDERS[plugin_id]}]
        )
    ):
        raise InstallerError("plugin_authentication_invalid", f"plugin required profile metadata is invalid: {plugin_id}")
    return {
        "id": plugin_id,
        "capabilities": list(capabilities),
        "dependencies": safe_dependencies,
        "long_running": long_running,
        "collects": collects,
        "required_profiles": [dict(item) for item in required_profiles],
        "project": project,
        "configuration": configuration,
    }


def plugin_metadata(source: Path, *, expected_id: str | None = None) -> dict[str, object]:
    """Return install-admissible metadata for a source-owned built-in plugin."""

    metadata = _plugin_project(source, expected_id=expected_id)
    plugin_id = str(metadata["id"])
    configuration = metadata["configuration"]
    project = metadata["project"]
    assert isinstance(configuration, dict) and isinstance(project, dict)
    groups = project["entry-points"]
    assert isinstance(groups, dict)
    expected_arguments = {
        "dispatch.plugins": 1,
        "dispatch.services": 1,
        "dispatch.collectors": 0,
        "dispatch.configurators": 1,
    }
    for group, argument_count in expected_arguments.items():
        points = groups.get(group, {})
        if not isinstance(points, dict):
            continue
        for target in points.values():
            assert isinstance(target, str)
            _source_callable_target(
                source,
                configuration,
                target,
                plugin_id,
                argument_count=argument_count,
            )
    return metadata


def plugin_dependencies(source: Path, *, expected_id: str | None = None) -> tuple[str, ...]:
    """Return trusted built-in runtime dependencies for explicit pip install."""

    metadata = plugin_metadata(source, expected_id=expected_id)
    dependencies = metadata["dependencies"]
    assert isinstance(dependencies, list)
    return tuple(str(value) for value in dependencies)


BUILD_BACKEND_REQUIREMENT = "setuptools==83.0.0"


def install_source_distribution(
    python: Path,
    source: Path,
    *,
    no_deps: bool = False,
    run: RunCommand = _run,
) -> subprocess.CompletedProcess[str]:
    """Install validated source through pip without mutating the checkout."""

    source = assert_source_project_safe(source)
    existing = _generated_package_metadata(source)
    if existing:
        raise InstallerError(
            "source_metadata_exists",
            "preexisting package metadata makes the checkout unsafe",
        )
    manifest = source / "pyproject.toml"
    try:
        configuration = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallerError("source_manifest_invalid", "project metadata cannot be read") from exc
    project = configuration.get("project", {})
    build_system = configuration.get("build-system", {})
    if not isinstance(project, dict) or not isinstance(build_system, dict):
        raise InstallerError("source_manifest_invalid", "project metadata is invalid")
    name = project.get("name")
    version = project.get("version")
    dependencies = project.get("dependencies", [])
    if not isinstance(name, str) or not isinstance(version, str) or not name or not version:
        raise InstallerError("source_manifest_invalid", "project name or version is invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version):
        raise InstallerError("source_manifest_invalid", "project version is unsafe")
    if not isinstance(dependencies, list) or any(not isinstance(value, str) for value in dependencies):
        raise InstallerError("source_manifest_invalid", "project dependencies are invalid")
    if dependencies and not no_deps:
        raise InstallerError("source_dependencies_unsupported", "source dependencies must be installed explicitly")
    if (
        build_system.get("build-backend") != "setuptools.build_meta"
        or build_system.get("requires") != [BUILD_BACKEND_REQUIREMENT]
    ):
        raise InstallerError(
            "source_build_backend_invalid",
            f"source projects must use the pinned {BUILD_BACKEND_REQUIREMENT} build backend",
        )

    normalized = re.sub(r"[-_.]+", "-", name).lower()
    temporary_parent = Path(tempfile.gettempdir())
    assert_directory_ancestors(temporary_parent, "temporary directory")
    work = Path(tempfile.mkdtemp(prefix=f"dispatch-source-{normalized}-", dir=temporary_parent))
    work.chmod(0o700)
    staged = work / "source"
    completed: subprocess.CompletedProcess[str] | None = None
    primary_error: BaseException | None = None
    try:
        shutil.copytree(source, staged, symlinks=False)
        completed = run(
            (
                str(python),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "--no-build-isolation",
                "--no-deps",
                "--force-reinstall",
                str(staged),
            ),
            None,
        )
    except BaseException as exc:
        primary_error = exc
    cleanup_error: BaseException | None = None
    try:
        shutil.rmtree(work)
    except BaseException as exc:
        cleanup_error = exc
    if primary_error is not None:
        if cleanup_error is not None:
            raise InstallerError(
                "source_stage_cleanup_failed",
                "source installation failed and private staging could not be removed",
            ) from cleanup_error
        raise primary_error
    if cleanup_error is not None:
        raise InstallerError(
            "source_stage_cleanup_failed",
            "private source staging could not be removed",
        ) from cleanup_error
    assert completed is not None
    return completed


def _plugin_id_map(layout: InstallLayout) -> dict[str, Path]:
    root = layout.clone / "plugins"
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        raise InstallerError("plugins_root_unsafe", "cloned plugins directory is unsafe")
    if not root.exists():
        return {}
    result: dict[str, Path] = {}
    for directory in sorted(root.iterdir(), key=lambda item: item.name):
        manifest = directory / "pyproject.toml"
        if directory.is_symlink() or not directory.is_dir() or manifest.is_symlink() or not manifest.is_file():
            continue
        try:
            project = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
            raise InstallerError("plugin_manifest_invalid", f"cannot read plugin metadata: {manifest}") from exc
        entry_points = project.get("project", {}).get("entry-points", {}).get("dispatch.plugins", {})
        if not isinstance(entry_points, dict):
            raise InstallerError("plugin_manifest_invalid", f"plugin entry points are invalid: {manifest}")
        dispatch = project.get("tool", {}).get("dispatch", {})
        manifest_id = dispatch.get("id") if isinstance(dispatch, dict) else None
        ids = [manifest_id] if isinstance(manifest_id, str) else []
        if not ids:
            ids = [value for value in entry_points if isinstance(value, str)]
        if not ids:
            ids = [directory.name]
        for plugin_id in ids:
            if plugin_id in result:
                raise InstallerError("plugin_duplicate", f"built-in plugin ID is duplicated: {plugin_id}")
            result[plugin_id] = directory
    return result


def available_plugins(layout: InstallLayout) -> list[str]:
    return sorted(_plugin_id_map(layout))


def _site_packages(layout: InstallLayout) -> Path:
    candidates = sorted((layout.venv / "lib").glob("python*/site-packages")) if (layout.venv / "lib").exists() else []
    if candidates:
        return candidates[-1]
    return layout.venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"


def _plugin_config(layout: InstallLayout, selected: list[str]) -> dict[str, object]:
    site_packages = _site_packages(layout)
    catalog = _plugin_id_map(layout)
    plugins: list[dict[str, object]] = []
    for plugin_id in selected:
        if plugin_id not in catalog:
            raise InstallerError("plugin_config_invalid", f"configured plugin is not present in the checkout: {plugin_id}")
        metadata = _plugin_project(catalog[plugin_id], expected_id=plugin_id)
        capabilities = metadata.get("capabilities")
        required_profiles = metadata.get("required_profiles", [])
        if not isinstance(capabilities, list) or not isinstance(required_profiles, list):
            raise InstallerError("plugin_manifest_invalid", f"plugin capabilities are invalid: {plugin_id}")
        plugins.append(
            {
                "id": plugin_id,
                "source": str(catalog[plugin_id]),
                "site_packages": str(site_packages),
                "capabilities": capabilities,
                "required_profiles": required_profiles,
            }
        )
    return {
        "schema_version": 1,
        "status": "complete",
        "selected_plugins": selected,
        "plugins": plugins,
        "contains_secrets": False,
    }


def _long_running_plugin_ids(layout: InstallLayout, selected: Sequence[str]) -> list[str]:
    catalog = _plugin_id_map(layout)
    unknown = sorted(set(selected) - set(catalog))
    if unknown:
        raise InstallerError("plugin_unknown", f"selected plugin source is missing: {unknown[0]}")
    result: list[str] = []
    for plugin_id in selected:
        metadata = plugin_metadata(catalog[plugin_id], expected_id=plugin_id)
        if metadata.get("long_running") is True:
            result.append(plugin_id)
    return result


def reconcile_plugin_services(
    layout: InstallLayout,
    selected: Sequence[str],
    *,
    run: RunCommand = _run,
) -> dict[str, object]:
    """Publish selected long-running units and remove deselected projections.

    Selection only prepares a disabled projection.  A previously explicitly
    enabled service is re-enabled after an update/repair projection refresh;
    no first-time selection is ever started implicitly.
    """

    selected_ids = list(selected)
    desired = set(_long_running_plugin_ids(layout, selected_ids))
    existing = plugin_service_ids(layout)
    inspected = inspect_plugin_services(layout, run=run)
    if inspected.get("status") != "ready":
        raise InstallerError(
            "plugin_service_unsafe",
            "plugin service projections are incomplete or not exact Dispatch projections",
        )
    services_value = inspected.get("services", {})
    services = services_value if isinstance(services_value, dict) else {}
    previously_enabled = {
        plugin_id
        for plugin_id, value in services.items()
        if isinstance(value, dict) and value.get("enabled") is True
    }
    removed: list[str] = []
    for plugin_id in sorted(existing - desired):
        remove_plugin_service(layout, plugin_id, run=run)
        removed.append(plugin_id)
    prepared: list[dict[str, object]] = []
    enabled: list[dict[str, object]] = []
    for plugin_id in sorted(desired):
        prepared.append(prepare_plugin_service(layout, plugin_id))
        if plugin_id in previously_enabled:
            enabled.append(enable_plugin_service(layout, plugin_id, run=run))
    return {
        "status": "ready",
        "prepared": prepared,
        "enabled": enabled,
        "removed": removed,
    }


def configure_plugins(
    layout: InstallLayout,
    selected: Sequence[str],
    *,
    run: RunCommand = _run,
) -> dict[str, object]:
    selected_ids = list(selected)
    if len(selected_ids) != len(set(selected_ids)):
        raise InstallerError("plugin_duplicate", "a plugin cannot be selected twice")
    catalog = _plugin_id_map(layout)
    unknown = sorted(set(selected_ids) - set(catalog))
    if unknown:
        raise InstallerError("plugin_unknown", f"unknown built-in plugin: {unknown[0]}")
    service_present = layout.service_path.exists() or layout.service_path.is_symlink()
    if service_present and not service_unit_is_owned(layout):
        raise InstallerError("service_unit_unsafe", "Dispatch service unit is not Dispatch-owned")
    main_service_state = (
        systemd_service_state("dispatch.service", run=run)
        if service_present
        else {"active": False, "enabled": False}
    )

    metadata = {
        plugin_id: plugin_metadata(catalog[plugin_id], expected_id=plugin_id)
        for plugin_id in selected_ids
    }
    dependencies: dict[str, tuple[str, ...]] = {}
    for plugin_id, value in metadata.items():
        raw_dependencies = value.get("dependencies")
        if isinstance(raw_dependencies, list):
            dependencies[plugin_id] = tuple(str(item) for item in raw_dependencies)
    replacement_ready = (
        layout.venv.is_dir()
        and not layout.venv.is_symlink()
        and (layout.clone / "dispatch-core" / "requirements.txt").is_file()
        and not (layout.clone / "dispatch-core" / "requirements.txt").is_symlink()
        and layout.installer_source.is_dir()
        and not layout.installer_source.is_symlink()
    )
    if not replacement_ready:
        raise InstallerError(
            "plugin_environment_unavailable",
            "plugin setup requires a complete installed checkout and environment",
        )

    previous_config = load_plugin_config(layout) if (layout.config / "plugins.json").exists() else None
    previous_selected = previous_config.get("selected_plugins", []) if previous_config else []
    if not isinstance(previous_selected, list) or any(not isinstance(value, str) for value in previous_selected):
        raise InstallerError("plugin_config_invalid", "plugin configuration selection is invalid")

    work: Path | None = None
    backup: Path | None = None
    stopped_plugin_services: list[dict[str, object]] = []
    main_service_stopped = False
    config: dict[str, object] | None = None
    try:
        from .lifecycle import _safe_remove, _swap_directory, ensure_venv

        work = Path(tempfile.mkdtemp(prefix=".dispatch-plugin-", dir=layout.dispatch_home))
        work.chmod(0o700)
        replacement = work / "venv"
        ensure_venv(
            layout,
            destination=replacement,
            selected_plugins=selected_ids,
            provision_browser=False,
            run=run,
        )
        stopped_plugin_services = stop_plugin_services_for_activation(
            layout,
            previous_selected,
            run=run,
        )
        if service_present:
            main_service_stopped = True
            stopped = run(("systemctl", "--user", "stop", "dispatch.service"), None)
            if stopped.returncode != 0:
                raise InstallerError(
                    "service_stop_failed",
                    "Dispatch service could not be stopped before plugin environment activation",
                )
        backup = _swap_directory(replacement, layout.venv)

        config = _plugin_config(layout, selected_ids)
        atomic_json(layout.config / "plugins.json", config)
        plugin_services = reconcile_plugin_services(layout, selected_ids, run=run)
        restore_plugin_service_states(
            layout,
            stopped_plugin_services,
            allowed_ids=selected_ids,
            run=run,
        )
        if service_present:
            if main_service_state.get("active") is True:
                completed = run(("systemctl", "--user", "restart", "dispatch.service"), None)
                if completed.returncode != 0:
                    raise InstallerError("service_restart_failed", "Dispatch service could not be restarted after setup")
            else:
                restore_systemd_service_state(
                    "dispatch.service",
                    main_service_state,
                    run=run,
                )
    except BaseException as primary:
        rollback_failure: BaseException | None = None
        cleanup_error: BaseException | None = None

        def attempt(action: Callable[[], object]) -> None:
            nonlocal rollback_failure
            try:
                action()
            except BaseException as exc:
                rollback_failure = rollback_failure or exc

        if backup is not None:
            from .lifecycle import _complete_rollback, _restore_directory

            attempt(lambda: _complete_rollback(lambda: _restore_directory(layout.venv, backup)))
        if previous_config is None:
            attempt(lambda: (layout.config / "plugins.json").unlink(missing_ok=True))
        else:
            attempt(lambda: atomic_json(layout.config / "plugins.json", previous_config))
        if previous_selected != selected_ids:
            attempt(lambda: reconcile_plugin_services(layout, previous_selected, run=run))
        attempt(lambda: restore_plugin_service_states(layout, stopped_plugin_services, run=run))
        if service_present and main_service_stopped:
            attempt(
                lambda: restore_systemd_service_state(
                    "dispatch.service",
                    main_service_state,
                    run=run,
                )
            )
        if work is not None:
            try:
                from .lifecycle import _safe_remove

                _safe_remove(work)
            except BaseException as exc:
                cleanup_error = exc
        if rollback_failure is not None:
            raise InstallerError(
                "plugin_environment_rollback_failed",
                "plugin setup failed and the previous environment could not be fully restored",
            ) from primary
        if cleanup_error is not None:
            raise InstallerError(
                "plugin_environment_cleanup_failed",
                "plugin setup failed and private staging could not be removed",
            ) from cleanup_error
        raise primary
    else:
        cleanup_error: BaseException | None = None
        if backup is not None:
            try:
                from .lifecycle import _safe_remove

                _safe_remove(backup)
            except BaseException as exc:
                cleanup_error = exc
        if work is not None:
            try:
                from .lifecycle import _safe_remove

                _safe_remove(work)
            except BaseException as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None:
            raise InstallerError(
                "plugin_environment_cleanup_failed",
                "plugin setup completed but obsolete private staging could not be removed",
            ) from cleanup_error

    assert config is not None
    return {
        "status": "complete",
        "selected_plugins": selected_ids,
        "plugins": config["plugins"],
        "services": plugin_services,
    }


def selected_long_running_plugins(layout: InstallLayout) -> list[str]:
    config = load_plugin_config(layout)
    selected = config.get("selected_plugins", [])
    if not isinstance(selected, list) or any(not isinstance(value, str) for value in selected):
        raise InstallerError("plugin_config_invalid", "plugin configuration selection is invalid")
    return _long_running_plugin_ids(layout, selected)


def load_plugin_config(layout: InstallLayout) -> dict[str, object]:
    path = layout.config / "plugins.json"
    if not path.exists():
        return {"schema_version": 1, "plugins": [], "contains_secrets": False}
    try:
        payload = read_json(path)
    except InstallerError as exc:
        raise InstallerError("plugin_config_invalid", "plugin configuration is invalid") from exc
    selected = payload.get("selected_plugins")
    plugins = payload.get("plugins")
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("contains_secrets") is not False
        or payload.get("status") != "complete"
        or not isinstance(selected, list)
        or any(not isinstance(value, str) for value in selected)
        or len(selected) != len(set(selected))
        or not isinstance(plugins, list)
    ):
        raise InstallerError("plugin_config_invalid", "plugin configuration is invalid")
    try:
        expected = _plugin_config(layout, list(selected))
    except (InstallerError, OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallerError("plugin_config_invalid", "plugin configuration authority is invalid") from exc
    if payload != expected:
        # Configurations written before profile metadata gained its projection
        # field remain valid. Normalize only that non-secret field; profile
        # credentials live in the separate encrypted Core store.
        legacy = dict(payload)
        legacy_plugins: list[object] = []
        expected_plugins = expected.get("plugins")
        if not isinstance(expected_plugins, list) or len(plugins) != len(expected_plugins):
            raise InstallerError("plugin_config_invalid", "plugin configuration does not match the installed checkout")
        for actual, wanted in zip(plugins, expected_plugins, strict=True):
            if not isinstance(actual, dict) or not isinstance(wanted, dict):
                raise InstallerError("plugin_config_invalid", "plugin configuration does not match the installed checkout")
            if set(actual) - {"required_profiles"} != set(wanted) - {"required_profiles"}:
                raise InstallerError("plugin_config_invalid", "plugin configuration does not match the installed checkout")
            normalized = dict(actual)
            normalized.setdefault("required_profiles", wanted.get("required_profiles", []))
            legacy_plugins.append(normalized)
        legacy["plugins"] = legacy_plugins
        if legacy != expected:
            raise InstallerError("plugin_config_invalid", "plugin configuration does not match the installed checkout")
        return expected
    return payload


def migrate_legacy_plugin_config(layout: InstallLayout) -> bool:
    """Import only a complete, non-secret legacy built-in selection."""
    current = layout.config / "plugins.json"
    legacy = layout.state / "install" / "setup.json"
    try:
        assert_user_owned_directory(legacy.parent, "legacy installation state")
    except InstallerError:
        return False
    if current.exists() or not legacy.is_file() or legacy.is_symlink():
        return False
    try:
        payload = read_json(legacy)
    except InstallerError:
        return False
    if not isinstance(payload, dict):
        return False
    selected = payload.get("selected_plugins")
    plugins = payload.get("plugins")
    product_version = payload.get("product_version")
    expected_fields = {
        "schema_version",
        "status",
        "product_version",
        "selected_plugins",
        "plugins",
        "contains_secrets",
    }
    plugin_fields = {"id", "package", "version", "release_id", "site_packages", "capabilities"}
    if (
        set(payload) != expected_fields
        or payload.get("schema_version") != 1
        or payload.get("status") != "complete"
        or payload.get("contains_secrets") is not False
        or not isinstance(product_version, str)
        or not product_version
        or len(product_version) > 128
        or not isinstance(selected, list)
        or not isinstance(plugins, list)
        or any(not isinstance(item, str) for item in selected)
        or len(selected) != len(set(selected))
        or not set(selected).issubset(set(available_plugins(layout)))
        or len(plugins) != len(selected)
        or any(
            not isinstance(item, dict)
            or set(item) != plugin_fields
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("package"), str)
            or not isinstance(item.get("version"), str)
            or not isinstance(item.get("release_id"), str)
            or not isinstance(item.get("site_packages"), str)
            or not isinstance(item.get("capabilities"), list)
            or any(not isinstance(capability, str) for capability in item.get("capabilities", []))
            for item in plugins
        )
        or selected != [item["id"] for item in plugins]
    ):
        return False
    atomic_json(current, _plugin_config(layout, selected))
    return True


def _auth_manager_for_layout(layout: InstallLayout):
    core_root = assert_source_project_safe(layout.clone / "dispatch-core")
    if str(core_root) not in sys.path:
        sys.path.insert(0, str(core_root))
    from authentication import AuthenticationManager
    from paths import DispatchPaths

    environment = {
        **os.environ,
        "DISPATCH_HOME": str(layout.dispatch_home),
        "DISPATCH_CODE_ROOT": str(layout.clone),
        "DISPATCH_CONFIG_ROOT": str(layout.config),
        "DISPATCH_SECRETS_ROOT": str(layout.secrets),
        "DISPATCH_DATA_ROOT": str(layout.data),
        "DISPATCH_STATE_ROOT": str(layout.state),
        "DISPATCH_CACHE_ROOT": str(layout.cache),
        "DISPATCH_LOGS_ROOT": str(layout.logs),
        "DISPATCH_RUNTIME_ROOT": str(layout.run),
    }
    return AuthenticationManager(DispatchPaths.from_environment(environment, code_root=layout.clone))


def _setup_auth_profiles(layout: InstallLayout, selected: Sequence[str], *, human: bool) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    config = load_plugin_config(layout)
    requirements: list[tuple[str, str]] = []
    configured_plugins = config.get("plugins", [])
    if not isinstance(configured_plugins, list):
        return [], [{"plugin": "unknown", "provider": "unknown", "action": "run dispatch setup again"}]
    for plugin in configured_plugins:
        if not isinstance(plugin, dict) or plugin.get("id") not in selected:
            continue
        required = plugin.get("required_profiles", [])
        if isinstance(required, list):
            for item in required:
                if isinstance(item, dict) and isinstance(item.get("provider"), str):
                    requirements.append((str(plugin["id"]), str(item["provider"])))
    try:
        authentication = _auth_manager_for_layout(layout)
        authentication.retain_plugin_bindings(set(selected))
    except (InstallerError, OSError, ValueError, RuntimeError) as exc:
        if isinstance(exc, InstallerError):
            raise
        return [], [
            {
                "plugin": plugin_id,
                "action": "run dispatch setup interactively to create or select a profile",
            }
            for plugin_id, provider in requirements
        ]
    if not requirements:
        return [], []

    configured: list[dict[str, object]] = []
    pending: list[dict[str, str]] = []
    from getpass import getpass

    for plugin_id, provider in requirements:
        if not human:
            try:
                profile = authentication.profile_for_plugin(plugin_id, provider)
                policy = authentication.provider(provider)
                configured.append({"plugin": plugin_id, "profile": profile, "type": policy.public_id, "status": "enrolled"})
            except (InstallerError, OSError, ValueError, RuntimeError):
                pending.append(
                    {
                        "plugin": plugin_id,
                        "action": "run dispatch setup interactively to create or select a profile",
                    }
                )
            continue

        policy = authentication.provider(provider)
        compatible = authentication.compatible_profiles(provider)
        profile: str | None = None
        if compatible:
            print(f"{policy.display_name} profile for {plugin_id}:")
            for index, item in enumerate(compatible, start=1):
                print(f"  {index}. {item['profile']} (reuse)")
            print("  c. create a new profile")
            answer = input("Select a profile: ").strip().lower()
            if answer != "c":
                try:
                    profile = compatible[int(answer) - 1]["profile"]
                except (ValueError, IndexError, KeyError) as exc:
                    raise InstallerError("profile_selection_invalid", "authentication profile selection is invalid") from exc
        if profile is None:
            # Validate at the prompt: Dispatch slugs are lowercase
            # letters/digits/hyphens starting with a letter. Re-ask with a
            # concrete reason instead of failing the whole setup run.
            while True:
                profile = input(f"New profile name for {plugin_id}: ").strip()
                if len(profile) > 63 or _PROFILE_NAME.fullmatch(profile) is None:
                    print(
                        "Profile names use lowercase letters, digits, and hyphens, "
                        "starting with a letter (e.g. amazon-work). Try another name.",
                        file=sys.stderr,
                    )
                    continue
                if any(item.get("profile") == profile for item in authentication.profiles()):
                    print(f"A profile named {profile} already exists; choose another name.", file=sys.stderr)
                    continue
                break
            values = {name: getpass(f"{name}: ") for name in policy.credential_fields}
            try:
                authentication.enroll_profile(profile, provider, values, plugin_id=plugin_id)
            except Exception as exc:
                raise InstallerError(
                    str(getattr(exc, "code", "authentication_profile_failed")),
                    str(exc) or "authentication profile could not be enrolled safely",
                ) from exc
        else:
            try:
                authentication.bind_profile(profile, plugin_id, provider)
            except Exception as exc:
                raise InstallerError(
                    str(getattr(exc, "code", "authentication_profile_failed")),
                    "authentication profile could not be selected safely",
                ) from exc
        configured.append({"plugin": plugin_id, "profile": profile, "type": policy.public_id, "status": "enrolled"})
    return configured, pending


def run_setup(layout: InstallLayout, argv: list[str] | None = None, *, human: bool = True, run: RunCommand = _run) -> int:
    try:
        return _run_setup(layout, argv, human=human, run=run)
    except EOFError as exc:
        raise InstallerError("input_unavailable", "interactive input is unavailable") from exc


def _run_setup(layout: InstallLayout, argv: list[str] | None, *, human: bool, run: RunCommand) -> int:
    parser = argparse.ArgumentParser(prog="dispatch setup")
    parser.add_argument("--plugin", action="append", default=[], help="built-in plugin ID; may be repeated")
    parser.add_argument("--list", action="store_true", help="list built-in plugins")
    parser.add_argument("--yes", action="store_true", help="confirm the selected plugins")
    args = parser.parse_args(argv)
    plugins = available_plugins(layout)
    if args.list:
        payload = {"ok": True, "action": "setup", "status": "available", "plugins": plugins}
        print(json.dumps(payload, sort_keys=True))
        return 0
    selected = list(args.plugin)
    # Wizard surface: pin the stage rail only for interactive selection runs.
    # StageRail self-degrades (returns False / no-ops) on non-TTY, NO_COLOR,
    # dumb TERM, or short terminals, leaving output byte-identical.
    rail = StageRail()
    rail_active = False
    if not args.yes and human and plugins:
        rail_active = rail.begin(("Plugins", "Credentials", "Done"), current=0)
        if rail_active:
            rail.enter("Built-in plugins", "Choose which Dispatch plugins to enable.")
    if not args.yes:
        if not human:
            print(json.dumps({"ok": False, "action": "setup", "status": "error", "error": {"code": "confirmation_required"}}))
            return 1
        if plugins:
            print()
            print(ui.bold("  Built-in plugins"))
            indices = interactive.multi_select_menu(
                "Select plugins to enable",
                [(plugin_id, "") for plugin_id in plugins],
                hint="↑↓ move · space select · enter confirm · empty = Core only",
                interactive=True,
            )
            if indices is None:
                # Arrow keys unavailable (piped/CI/limited SSH): numbered fallback
                print()
                print(ui.bold("  Select plugins to enable"))
                print(ui.dim("  (enter = Core only)"))
                ui.print_numbered_options([(plugin_id, "") for plugin_id in plugins])
                answer = input("  Select plugin numbers separated by commas, or press Enter for Core only: ").strip()
                selected = interactive.parse_plugin_selection(answer, plugins)
            else:
                selected = [plugins[index] for index in indices]
        else:
            print(ui.status_line("warn", "No built-in plugins available; continuing Core-only"))
    try:
        result = configure_plugins(layout, selected, run=run)
        if rail_active:
            rail.advance(1)
            rail.enter("Authentication profiles", "Bind encrypted credential profiles to each plugin.")
        configured, pending = _setup_auth_profiles(layout, selected, human=human)
    except BaseException:
        # Freeze, never wipe: release the region and leave every completed
        # line visible for debugging.
        if rail_active:
            rail.fail()
        raise
    if pending:
        if human:
            print("Plugin setup completed, but authentication profiles are still required:")
            for item in pending:
                print(f"  - {item['plugin']}: {item['action']}")
            if rail_active:
                rail.fail()
            return 1
        print(
            json.dumps(
                {
                    "ok": False,
                    "action": "setup",
                    "status": "pending_requirements",
                    "data": {
                        "setup": result,
                        "setup_committed": True,
                        "configured_profiles": configured,
                        "pending_requirements": pending,
                        "contains_secrets": False,
                    },
                    "error": {
                        "code": "authentication_profiles_required",
                        "message": "selected authenticated plugins need an enrolled profile",
                    },
                },
                sort_keys=True,
            )
        )
        return 1
    if human:
        summary: list[str] = []
        summary.append("")
        summary.append(ui.summary_divider())
        summary.append(ui.status_line("ok", "Dispatch setup complete"))
        if selected:
            summary.append(ui.status_line("ok", "Selected plugins", ", ".join(selected)))
        else:
            summary.append(ui.status_line("ok", "Selected plugins", "Core only"))
        for item in configured:
            summary.append(
                ui.status_line(
                    "warn" if item.get("status") != "enrolled" else "run",
                    f"Authentication profile for {item['plugin']}",
                    f"{item['profile']} (enrolled, not yet verified)",
                )
            )
        summary.append(ui.summary_divider())
        if rail_active:
            rail.advance(len(("Plugins", "Credentials", "Done")) - 1)
            rail.end(summary)
        else:
            for line in summary:
                print(line)
        return 0
    print(json.dumps({"ok": True, "action": "setup", **result, "profiles": configured}, sort_keys=True))
    return 0


__all__ = [
    "available_plugins",
    "configure_plugins",
    "load_plugin_config",
    "migrate_legacy_plugin_config",
    "plugin_dependencies",
    "plugin_metadata",
    "reconcile_plugin_services",
    "selected_long_running_plugins",
    "run_setup",
]
