from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Iterable

from .core_release import _safe_members, _verify_record, sha256_file
from .download import download_release_artifact
from .layout import InstallLayout, InstallerError, atomic_json
from .manifest import BuiltinPlugin, InstallationManifest, load_manifest


_MAX_PLUGIN_WHEEL_FILES = 512
_MAX_PLUGIN_WHEEL_MEMBER_BYTES = 64 * 1024 * 1024
_APACHE_2_LICENSE_SHA256 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_bytes(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def persist_release_manifest(
    layout: InstallLayout,
    source: Path,
    *,
    expected_sha256: str,
    product_version: str,
) -> Path:
    content = source.read_bytes()
    observed = hashlib.sha256(content).hexdigest()
    if observed != expected_sha256:
        raise InstallerError("manifest_digest_mismatch", "installation manifest SHA-256 differs")
    destination = layout.state / "install" / "release-manifest.json"
    _atomic_bytes(destination, content)
    atomic_json(
        layout.state / "install" / "release.json",
        {
            "schema_version": 1,
            "product_version": product_version,
            "manifest": str(destination),
            "manifest_sha256": observed,
            "contains_secrets": False,
        },
        mode=0o600,
    )
    return destination


def load_installed_manifest(layout: InstallLayout) -> InstallationManifest:
    receipt_path = layout.state / "install" / "release.json"
    if receipt_path.is_symlink() or not receipt_path.is_file() or receipt_path.stat().st_size > 8192:
        raise InstallerError("setup_release_receipt_missing", "installed release receipt is missing or unsafe")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError("setup_release_receipt_invalid", "installed release receipt is invalid") from exc
    manifest_path = layout.state / "install" / "release-manifest.json"
    if (
        set(receipt) != {
            "schema_version",
            "product_version",
            "manifest",
            "manifest_sha256",
            "contains_secrets",
        }
        or receipt.get("schema_version") != 1
        or receipt.get("manifest") != str(manifest_path)
        or receipt.get("contains_secrets") is not False
        or not isinstance(receipt.get("manifest_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", receipt["manifest_sha256"]) is None
    ):
        raise InstallerError("setup_release_receipt_invalid", "installed release receipt is invalid")
    manifest = load_manifest(manifest_path, expected_sha256=receipt["manifest_sha256"])
    if not manifest.ready or manifest.product_version != receipt.get("product_version"):
        raise InstallerError("setup_release_authority_invalid", "installed release authority is not ready")
    return manifest


def active_plugins(layout: InstallLayout) -> list[tuple[str, Path]]:
    setup_path = layout.state / "install" / "setup.json"
    if not setup_path.exists():
        return []
    if setup_path.is_symlink() or not setup_path.is_file() or setup_path.stat().st_size > 64 * 1024:
        raise InstallerError("setup_receipt_unsafe", "setup receipt is unsafe")
    try:
        receipt = json.loads(setup_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError("setup_receipt_invalid", "setup receipt is invalid") from exc
    if (
        set(receipt) != {
            "schema_version",
            "status",
            "product_version",
            "selected_plugins",
            "plugins",
            "contains_secrets",
        }
        or receipt.get("schema_version") != 1
        or receipt.get("status") != "complete"
        or receipt.get("contains_secrets") is not False
        or not isinstance(receipt.get("selected_plugins"), list)
        or not isinstance(receipt.get("plugins"), list)
    ):
        raise InstallerError("setup_receipt_invalid", "setup receipt is invalid")
    manifest = load_installed_manifest(layout)
    if receipt.get("product_version") != manifest.product_version:
        raise InstallerError("setup_receipt_invalid", "setup product version differs from release authority")
    catalog = {plugin.id: plugin for plugin in manifest.builtin_plugins}
    plugins: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for plugin in receipt["plugins"]:
        if not isinstance(plugin, dict) or set(plugin) != {
            "id",
            "package",
            "version",
            "release_id",
            "site_packages",
            "capabilities",
        }:
            raise InstallerError("setup_receipt_invalid", "setup plugin receipt is invalid")
        plugin_id = plugin.get("id")
        release_id = plugin.get("release_id")
        if (
            not isinstance(plugin_id, str)
            or plugin_id in seen
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", plugin_id) is None
            or not isinstance(release_id, str)
        ):
            raise InstallerError("setup_receipt_invalid", "setup plugin identity is invalid")
        expected = layout.plugins / plugin_id / "releases" / release_id / "site-packages"
        authority = catalog.get(plugin_id)
        if (
            authority is None
            or plugin.get("package") != authority.package
            or plugin.get("version") != authority.version
            or plugin.get("capabilities") != list(authority.capabilities)
            or plugin.get("site_packages") != str(expected)
            or expected.is_symlink()
            or not expected.is_dir()
        ):
            raise InstallerError("setup_plugin_release_invalid", "active plugin release is missing or unsafe")
        selector = layout.plugins / plugin_id / "active.json"
        try:
            selector_payload = json.loads(selector.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise InstallerError("setup_plugin_selector_invalid", "active plugin selector is invalid") from exc
        if (
            selector.is_symlink()
            or selector_payload.get("active") != release_id
            or selector_payload.get("artifact_sha256") != authority.artifact.sha256
        ):
            raise InstallerError("setup_plugin_selector_invalid", "active plugin selector differs from setup receipt")
        _verify_plugin_release(expected.parent, authority)
        seen.add(plugin_id)
        plugins.append((plugin_id, expected))
    if receipt["selected_plugins"] != [plugin["id"] for plugin in receipt["plugins"]]:
        raise InstallerError("setup_receipt_invalid", "setup plugin selection differs from installed plugins")
    return plugins


def active_plugin_paths(layout: InstallLayout) -> list[Path]:
    return [path for _plugin_id, path in active_plugins(layout)]


def _plugin_wheel_identity(wheel: Path, plugin: BuiltinPlugin, core_version: str) -> dict[str, zipfile.ZipInfo]:
    try:
        archive = zipfile.ZipFile(wheel)
    except (OSError, zipfile.BadZipFile) as exc:
        raise InstallerError("plugin_wheel_invalid", "built-in plugin artifact is not a valid wheel") from exc
    with archive:
        members = _safe_members(archive)
        metadata_names = [name for name in members if name.endswith(".dist-info/METADATA")]
        record_names = [name for name in members if name.endswith(".dist-info/RECORD")]
        wheel_names = [name for name in members if name.endswith(".dist-info/WHEEL")]
        top_level_names = [name for name in members if name.endswith(".dist-info/top_level.txt")]
        entry_point_names = [name for name in members if name.endswith(".dist-info/entry_points.txt")]
        license_names = [name for name in members if name.endswith(".dist-info/licenses/LICENSE")]
        if not all(
            len(names) == 1
            for names in (
                metadata_names,
                record_names,
                wheel_names,
                top_level_names,
                entry_point_names,
                license_names,
            )
        ):
            raise InstallerError("plugin_wheel_metadata", "built-in plugin wheel metadata set is invalid")
        metadata_root = metadata_names[0].removesuffix("METADATA")
        if any(
            not name.startswith(metadata_root)
            for name in (
                record_names[0],
                wheel_names[0],
                top_level_names[0],
                entry_point_names[0],
                license_names[0],
            )
        ):
            raise InstallerError("plugin_wheel_metadata", "built-in plugin metadata roots differ")
        metadata = BytesParser().parsebytes(archive.read(members[metadata_names[0]]))
        name = metadata.get("Name", "").lower().replace("_", "-")
        version = metadata.get("Version", "")
        expected_root = f"{plugin.package.replace('-', '_')}-{plugin.version}.dist-info/"
        if name != plugin.package or version != plugin.version or metadata_root != expected_root:
            raise InstallerError("plugin_wheel_identity", "built-in plugin wheel identity differs from release authority")
        if metadata.get("License-Expression") != "Apache-2.0" or hashlib.sha256(
            archive.read(members[license_names[0]])
        ).hexdigest() != _APACHE_2_LICENSE_SHA256:
            raise InstallerError("plugin_wheel_license", "built-in plugin wheel license differs from Apache-2.0 policy")
        if metadata.get("Requires-Python") != "<3.14,>=3.11":
            raise InstallerError("plugin_wheel_python", "built-in plugin Python requirement differs from policy")
        if (
            metadata.get_all("Requires-Dist", []) != list(plugin.requires_dist)
            or f"dispatch-core=={core_version}" not in plugin.requires_dist
        ):
            raise InstallerError("plugin_wheel_dependencies", "built-in plugin dependency closure differs from policy")
        top_levels = archive.read(members[top_level_names[0]]).decode("utf-8").splitlines()
        if len(top_levels) != 1 or re.fullmatch(r"dispatch_[a-z0-9_]+", top_levels[0]) is None:
            raise InstallerError("plugin_wheel_top_level", "built-in plugin top-level package is invalid")
        package_root = f"{top_levels[0]}/"
        entry_points = configparser.ConfigParser(interpolation=None, strict=True)
        try:
            entry_points.read_string(archive.read(members[entry_point_names[0]]).decode("utf-8"))
        except (configparser.Error, UnicodeError) as exc:
            raise InstallerError("plugin_wheel_entry_point", "built-in plugin entry point metadata is invalid") from exc
        if not entry_points.has_section("dispatch.plugins") or set(entry_points["dispatch.plugins"]) != {plugin.id}:
            raise InstallerError(
                "plugin_wheel_entry_point",
                "built-in plugin must publish one dispatch.plugins entry point matching its id",
            )
        target = entry_points["dispatch.plugins"][plugin.id].strip()
        if re.fullmatch(
            rf"{re.escape(top_levels[0])}(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*:[a-zA-Z_][a-zA-Z0-9_]*",
            target,
        ) is None:
            raise InstallerError("plugin_wheel_entry_point", "built-in plugin entry point target is invalid")
        if any(
            name.endswith(".pth")
            or ".data/" in name
            or (not name.startswith(package_root) and not name.startswith(metadata_root))
            for name in members
        ):
            raise InstallerError("plugin_wheel_scope", "built-in plugin wheel contains unapproved members")
        wheel_metadata = BytesParser().parsebytes(archive.read(members[wheel_names[0]]))
        if wheel_metadata.get("Root-Is-Purelib", "").lower() != "true" or wheel_metadata.get_all("Tag", []) != [
            "py3-none-any"
        ]:
            raise InstallerError("plugin_wheel_tag", "built-in plugin wheel platform tag differs from policy")
        _verify_record(archive, members, record_names[0])
        return members


def _extract_plugin_wheel(wheel: Path, site_packages: Path, members: Iterable[str]) -> None:
    site_packages.mkdir(mode=0o700)
    with zipfile.ZipFile(wheel) as archive:
        for name in sorted(members):
            relative = PurePosixPath(name)
            destination = site_packages.joinpath(*relative.parts)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
            try:
                with archive.open(name) as source:
                    while chunk := source.read(64 * 1024):
                        remaining = memoryview(chunk)
                        while remaining:
                            written = os.write(descriptor, remaining)
                            if written <= 0:
                                raise InstallerError("plugin_extract_write", "built-in plugin extraction write failed")
                            remaining = remaining[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    for directory in sorted((path for path in site_packages.rglob("*") if path.is_dir()), reverse=True):
        directory.chmod(0o555)
    site_packages.chmod(0o555)


def _plugin_tree(site_packages: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    count = 0
    total_size = 0
    for path in site_packages.rglob("*"):
        if path.is_symlink():
            raise InstallerError("plugin_release_symlink", "built-in plugin release contains a symlink")
        if path.is_file():
            size = path.stat().st_size
            count += 1
            total_size += size
            if (
                count > _MAX_PLUGIN_WHEEL_FILES
                or size > _MAX_PLUGIN_WHEEL_MEMBER_BYTES
                or total_size > 128 * 1024 * 1024
            ):
                raise InstallerError("plugin_release_bounds", "built-in plugin release exceeds verification bounds")
            entries.append(
                {
                    "path": path.relative_to(site_packages).as_posix(),
                    "size": size,
                    "sha256": sha256_file(path),
                    "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}",
                }
            )
    return sorted(entries, key=lambda item: str(item["path"]))


def _verify_plugin_release(release: Path, plugin: BuiltinPlugin) -> Path:
    if release.is_symlink() or not release.is_dir() or stat.S_IMODE(release.stat().st_mode) != 0o555:
        raise InstallerError("plugin_release_invalid", "built-in plugin release root is unsafe")
    receipt_path = release / "release-receipt.json"
    tree_path = release / "tree-manifest.json"
    site_packages = release / "site-packages"
    for path in (receipt_path, tree_path):
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size > 1024 * 1024
            or stat.S_IMODE(path.stat().st_mode) != 0o444
        ):
            raise InstallerError("plugin_release_invalid", "built-in plugin release metadata is unsafe")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        tree = json.loads(tree_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InstallerError("plugin_release_invalid", "built-in plugin release metadata is invalid") from exc
    if (
        set(receipt) != {
            "schema_version",
            "id",
            "distribution",
            "version",
            "artifact",
            "tree_sha256",
            "contains_secrets",
        }
        or receipt.get("schema_version") != 1
        or receipt.get("id") != plugin.id
        or receipt.get("distribution") != plugin.package
        or receipt.get("version") != plugin.version
        or receipt.get("artifact") != {"sha256": plugin.artifact.sha256, "size": plugin.artifact.size}
        or receipt.get("tree_sha256") != sha256_file(tree_path)
        or receipt.get("contains_secrets") is not False
        or not isinstance(tree, dict)
        or set(tree) != {"schema_version", "files"}
        or tree.get("schema_version") != 1
        or tree.get("files") != _plugin_tree(site_packages)
    ):
        raise InstallerError("plugin_release_invalid", "built-in plugin release differs from its receipt")
    return site_packages


def install_plugin_wheel(
    layout: InstallLayout,
    plugin: BuiltinPlugin,
    wheel: Path,
    *,
    core_version: str,
) -> dict[str, object]:
    artifact = plugin.artifact
    if artifact.sha256 is None or artifact.size is None:
        raise InstallerError("plugin_artifact_incomplete", "built-in plugin artifact identity is incomplete")
    if wheel.stat().st_size != artifact.size or sha256_file(wheel) != artifact.sha256:
        raise InstallerError("plugin_artifact_mismatch", "built-in plugin artifact differs from release authority")
    members = _plugin_wheel_identity(wheel, plugin, core_version)
    owner = layout.plugins / plugin.id
    releases = owner / "releases"
    for path in (owner, releases):
        if path.exists() and (path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.geteuid()):
            raise InstallerError("plugin_release_root_unsafe", "built-in plugin release root is unsafe")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    release_id = f"{plugin.package}-{plugin.version}-{artifact.sha256[:16]}"
    release = releases / release_id
    if release.exists():
        _verify_plugin_release(release, plugin)
    else:
        stage = Path(tempfile.mkdtemp(prefix=f".plugin-{plugin.id}-", dir=layout.staging))
        try:
            site_packages = stage / "site-packages"
            _extract_plugin_wheel(wheel, site_packages, members)
            tree_path = stage / "tree-manifest.json"
            atomic_json(tree_path, {"schema_version": 1, "files": _plugin_tree(site_packages)}, mode=0o444)
            atomic_json(
                stage / "release-receipt.json",
                {
                    "schema_version": 1,
                    "id": plugin.id,
                    "distribution": plugin.package,
                    "version": plugin.version,
                    "artifact": {"sha256": artifact.sha256, "size": artifact.size},
                    "tree_sha256": sha256_file(tree_path),
                    "contains_secrets": False,
                },
                mode=0o444,
            )
            os.replace(stage, release)
            _fsync_directory(releases)
            for directory in sorted((path for path in release.rglob("*") if path.is_dir()), reverse=True):
                directory.chmod(0o555)
            release.chmod(0o555)
            _verify_plugin_release(release, plugin)
        finally:
            if stage.exists():
                for path in stage.rglob("*"):
                    if path.is_dir():
                        path.chmod(0o700)
                    elif path.is_file():
                        path.chmod(0o600)
                stage.chmod(0o700)
                shutil.rmtree(stage)
    selector_path = owner / "active.json"
    rollback = None
    if selector_path.exists() and not selector_path.is_symlink():
        try:
            prior = json.loads(selector_path.read_text(encoding="utf-8"))
            rollback = prior.get("active") if prior.get("active") != release_id else prior.get("rollback")
        except (OSError, json.JSONDecodeError):
            rollback = None
    atomic_json(
        selector_path,
        {
            "schema_version": 1,
            "id": plugin.id,
            "active": release_id,
            "rollback": rollback,
            "artifact_sha256": artifact.sha256,
            "contains_secrets": False,
        },
        mode=0o600,
    )
    return {
        "id": plugin.id,
        "package": plugin.package,
        "version": plugin.version,
        "release_id": release_id,
        "site_packages": str(release / "site-packages"),
        "capabilities": list(plugin.capabilities),
    }


def configure_plugins(layout: InstallLayout, selected: Iterable[str]) -> dict[str, object]:
    manifest = load_installed_manifest(layout)
    selected_ids = list(selected)
    if len(selected_ids) != len(set(selected_ids)):
        raise InstallerError("setup_plugin_duplicate", "built-in plugin selection contains duplicates")
    catalog = {plugin.id: plugin for plugin in manifest.builtin_plugins}
    unknown = sorted(set(selected_ids) - set(catalog))
    if unknown:
        raise InstallerError("setup_plugin_unknown", f"unknown built-in plugin: {unknown[0]}")
    unsupported = sorted({capability for plugin_id in selected_ids for capability in catalog[plugin_id].capabilities})
    if unsupported:
        raise InstallerError("setup_capability_unavailable", f"capability provisioning is not ready: {unsupported[0]}")
    downloads = layout.staging / "setup-downloads"
    downloads.mkdir(mode=0o700, exist_ok=True)
    installed: list[dict[str, object]] = []
    for plugin_id in selected_ids:
        plugin = catalog[plugin_id]
        artifact = plugin.artifact
        if artifact.url is None or artifact.size is None or artifact.sha256 is None:
            raise InstallerError("plugin_artifact_incomplete", "built-in plugin artifact identity is incomplete")
        wheel = downloads / artifact.url.rsplit("/", 1)[-1]
        download_release_artifact(
            artifact.url,
            wheel,
            expected_size=artifact.size,
            expected_sha256=artifact.sha256,
        )
        installed.append(install_plugin_wheel(layout, plugin, wheel, core_version=manifest.core_version))
    setup_path = layout.state / "install" / "setup.json"
    atomic_json(
        setup_path,
        {
            "schema_version": 1,
            "status": "configured",
            "product_version": manifest.product_version,
            "selected_plugins": selected_ids,
            "plugins": installed,
            "contains_secrets": False,
        },
        mode=0o600,
    )
    completed = subprocess.run(
        ("systemctl", "--user", "restart", "dispatch-core.service"),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise InstallerError("setup_service_restart", "Dispatch Core could not be restarted after setup")
    receipt = json.loads(setup_path.read_text(encoding="utf-8"))
    receipt["status"] = "complete"
    atomic_json(setup_path, receipt, mode=0o600)
    return {"status": "complete", "selected_plugins": selected_ids, "plugins": installed}


def run_setup(layout: InstallLayout, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="dispatch setup")
    parser.add_argument("--plugin", action="append", default=[], help="built-in plugin ID; may be repeated")
    parser.add_argument("--yes", action="store_true", help="accept the explicit plugin selection")
    parser.add_argument("--list", action="store_true", help="list available built-in plugins without changing setup")
    args = parser.parse_args(argv)
    manifest = load_installed_manifest(layout)
    catalog = list(manifest.builtin_plugins)
    if args.list:
        print(json.dumps({"ok": True, "action": "setup", "status": "available", "plugins": [plugin.id for plugin in catalog]}, sort_keys=True))
        return 0
    selected = list(args.plugin)
    if not args.yes:
        print("Available built-in plugins:")
        for index, plugin in enumerate(catalog, start=1):
            print(f"  {index}. {plugin.id}")
        response = input("Select plugin numbers separated by commas, or press Enter for Core only: ").strip()
        if response:
            try:
                indexes = [int(value.strip()) for value in response.split(",")]
                if any(index < 1 or index > len(catalog) for index in indexes):
                    raise ValueError
            except ValueError as exc:
                raise InstallerError("setup_selection_invalid", "plugin selection is invalid") from exc
            selected = [catalog[index - 1].id for index in indexes]
    result = configure_plugins(layout, selected)
    print(json.dumps({"ok": True, "action": "setup", **result}, sort_keys=True))
    return 0
