from __future__ import annotations

import base64
import configparser
import csv
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Collection, Mapping
from email.parser import BytesParser
from pathlib import Path, PurePosixPath

from .layout import InstallLayout, InstallerError, atomic_json, installation_lock

_RELEASE_RE = re.compile(r"^dispatch-core-[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{16}$")
_APACHE_2_LICENSE_SHA256 = "c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fsync_tree(root: Path) -> None:
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        flags = os.O_RDONLY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    directories = sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True)
    directories.append(root)
    for path in directories:
        flags = os.O_RDONLY | os.O_DIRECTORY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | os.O_DIRECTORY | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_release(path: Path) -> None:
    for directory in sorted((item for item in path.rglob("*") if item.is_dir()), reverse=True):
        directory.chmod(0o700)
    path.chmod(0o700)
    shutil.rmtree(path)


def _clean_stale_core_staging(layout: InstallLayout) -> None:
    for path in sorted(layout.staging.glob(".core-*")):
        if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.geteuid():
            raise InstallerError("staging_unsafe", f"unsafe stale Core staging path: {path}")
        members = list(path.rglob("*"))
        if any(member.is_symlink() or member.stat().st_uid != os.geteuid() for member in members):
            raise InstallerError("staging_unsafe", f"unsafe stale Core staging tree: {path}")
        _remove_release(path)


def _safe_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members: dict[str, zipfile.ZipInfo] = {}
    expanded_size = 0
    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        if info.is_dir():
            continue
        if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
            raise InstallerError("wheel_path_unsafe", f"unsafe wheel member: {info.filename}")
        name = path.as_posix()
        if name in members:
            raise InstallerError("wheel_member_duplicate", f"duplicate wheel member: {name}")
        file_type = (info.external_attr >> 16) & 0o170000
        if file_type not in {0, stat.S_IFREG}:
            raise InstallerError("wheel_member_type", f"non-regular wheel member: {name}")
        expanded_size += info.file_size
        if info.file_size > 16 * 1024 * 1024 or expanded_size > 64 * 1024 * 1024:
            raise InstallerError("wheel_expanded_size", "Core wheel expanded size exceeds policy")
        members[name] = info
    return members


def _verify_record(archive: zipfile.ZipFile, members: dict[str, zipfile.ZipInfo], record_name: str) -> None:
    rows = list(csv.reader(io.TextIOWrapper(archive.open(members[record_name]), encoding="utf-8", newline="")))
    declared: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3 or row[0] in declared:
            raise InstallerError("wheel_record_invalid", "invalid or duplicate RECORD row")
        declared[row[0]] = (row[1], row[2])
    if set(declared) != set(members):
        raise InstallerError("wheel_record_members", "wheel RECORD member set mismatch")
    for name, info in members.items():
        hash_value, size_value = declared[name]
        if name == record_name:
            if hash_value or size_value:
                raise InstallerError("wheel_record_self", "RECORD self-entry must be unhashed")
            continue
        if not hash_value.startswith("sha256=") or not size_value.isdigit():
            raise InstallerError("wheel_record_hash", f"missing SHA-256 for {name}")
        data = archive.read(info)
        observed = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
        if hash_value != f"sha256={observed}" or int(size_value) != len(data):
            raise InstallerError("wheel_record_mismatch", f"wheel RECORD mismatch: {name}")


