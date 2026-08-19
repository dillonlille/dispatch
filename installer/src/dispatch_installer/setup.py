"""Explicit built-in plugin setup from the checked-out source tree."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import tomllib
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from .layout import (
    InstallLayout,
    InstallerError,
    assert_user_owned_directory,
    atomic_json,
    read_json,
)
from .service import (
    disable_plugin_service,
    enable_plugin_service,
    inspect_plugin_services,
    plugin_service_ids,
    plugin_service_receipt_path,
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


def _run(command: Sequence[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=cwd, check=False, capture_output=True, text=True)


def _editable_metadata(source: Path) -> set[Path]:
    roots = (source, source / "src")
    return {
        candidate
        for root in roots
        if root.is_dir() and not root.is_symlink()
        for candidate in root.glob("*.egg-info")
    }


def _assert_editable_source_safe(source: Path) -> Path:
    absolute = Path(os.path.abspath(source))
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise InstallerError("editable_source_unsafe", "editable source is missing or unsafe") from exc
    if resolved != absolute or source.is_symlink() or not source.is_dir():
        raise InstallerError("editable_source_unsafe", "editable source aliases are not allowed")
    for candidate in (source, *source.rglob("*")):
        details = candidate.lstat()
        if details.st_uid != os.geteuid() or candidate.is_symlink():
            raise InstallerError("editable_source_unsafe", "editable source entries are unsafe")
        if candidate != source and not (
            stat.S_ISDIR(details.st_mode)
            or (stat.S_ISREG(details.st_mode) and details.st_nlink == 1)
        ):
            raise InstallerError("editable_source_unsafe", "editable source entries are unsafe")
    return absolute


def _plugin_project(source: Path, *, expected_id: str | None = None) -> dict[str, object]:
    """Read the trusted built-in plugin metadata used by installer projections.

    The checkout has already passed the repository authority checks when this is
    called from lifecycle code.  We still validate the source boundary and the
    small metadata subset that is allowed to influence pip or systemd.  In
    particular, dependencies are returned as individual, bounded arguments and
    never as shell text.
    """

    source = _assert_editable_source_safe(source)
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
    service_points = project.get("entry-points", {}).get("dispatch.services", {}) if isinstance(project.get("entry-points"), dict) else {}
    if not isinstance(service_points, dict) or any(
        not isinstance(name, str) or not isinstance(value, str)
        for name, value in service_points.items()
    ):
        raise InstallerError("plugin_manifest_invalid", f"plugin service entry points are invalid: {plugin_id}")
    long_running = "long_running" in capabilities
    if long_running and set(service_points) != {plugin_id}:
        raise InstallerError("plugin_service_missing", f"long-running plugin has no unique service entry point: {plugin_id}")
    if not long_running and service_points:
        raise InstallerError("plugin_service_unexpected", f"non-service plugin publishes a service entry point: {plugin_id}")
    return {
        "id": plugin_id,
        "capabilities": list(capabilities),
        "dependencies": safe_dependencies,
        "long_running": long_running,
        "project": project,
        "configuration": configuration,
    }


def plugin_metadata(source: Path, *, expected_id: str | None = None) -> dict[str, object]:
    """Return validated metadata for a source-owned built-in plugin."""

    return _plugin_project(source, expected_id=expected_id)


def plugin_dependencies(source: Path, *, expected_id: str | None = None) -> tuple[str, ...]:
    """Return trusted built-in runtime dependencies for explicit pip install."""

    metadata = _plugin_project(source, expected_id=expected_id)
    dependencies = metadata["dependencies"]
    assert isinstance(dependencies, list)
    return tuple(str(value) for value in dependencies)


def _site_packages_for_python(python: Path) -> tuple[Path, tuple[int, int]]:
    venv = python.parent.parent
    try:
        venv_resolved = venv.resolve(strict=True)
    except OSError as exc:
        raise InstallerError("editable_site_packages_unsafe", "virtual environment path is unsafe") from exc
    if venv.is_symlink() or venv_resolved != Path(os.path.abspath(venv)):
        raise InstallerError("editable_site_packages_unsafe", "virtual environment path is aliased")
    candidates = sorted((python.parent.parent / "lib").glob("python*/site-packages"))
    if len(candidates) != 1 or candidates[0].is_symlink() or not candidates[0].is_dir():
        raise InstallerError("editable_site_packages_unsafe", "editable install site-packages is unsafe")
    site_packages = candidates[0]
    for ancestor in (venv, site_packages.parent.parent, site_packages.parent, site_packages):
        try:
            details = ancestor.lstat()
            resolved = ancestor.resolve(strict=True)
        except OSError as exc:
            raise InstallerError("editable_site_packages_unsafe", "site-packages ancestors are unsafe") from exc
        if (
            ancestor.is_symlink()
            or not stat.S_ISDIR(details.st_mode)
            or details.st_uid != os.geteuid()
            or stat.S_IMODE(details.st_mode) & 0o022
            or not resolved.is_relative_to(venv.resolve(strict=True))
        ):
            raise InstallerError("editable_site_packages_unsafe", "site-packages ancestors are unsafe")
    site_details = site_packages.lstat()
    return site_packages, (site_details.st_dev, site_details.st_ino)


def _canonical_distribution(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _reject_stale_direct_artifacts(site_packages: Path, name: str, normalized: str) -> None:
    canonical = _canonical_distribution(name)
    variants = {
        name.lower(),
        normalized.lower(),
        canonical,
        canonical.replace("-", "_"),
        canonical.replace("-", "."),
    }
    suspects: set[Path] = set()
    for artifact in site_packages.iterdir():
        lowered = artifact.name.lower()
        if lowered.endswith(".egg-link"):
            suspects.add(artifact)
        if lowered.endswith(".dist-info") and any(
            lowered.startswith(f"{variant}-") for variant in variants
        ):
            suspects.add(artifact)
        if lowered.endswith(".pth") and lowered.startswith("__editable__."):
            suspects.add(artifact)
        if lowered.endswith(".py") and lowered.startswith("__editable___"):
            suspects.add(artifact)
        if lowered.endswith(".dist-info") and artifact.is_symlink():
            suspects.add(artifact)
        elif lowered.endswith(".dist-info") and artifact.is_dir():
            metadata = artifact / "METADATA"
            if metadata.is_file() and not metadata.is_symlink():
                try:
                    lines = metadata.read_text(encoding="utf-8", errors="strict").splitlines()
                except (OSError, UnicodeError):
                    suspects.add(artifact)
                    continue
                for line in lines:
                    if line.startswith("Name: ") and _canonical_distribution(line[6:].strip()) == canonical:
                        suspects.add(artifact)
                        break
    existing = [path for path in suspects if path.exists() or path.is_symlink()]
    if existing:
        raise InstallerError(
            "editable_stale_metadata",
            "legacy or duplicate editable metadata must be removed by replacement-environment repair",
        )


def _direct_generation_owned(path: Path, name: str, source: Path) -> bool:
    if path.is_symlink() or not path.is_dir():
        return False
    for candidate in (path, *path.rglob("*")):
        details = candidate.lstat()
        if (
            details.st_uid != os.geteuid()
            or candidate.is_symlink()
            or stat.S_IMODE(details.st_mode) & 0o022
            or (
                candidate != path
                and not (
                    stat.S_ISDIR(details.st_mode)
                    or (stat.S_ISREG(details.st_mode) and details.st_nlink == 1)
                )
            )
        ):
            return False
    receipt = path / ".dispatch-direct.json"
    try:
        payload = json.loads(receipt.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return payload == {
        "contains_secrets": False,
        "distribution": name,
        "schema_version": 1,
        "source": str(source),
    }


def _open_private_directory(path: Path, expected_identity: tuple[int, int]) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InstallerError("editable_site_packages_unsafe", "private directory could not be pinned") from exc
    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISDIR(opened.st_mode)
            or opened.st_uid != os.geteuid()
            or stat.S_IMODE(opened.st_mode) & 0o022
            or (opened.st_dev, opened.st_ino) != expected_identity
            or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise InstallerError("editable_site_packages_unsafe", "private directory identity changed")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _directory_identity_matches(path: Path, descriptor: int) -> bool:
    try:
        current = path.lstat()
        opened = os.fstat(descriptor)
    except OSError:
        return False
    return (
        not path.is_symlink()
        and stat.S_ISDIR(current.st_mode)
        and (current.st_dev, current.st_ino) == (opened.st_dev, opened.st_ino)
    )


def _atomic_private_bytes_at(directory: int, name: str, payload: bytes) -> None:
    temporary_name = f".{name}.{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory)
        except FileNotFoundError:
            pass


def _read_bytes_at(directory: int, name: str) -> bytes:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) & 0o022
        ):
            raise InstallerError("editable_site_packages_unsafe", "direct-source file is unsafe")
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_child_private_directory(parent: int, name: str) -> int:
    descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    details = os.fstat(descriptor)
    if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o022:
        os.close(descriptor)
        raise InstallerError("editable_site_packages_unsafe", "direct-source generation is unsafe")
    return descriptor


def _remove_tree_at(parent: int, name: str) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    child = os.open(name, flags, dir_fd=parent)
    try:
        for entry in os.listdir(child):
            details = os.stat(entry, dir_fd=child, follow_symlinks=False)
            if stat.S_ISDIR(details.st_mode):
                _remove_tree_at(child, entry)
            else:
                os.unlink(entry, dir_fd=child)
    finally:
        os.close(child)
    os.rmdir(name, dir_fd=parent)


def _remove_tree_at_deferred(parent: int, name: str) -> None:
    interruption: BaseException | None = None
    while True:
        try:
            os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            break
        try:
            _remove_tree_at(parent, name)
        except (KeyboardInterrupt, SystemExit) as exc:
            if interruption is None:
                interruption = exc
            continue
    if interruption is not None:
        raise interruption


def _direct_source_roots(source: Path, project: dict[str, object]) -> list[Path]:
    tool = project.get("tool", {})
    setuptools = tool.get("setuptools", {}) if isinstance(tool, dict) else {}
    if not isinstance(setuptools, dict):
        raise InstallerError("editable_manifest_invalid", "setuptools metadata is invalid")
    package_dir = setuptools.get("package-dir", {})
    roots: set[Path] = set()
    if isinstance(package_dir, dict) and "" in package_dir:
        value = package_dir[""]
        if not isinstance(value, str):
            raise InstallerError("editable_manifest_invalid", "package-dir is invalid")
        roots.add(source / value)
    elif isinstance(package_dir, dict) and package_dir:
        for package, value in package_dir.items():
            if not isinstance(package, str) or not isinstance(value, str) or not package:
                raise InstallerError("editable_manifest_invalid", "package-dir is invalid")
            root = source / value
            for _ in package.split("."):
                root = root.parent
            roots.add(root)
    else:
        find = setuptools.get("packages", {})
        if isinstance(find, dict):
            where = find.get("find", {}).get("where", ["."]) if isinstance(find.get("find"), dict) else ["."]
            if not isinstance(where, list) or any(not isinstance(value, str) for value in where):
                raise InstallerError("editable_manifest_invalid", "package discovery roots are invalid")
            roots.update(source / value for value in where)
        else:
            roots.add(source)
    result = sorted(roots, key=str)
    for root in result:
        if any(character in str(root) for character in ("\x00", "\n", "\r")):
            raise InstallerError("editable_manifest_invalid", "package source root contains unsafe characters")
        try:
            resolved = root.resolve(strict=True)
        except OSError as exc:
            raise InstallerError("editable_manifest_invalid", "package source root is missing") from exc
        if not resolved.is_relative_to(source) or root.is_symlink() or not root.is_dir():
            raise InstallerError("editable_manifest_invalid", "package source root is unsafe")
    return result


def _entry_points(project: dict[str, object]) -> bytes:
    metadata = project.get("project", {})
    if not isinstance(metadata, dict):
        raise InstallerError("editable_manifest_invalid", "project metadata is invalid")
    groups: list[tuple[str, dict[str, str]]] = []
    scripts = metadata.get("scripts", {})
    if not isinstance(scripts, dict):
        raise InstallerError("editable_manifest_invalid", "project scripts are invalid")
    if isinstance(scripts, dict) and scripts:
        groups.append(("console_scripts", scripts))
    declared = metadata.get("entry-points", {})
    if not isinstance(declared, dict) or any(
        not isinstance(group, str) or not isinstance(values, dict)
        for group, values in declared.items()
    ):
        raise InstallerError("editable_manifest_invalid", "project entry-point groups are invalid")
    groups.extend((group, values) for group, values in declared.items())
    lines: list[str] = []
    for group, values in sorted(groups):
        if any(not isinstance(name, str) or not isinstance(value, str) for name, value in values.items()):
            raise InstallerError("editable_manifest_invalid", "project entry points are invalid")
        if any("\n" in value or "\r" in value for value in (group, *values, *values.values())):
            raise InstallerError("editable_manifest_invalid", "project entry points contain unsafe characters")
        lines.append(f"[{group}]")
        lines.extend(f"{name} = {value}" for name, value in sorted(values.items()))
        lines.append("")
    return "\n".join(lines).encode()


def install_editable_source(
    python: Path,
    source: Path,
    *,
    no_deps: bool = False,
    run: RunCommand = _run,
) -> subprocess.CompletedProcess[str]:
    """Register controlled source directly without invoking a build backend."""

    source = _assert_editable_source_safe(source)
    existing = _editable_metadata(source)
    if existing:
        raise InstallerError(
            "editable_metadata_exists",
            "preexisting editable package metadata makes the checkout unsafe",
        )
    manifest = source / "pyproject.toml"
    try:
        configuration = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise InstallerError("editable_manifest_invalid", "project metadata cannot be read") from exc
    project = configuration.get("project", {})
    if not isinstance(project, dict):
        raise InstallerError("editable_manifest_invalid", "project metadata is invalid")
    name = project.get("name")
    version = project.get("version")
    dependencies = project.get("dependencies", [])
    if not isinstance(name, str) or not isinstance(version, str) or not name or not version:
        raise InstallerError("editable_manifest_invalid", "project name or version is invalid")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", version):
        raise InstallerError("editable_manifest_invalid", "project version is unsafe")
    if not isinstance(dependencies, list) or any(not isinstance(value, str) for value in dependencies):
        raise InstallerError("editable_manifest_invalid", "project dependencies are invalid")
    header_values = [*dependencies]
    for key in ("description", "requires-python"):
        value = project.get(key)
        if value is not None and not isinstance(value, str):
            raise InstallerError("editable_manifest_invalid", f"project {key} is invalid")
        if isinstance(value, str):
            header_values.append(value)
    if any("\n" in value or "\r" in value for value in header_values):
        raise InstallerError("editable_manifest_invalid", "project metadata contains unsafe characters")
    if dependencies and not no_deps:
        raise InstallerError("editable_dependencies_unsupported", "direct-source dependencies must be installed explicitly")
    roots = _direct_source_roots(source, configuration)
    site_packages, site_identity = _site_packages_for_python(python)
    normalized = re.sub(r"[-_.]+", "_", name).strip("_")
    if not normalized or not re.fullmatch(r"[A-Za-z0-9_]+", normalized):
        raise InstallerError("editable_manifest_invalid", "project name is unsafe")
    pth = site_packages / f"__dispatch__.{normalized}.pth"
    _reject_stale_direct_artifacts(site_packages, name, normalized)
    old_generations = list(site_packages.glob(f".dispatch-direct-{normalized}-*"))
    if any(not _direct_generation_owned(path, name, source) for path in old_generations):
        raise InstallerError("editable_stale_metadata", "prior direct-source generation is unsafe")

    generation_name = f".dispatch-direct-{normalized}-{uuid.uuid4().hex}"
    generation = site_packages / generation_name
    dist_info_name = f"{normalized}-{version}.dist-info"
    dist_info = generation / dist_info_name
    receipt = {
        "contains_secrets": False,
        "distribution": name,
        "schema_version": 1,
        "source": str(source),
    }
    metadata_lines = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
    ]
    if isinstance(project.get("description"), str):
        metadata_lines.append(f"Summary: {project['description']}")
    if isinstance(project.get("requires-python"), str):
        metadata_lines.append(f"Requires-Python: {project['requires-python']}")
    metadata_lines.extend(f"Requires-Dist: {value}" for value in dependencies)
    pth_payload = "".join(f"{root}\n" for root in roots).encode() + f"{generation_name}\n".encode()
    generation_files = {
        ".dispatch-direct.json": json.dumps(
            receipt, sort_keys=True, separators=(",", ":")
        ).encode()
        + b"\n",
    }
    metadata_files = {
        "METADATA": ("\n".join(metadata_lines) + "\n").encode(),
        "entry_points.txt": _entry_points(configuration),
        "direct_url.json": json.dumps(
            {"dir_info": {"editable": True}, "url": source.as_uri()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        + b"\n",
        "INSTALLER": b"dispatch-direct-source\n",
    }
    record_payloads = {
        generation / name: payload for name, payload in generation_files.items()
    }
    record_payloads.update({dist_info / name: payload for name, payload in metadata_files.items()})
    record_payloads[pth] = pth_payload
    record_rows: list[list[str]] = []
    for path, payload in sorted(record_payloads.items(), key=lambda item: str(item[0])):
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode("ascii")
        record_rows.append([os.path.relpath(path, generation), f"sha256={digest}", str(len(payload))])
    record_rows.append([os.path.relpath(dist_info / "RECORD", generation), "", ""])
    record_buffer = io.StringIO(newline="")
    csv.writer(record_buffer, lineterminator="\n").writerows(record_rows)
    metadata_files["RECORD"] = record_buffer.getvalue().encode()

    site_descriptor = _open_private_directory(site_packages, site_identity)
    generation_descriptor = -1
    metadata_descriptor = -1
    generation_created = False
    previous_pth: bytes | None = None
    committed = False
    try:
        if not _directory_identity_matches(site_packages, site_descriptor):
            raise InstallerError("editable_site_packages_unsafe", "site-packages identity changed")
        try:
            previous_pth = _read_bytes_at(site_descriptor, pth.name)
        except FileNotFoundError:
            previous_pth = None
        except OSError as exc:
            raise InstallerError("editable_site_packages_unsafe", "direct-source path record is unsafe") from exc
        expected_existing = (
            "".join(f"{root}\n" for root in roots).encode() + f"{old_generations[0].name}\n".encode()
            if len(old_generations) == 1
            else None
        )
        if previous_pth is not None and (
            expected_existing is None or previous_pth != expected_existing
        ):
            raise InstallerError("editable_stale_metadata", "direct-source path record is inconsistent")
        if previous_pth is None and old_generations:
            raise InstallerError("editable_stale_metadata", "direct-source generation has no path record")

        try:
            os.mkdir(generation_name, 0o700, dir_fd=site_descriptor)
            generation_created = True
            generation_descriptor = _open_child_private_directory(site_descriptor, generation_name)
            os.mkdir(dist_info_name, 0o700, dir_fd=generation_descriptor)
            metadata_descriptor = _open_child_private_directory(generation_descriptor, dist_info_name)
            for filename, payload in generation_files.items():
                _atomic_private_bytes_at(generation_descriptor, filename, payload)
            for filename, payload in metadata_files.items():
                _atomic_private_bytes_at(metadata_descriptor, filename, payload)
        except BaseException as error:
            if metadata_descriptor >= 0:
                os.close(metadata_descriptor)
                metadata_descriptor = -1
            if generation_descriptor >= 0:
                os.close(generation_descriptor)
                generation_descriptor = -1
            cleanup_error: BaseException | None = None
            if generation_created:
                try:
                    _remove_tree_at_deferred(site_descriptor, generation_name)
                    generation_created = False
                except BaseException as exc:
                    cleanup_error = exc
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if cleanup_error is not None:
                raise InstallerError(
                    "editable_metadata_cleanup_failed",
                    "incomplete direct-source metadata could not be removed",
                ) from cleanup_error
            if isinstance(error, OSError):
                raise InstallerError(
                    "editable_metadata_write_failed",
                    "direct-source metadata could not be written",
                ) from error
            raise
        else:
            os.close(metadata_descriptor)
            metadata_descriptor = -1
            os.close(generation_descriptor)
            generation_descriptor = -1

        try:
            _atomic_private_bytes_at(site_descriptor, pth.name, pth_payload)
            committed = _read_bytes_at(site_descriptor, pth.name) == pth_payload
            if not committed:
                raise InstallerError("editable_metadata_write_failed", "direct-source publication did not commit")
        except BaseException as error:
            try:
                committed = _read_bytes_at(site_descriptor, pth.name) == pth_payload
            except (FileNotFoundError, OSError):
                committed = False
            cleanup_error: BaseException | None = None
            if not committed and generation_created:
                try:
                    _remove_tree_at_deferred(site_descriptor, generation_name)
                    generation_created = False
                except BaseException as exc:
                    cleanup_error = exc
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                raise
            if cleanup_error is not None:
                raise InstallerError(
                    "editable_metadata_cleanup_failed",
                    "incomplete direct-source metadata could not be removed",
                ) from cleanup_error
            if isinstance(error, OSError):
                raise InstallerError(
                    "editable_metadata_write_failed",
                    "direct-source metadata could not be written",
                ) from error
            raise

        if not _directory_identity_matches(site_packages, site_descriptor):
            if previous_pth is None:
                os.unlink(pth.name, dir_fd=site_descriptor)
            else:
                _atomic_private_bytes_at(site_descriptor, pth.name, previous_pth)
            _remove_tree_at_deferred(site_descriptor, generation_name)
            generation_created = False
            raise InstallerError("editable_site_packages_unsafe", "site-packages changed during publication")

        for old_generation in old_generations:
            try:
                _remove_tree_at_deferred(site_descriptor, old_generation.name)
            except OSError as exc:
                raise InstallerError(
                    "editable_metadata_cleanup_failed",
                    "prior direct-source metadata could not be removed",
                ) from exc
        return subprocess.CompletedProcess(("dispatch-direct-source", str(source)), 0, "", "")
    finally:
        if metadata_descriptor >= 0:
            os.close(metadata_descriptor)
        if generation_descriptor >= 0:
            os.close(generation_descriptor)
        os.close(site_descriptor)


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
        project = tomllib.loads((catalog[plugin_id] / "pyproject.toml").read_text(encoding="utf-8"))
        metadata = project.get("tool", {}).get("dispatch", {})
        capabilities = metadata.get("capabilities") if isinstance(metadata, dict) else None
        if not isinstance(capabilities, list) or any(not isinstance(value, str) for value in capabilities):
            raise InstallerError("plugin_manifest_invalid", f"plugin capabilities are invalid: {plugin_id}")
        plugins.append(
            {
                "id": plugin_id,
                "source": str(catalog[plugin_id]),
                "site_packages": str(site_packages),
                "capabilities": capabilities,
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
    inspected = inspect_plugin_services(layout, existing, run=run)
    if inspected.get("status") != "ready":
        raise InstallerError(
            "plugin_service_unsafe",
            "plugin service projections are incomplete or not receipt-owned",
        )
    services_value = inspected.get("services", {})
    services = services_value if isinstance(services_value, dict) else {}
    previously_enabled = {
        plugin_id
        for plugin_id, value in services.items()
        if isinstance(value, dict)
        and (value.get("receipt_status") == "enabled" or value.get("enabled") is True)
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
    if any(dependencies.values()) and not replacement_ready:
        raise InstallerError(
            "plugin_environment_unavailable",
            "plugin dependencies require a complete replacement environment",
        )

    previous_config = load_plugin_config(layout) if (layout.config / "plugins.json").exists() else None
    previous_selected = previous_config.get("selected_plugins", []) if previous_config else []
    if not isinstance(previous_selected, list) or any(not isinstance(value, str) for value in previous_selected):
        raise InstallerError("plugin_config_invalid", "plugin configuration selection is invalid")

    # A dependency-bearing setup, deselection, or any fully installed clone is
    # rebuilt as one replacement venv. The compatibility fallback retains the
    # old direct-source behavior for lightweight unit fixtures that do not have
    # Core's requirements and installer source available.
    use_replacement = replacement_ready
    work: Path | None = None
    backup: Path | None = None
    stopped_plugin_services: list[dict[str, object]] = []
    main_service_stopped = False
    config: dict[str, object] | None = None
    try:
        if use_replacement:
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
                stopped = run(("systemctl", "--user", "stop", "dispatch.service"), None)
                if stopped.returncode != 0:
                    raise InstallerError(
                        "service_stop_failed",
                        "Dispatch service could not be stopped before plugin environment activation",
                    )
                main_service_stopped = True
            backup = _swap_directory(replacement, layout.venv)
        else:
            for plugin_id in selected_ids:
                source = catalog[plugin_id]
                plugin_deps = dependencies.get(plugin_id, ())
                if plugin_deps:
                    completed = run(
                        (str(layout.venv_python), "-m", "pip", "install", "--disable-pip-version-check", *plugin_deps),
                        None,
                    )
                    if completed.returncode != 0:
                        raise InstallerError("plugin_dependencies_failed", f"could not install dependencies for selected plugin: {plugin_id}")
                completed = install_editable_source(layout.venv_python, source, no_deps=True, run=run)
                if completed.returncode != 0:
                    raise InstallerError("plugin_install_failed", f"could not install built-in plugin: {plugin_id}")

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
    except BaseException:
        if use_replacement and backup is not None:
            try:
                from .lifecycle import _restore_directory

                _restore_directory(layout.venv, backup)
            except BaseException:
                raise InstallerError("plugin_environment_rollback_failed", "plugin environment rollback failed")
        if previous_config is None:
            (layout.config / "plugins.json").unlink(missing_ok=True)
        else:
            atomic_json(layout.config / "plugins.json", previous_config)
        if work is not None:
            try:
                from .lifecycle import _safe_remove

                _safe_remove(work)
            except BaseException:
                pass
        if previous_selected != selected_ids:
            try:
                reconcile_plugin_services(layout, previous_selected, run=run)
            except BaseException:
                pass
        try:
            restore_plugin_service_states(layout, stopped_plugin_services, run=run)
        except BaseException:
            raise InstallerError(
                "plugin_environment_rollback_failed",
                "plugin setup failed and a prior plugin service could not be restored",
            )
        if service_present and (backup is not None or main_service_stopped):
            try:
                restore_systemd_service_state(
                    "dispatch.service",
                    main_service_state,
                    run=run,
                )
            except InstallerError:
                raise InstallerError(
                    "plugin_environment_rollback_failed",
                    "plugin setup failed and the prior Dispatch service could not be restored",
                )
        raise
    else:
        if backup is not None:
            try:
                from .lifecycle import _safe_remove

                _safe_remove(backup)
            except BaseException:
                pass
        if work is not None:
            try:
                from .lifecycle import _safe_remove

                _safe_remove(work)
            except BaseException:
                pass

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
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("contains_secrets") is not False
        or payload.get("status") != "complete"
        or not isinstance(payload.get("selected_plugins"), list)
        or not isinstance(payload.get("plugins"), list)
    ):
        raise InstallerError("plugin_config_invalid", "plugin configuration is invalid")
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


def run_setup(layout: InstallLayout, argv: list[str] | None = None, *, human: bool = True, run: RunCommand = _run) -> int:
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
    if not args.yes:
        if not human:
            print(json.dumps({"ok": False, "action": "setup", "status": "error", "error": {"code": "confirmation_required"}}))
            return 1
        print("Available built-in plugins:")
        for index, plugin_id in enumerate(plugins, start=1):
            print(f"  {index}. {plugin_id}")
        answer = input("Select plugin numbers separated by commas, or press Enter for Core only: ").strip()
        if answer:
            try:
                indexes = [int(value.strip()) for value in answer.split(",")]
                if any(index < 1 or index > len(plugins) for index in indexes):
                    raise ValueError
                selected = [plugins[index - 1] for index in indexes]
            except ValueError as exc:
                raise InstallerError("plugin_selection_invalid", "plugin selection is invalid") from exc
    result = configure_plugins(layout, selected, run=run)
    print(json.dumps({"ok": True, "action": "setup", **result}, sort_keys=True))
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