def _wheel_identity(
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
    expected_package_files: Mapping[str, str],
    expected_requires_dist: Collection[str],
) -> tuple[str, str]:
    metadata_names = [name for name in members if name.endswith(".dist-info/METADATA")]
    record_names = [name for name in members if name.endswith(".dist-info/RECORD")]
    if len(metadata_names) != 1 or len(record_names) != 1:
        raise InstallerError("wheel_metadata_set", "wheel must contain one METADATA and one RECORD")
    metadata_root = metadata_names[0].removesuffix("METADATA")
    if record_names[0] != f"{metadata_root}RECORD":
        raise InstallerError("wheel_metadata_root", "wheel metadata roots differ")
    metadata = BytesParser().parsebytes(archive.read(members[metadata_names[0]]))
    name = metadata.get("Name", "").lower().replace("_", "-")
    version = metadata.get("Version", "")
    if name != "dispatch-core" or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise InstallerError("wheel_identity", "artifact is not an expected dispatch-core wheel")
    expected_root = f"dispatch_core-{version}.dist-info/"
    if metadata_root != expected_root:
        raise InstallerError("wheel_metadata_root", "wheel metadata root does not match package identity")
    wheel_name = f"{metadata_root}WHEEL"
    license_name = f"{metadata_root}licenses/LICENSE"
    expected_metadata = {
        f"{metadata_root}METADATA",
        f"{metadata_root}WHEEL",
        f"{metadata_root}entry_points.txt",
        f"{metadata_root}top_level.txt",
        license_name,
        f"{metadata_root}RECORD",
    }
    package_policy = dict(expected_package_files)
    if not package_policy or "dispatch_core/__init__.py" not in package_policy or any(
        not isinstance(path, str)
        or not path.startswith("dispatch_core/")
        or PurePosixPath(path).as_posix() != path
        or any(part in {"", ".", ".."} for part in PurePosixPath(path).parts)
        or any(part in {"plugin", "plugins"} for part in PurePosixPath(path).parts)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        for path, digest in package_policy.items()
    ):
        raise InstallerError("wheel_package_policy", "Core wheel package policy is invalid")
    if set(members) != set(package_policy) | expected_metadata:
        raise InstallerError("wheel_package_scope", "wheel member set differs from approved Core policy")
    for path, digest in package_policy.items():
        if hashlib.sha256(archive.read(members[path])).hexdigest() != digest:
            raise InstallerError("wheel_package_digest", "Core wheel package bytes differ from approved policy")
    if metadata.get("License-Expression") != "Apache-2.0" or hashlib.sha256(
        archive.read(members[license_name])
    ).hexdigest() != _APACHE_2_LICENSE_SHA256:
        raise InstallerError("wheel_license", "Core wheel license differs from Apache-2.0 policy")
    if metadata.get("Requires-Python") != "<3.14,>=3.11":
        raise InstallerError("wheel_python_requirement", "wheel Python requirement differs from policy")
    dependency_policy = set(expected_requires_dist)
    if (
        isinstance(expected_requires_dist, str)
        or not dependency_policy
        or len(expected_requires_dist) != len(dependency_policy)
        or any(not isinstance(value, str) or not value for value in dependency_policy)
    ):
        raise InstallerError("wheel_dependency_policy", "Core wheel dependency policy is invalid")
    declared_dependencies = metadata.get_all("Requires-Dist", [])
    expected_extras = sorted(
        {
            match.group(1)
            for dependency in dependency_policy
            if (match := re.search(r";\s*extra == [\"']([A-Za-z0-9_.-]+)[\"']$", dependency)) is not None
        }
    )
    if (
        len(declared_dependencies) != len(set(declared_dependencies))
        or set(declared_dependencies) != dependency_policy
        or metadata.get_all("Provides-Extra", []) != expected_extras
    ):
        raise InstallerError("wheel_dependencies", "wheel dependencies differ from approved policy")
    wheel_metadata = BytesParser().parsebytes(archive.read(members[wheel_name]))
    if wheel_metadata.get("Root-Is-Purelib", "").lower() != "true" or wheel_metadata.get_all("Tag", []) != ["py3-none-any"]:
        raise InstallerError("wheel_tag", "wheel platform tag differs from policy")
    entry_points = configparser.ConfigParser()
    try:
        entry_points.read_string(archive.read(members[f"{metadata_root}entry_points.txt"]).decode("utf-8"))
        scripts = dict(entry_points.items("console_scripts"))
    except (UnicodeDecodeError, configparser.Error) as exc:
        raise InstallerError("wheel_entry_points", "wheel console scripts are invalid") from exc
    if scripts != {"dispatch-core": "dispatch_core.command_interface:main"}:
        raise InstallerError("wheel_entry_points", "wheel console scripts differ from policy")
    try:
        top_level = archive.read(members[f"{metadata_root}top_level.txt"]).decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise InstallerError("wheel_top_level", "wheel top-level package declaration is invalid") from exc
    if top_level != ["dispatch_core"]:
        raise InstallerError("wheel_top_level", "wheel top-level package declaration differs from policy")
    _verify_record(archive, members, record_names[0])
    return name, version


def _tree_entries(root: Path) -> list[dict[str, str | int]]:
    entries: list[dict[str, str | int]] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise InstallerError("release_symlink", f"release contains symlink: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in {"release-receipt.json", "tree-manifest.json"}:
            continue
        entries.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
                "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}",
            }
        )
    return entries


def _write_release_metadata(stage: Path, *, release_id: str, version: str, artifact_sha256: str, artifact_size: int) -> None:
    tree = {"schema_version": 1, "release_id": release_id, "files": _tree_entries(stage)}
    tree_path = stage / "tree-manifest.json"
    tree_path.write_text(json.dumps(tree, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    tree_path.chmod(0o444)
    receipt = {
        "schema_version": 1,
        "release_id": release_id,
        "distribution": "dispatch-core",
        "version": version,
        "artifact": {"sha256": artifact_sha256, "size": artifact_size},
        "contains_secrets": False,
    }
    receipt_path = stage / "release-receipt.json"
    receipt_path.write_text(json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    receipt_path.chmod(0o444)


def _verify_release_against_wheel(
    release: Path,
    archive: zipfile.ZipFile,
    members: dict[str, zipfile.ZipInfo],
) -> None:
    for name, info in members.items():
        installed = release / "site-packages" / PurePosixPath(name)
        details = installed.stat()
        expected = archive.read(info)
        if (
            details.st_nlink != 1
            or details.st_size != len(expected)
            or sha256_file(installed) != hashlib.sha256(expected).hexdigest()
        ):
            raise InstallerError("release_artifact_binding", f"Core release differs from artifact: {name}")


def _recover_unsealed_release(release: Path) -> None:
    if release.is_symlink() or not release.is_dir() or release.stat().st_uid != os.geteuid():
        raise InstallerError("release_recovery_unsafe", "interrupted Core release path is unsafe")
    members = list(release.rglob("*"))
    if any(
        member.is_symlink()
        or member.stat().st_uid != os.geteuid()
        or (member.is_file() and member.stat().st_nlink != 1)
        for member in members
    ):
        raise InstallerError("release_recovery_unsafe", "interrupted Core release tree is unsafe")
    release.chmod(0o555)
    _fsync_directory(release)
    _fsync_directory(release.parent)


def stage_core_wheel(
    layout: InstallLayout,
    wheel: Path,
    *,
    expected_sha256: str,
    expected_version: str,
    expected_package_files: Mapping[str, str],
    expected_requires_dist: Collection[str],
) -> dict[str, str | bool]:
    if not wheel.is_absolute():
        raise InstallerError("artifact_unsafe", "Core artifact path must be absolute")
    artifact = wheel.resolve(strict=True)
    if wheel != artifact or wheel.is_symlink() or not artifact.is_file():
        raise InstallerError("artifact_unsafe", "Core artifact must be a regular non-symlink file")
    artifact_details = artifact.stat()
    if (
        artifact_details.st_uid != os.geteuid()
        or artifact_details.st_nlink != 1
        or stat.S_IMODE(artifact_details.st_mode) & 0o022
    ):
        raise InstallerError("artifact_permissions", "Core artifact ownership, link count, or mode is unsafe")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise InstallerError("artifact_digest_invalid", "expected SHA-256 is invalid")
    observed_sha256 = sha256_file(artifact)
    if observed_sha256 != expected_sha256:
        raise InstallerError("artifact_digest_mismatch", "Core artifact SHA-256 mismatch")
    artifact_size = artifact.stat().st_size
    with zipfile.ZipFile(artifact) as archive:
        members = _safe_members(archive)
        _, version = _wheel_identity(archive, members, expected_package_files, expected_requires_dist)
        if version != expected_version:
            raise InstallerError("artifact_version_mismatch", "Core artifact version mismatch")
        release_id = f"dispatch-core-{version}-{observed_sha256[:16]}"
        destination = layout.releases / release_id
        with installation_lock(layout, prepare_layout=True):
            _clean_stale_core_staging(layout)
            if destination.exists():
                recovered = False
                if (
                    not destination.is_symlink()
                    and destination.is_dir()
                    and stat.S_IMODE(destination.stat().st_mode) == 0o700
                ):
                    _recover_unsealed_release(destination)
                    recovered = True
                try:
                    verified = verify_core_release(destination)
                    _verify_release_against_wheel(destination, archive, members)
                    receipt = json.loads((destination / "release-receipt.json").read_text(encoding="utf-8"))
                    if receipt["artifact"] != {"sha256": observed_sha256, "size": artifact_size}:
                        raise InstallerError("release_identity_collision", "existing release artifact identity differs")
                    return {**verified, "reused": True}
                except Exception:
                    if not recovered:
                        raise
                    _remove_release(destination)

            stage = Path(tempfile.mkdtemp(prefix=".core-", dir=layout.staging))
            published = False
            try:
                site_packages = stage / "site-packages"
                site_packages.mkdir(mode=0o700)
                for name, info in members.items():
                    target = site_packages.joinpath(*PurePosixPath(name).parts)
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    target.write_bytes(archive.read(info))
                    target.chmod(0o444)
                if sha256_file(artifact) != observed_sha256:
                    raise InstallerError("artifact_changed", "Core artifact changed during installation")
                _write_release_metadata(
                    stage,
                    release_id=release_id,
                    version=version,
                    artifact_sha256=observed_sha256,
                    artifact_size=artifact_size,
                )
                for directory in sorted((path for path in stage.rglob("*") if path.is_dir()), reverse=True):
                    directory.chmod(0o555)
                _fsync_tree(stage)
                os.replace(stage, destination)
                published = True
                destination.chmod(0o555)
                _fsync_directory(destination)
                _fsync_directory(layout.releases)
                verified = verify_core_release(destination)
                _verify_release_against_wheel(destination, archive, members)
            except Exception:
                if stage.exists():
                    _remove_release(stage)
                if published and destination.exists():
                    _remove_release(destination)
                raise
    return {**verified, "reused": False}


def verify_core_release(release: Path) -> dict[str, str | int]:
    if not release.is_dir() or release.is_symlink() or not _RELEASE_RE.fullmatch(release.name):
        raise InstallerError("release_unsafe", "Core release path is unsafe")
    if release.stat().st_uid != os.geteuid():
        raise InstallerError("release_owner", "Core release is not owned by the current user")
    if stat.S_IMODE(release.stat().st_mode) != 0o555:
        raise InstallerError("release_mode", "Core release root mode is unsafe")
    manifest_path = release / "tree-manifest.json"
    receipt_path = release / "release-receipt.json"
    if not manifest_path.is_file() or manifest_path.is_symlink() or not receipt_path.is_file() or receipt_path.is_symlink():
        raise InstallerError("release_metadata_missing", "Core release metadata is missing or unsafe")
    if manifest_path.stat().st_size > 4 * 1024 * 1024 or receipt_path.stat().st_size > 64 * 1024:
        raise InstallerError("release_metadata_size", "Core release metadata exceeds policy")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallerError("release_metadata_invalid", "Core release metadata is invalid") from exc
    if not isinstance(manifest, dict) or not isinstance(receipt, dict):
        raise InstallerError("release_metadata_shape", "Core release metadata shape is invalid")
    if set(manifest) != {"schema_version", "release_id", "files"} or manifest["schema_version"] != 1:
        raise InstallerError("tree_manifest_shape", "Core tree manifest shape is invalid")
    if manifest["release_id"] != release.name or receipt.get("release_id") != release.name:
        raise InstallerError("release_identity_mismatch", "Core release identity differs")
    if receipt.get("contains_secrets") is not False or receipt.get("distribution") != "dispatch-core":
        raise InstallerError("release_receipt_invalid", "Core release receipt is invalid")
    if set(receipt) != {"schema_version", "release_id", "distribution", "version", "artifact", "contains_secrets"}:
        raise InstallerError("release_receipt_shape", "Core release receipt shape is invalid")
    artifact = receipt["artifact"]
    if (
        receipt["schema_version"] != 1
        or not isinstance(receipt["version"], str)
        or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", receipt["version"])
        or not isinstance(artifact, dict)
        or set(artifact) != {"sha256", "size"}
        or type(artifact["size"]) is not int
        or artifact["size"] < 1
        or not isinstance(artifact["sha256"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
        or release.name != f"dispatch-core-{receipt['version']}-{artifact['sha256'][:16]}"
    ):
        raise InstallerError("release_receipt_identity", "Core release receipt identity is invalid")
    if stat.S_IMODE(manifest_path.stat().st_mode) != 0o444 or stat.S_IMODE(receipt_path.stat().st_mode) != 0o444:
        raise InstallerError("release_metadata_mode", "Core release metadata modes are invalid")
    if not isinstance(manifest["files"], list):
        raise InstallerError("tree_manifest_files", "Core tree manifest files are invalid")
    declared: dict[str, dict] = {}
    for entry in manifest["files"]:
        if (
            not isinstance(entry, dict)
            or set(entry) != {"path", "size", "sha256", "mode"}
            or not isinstance(entry["path"], str)
            or type(entry["size"]) is not int
            or entry["size"] < 0
            or not isinstance(entry["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            or not isinstance(entry["mode"], str)
            or not re.fullmatch(r"0[0-7]{3}", entry["mode"])
        ):
            raise InstallerError("tree_entry_shape", "Core tree entry shape is invalid")
        relative = PurePosixPath(entry["path"])
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts) or entry["path"] in declared:
            raise InstallerError("tree_entry_path", "Core tree entry path is unsafe")
        declared[entry["path"]] = entry
    expected_directories = {"site-packages"}
    for relative in declared:
        parent = PurePosixPath(relative).parent
        while parent.as_posix() not in {"", "."}:
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    release_members = list(release.rglob("*"))
    if any(path.is_symlink() for path in release_members):
        raise InstallerError("release_symlink", "Core release contains a symlink")
    if any(path.stat().st_uid != os.geteuid() for path in release_members):
        raise InstallerError("release_owner", "Core release contains a differently owned member")
    if any(path.is_file() and path.stat().st_nlink != 1 for path in release_members):
        raise InstallerError("release_hardlink", "Core release contains a hard-linked file")
    observed_directories = {
        path.relative_to(release).as_posix()
        for path in release_members
        if path.is_dir()
    }
    if observed_directories != expected_directories:
        raise InstallerError("release_directory_set", "Core release directory set differs")
    for path in (item for item in release_members if item.is_dir()):
        if stat.S_IMODE(path.stat().st_mode) != 0o555:
            raise InstallerError(
                "release_directory_mode",
                f"Core release directory mode is unsafe: {path.relative_to(release).as_posix()}",
            )
    observed = {
        path.relative_to(release).as_posix()
        for path in release_members
        if path.is_file()
    } - {"tree-manifest.json", "release-receipt.json"}
    if observed != set(declared):
        raise InstallerError("release_member_set", "Core release member set differs")
    for relative, entry in declared.items():
        path = release.joinpath(*PurePosixPath(relative).parts)
        if path.is_symlink() or not path.is_file():
            raise InstallerError("release_member_unsafe", f"unsafe Core release member: {relative}")
        if path.stat().st_size != entry["size"] or sha256_file(path) != entry["sha256"]:
            raise InstallerError("release_member_digest", f"Core release member differs: {relative}")
        if stat.S_IMODE(path.stat().st_mode) != int(entry["mode"], 8):
            raise InstallerError("release_member_mode", f"Core release member mode differs: {relative}")
    return {"release_id": release.name, "version": receipt["version"], "files": len(declared)}


def activate_core_release(layout: InstallLayout, release: Path) -> dict[str, str | int]:
    with installation_lock(layout, prepare_layout=True):
        verified = verify_core_release(release)
        try:
            release.resolve(strict=True).relative_to(layout.releases.resolve(strict=True))
        except ValueError as exc:
            raise InstallerError("release_outside_root", "Core release is outside the installation root") from exc
        manifest_sha256 = sha256_file(release / "tree-manifest.json")
        payload = {
            "schema_version": 1,
            "release_id": verified["release_id"],
            "tree_manifest_sha256": manifest_sha256,
            "release_receipt_sha256": sha256_file(release / "release-receipt.json"),
        }
        atomic_json(layout.active_release_selector, payload)
    return verified
