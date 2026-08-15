from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import replace
from pathlib import Path

from .core_release import sha256_file, verify_core_release
from .layout import (
    InstallLayout,
    InstallerError,
    atomic_json,
    installation_transaction_lock,
    lifecycle_lock,
)

_RELEASE_RE = re.compile(r"^dispatch-core-[0-9]+\.[0-9]+\.[0-9]+-[0-9a-f]{16}$")
_LAYOUT_KEYS = {
    "home",
    "dispatch_home",
    "releases",
    "bin",
    "config",
    "data",
    "state",
    "cache",
    "staging",
    "runtime",
    "browser_selector",
    "browser_generations",
}
_KNOWN_INSTALL_FILES = {
    "active-release.json",
    "installer.lock",
    "layout.json",
    "uninstall-receipt.json",
    "uninstall-transaction.json",
}
_RELEASE_QUARANTINE_PREFIX = ".uninstall-"


def _validate_layout_payload(payload: dict, layout: InstallLayout) -> None:
    if (
        set(payload)
        != {
            "schema_version",
            "installation_id",
            "layout",
            "ownership",
            "browser_authority",
            "contains_secrets",
        }
        or payload.get("schema_version") != 2
        or not isinstance(payload.get("installation_id"), str)
        or re.fullmatch(r"[0-9a-f]{32}", payload["installation_id"]) is None
        or payload.get("ownership") != "per-user"
        or payload.get("browser_authority") != "installer-owned-system"
        or payload.get("contains_secrets") is not False
        or not isinstance(payload.get("layout"), dict)
        or set(payload["layout"]) != _LAYOUT_KEYS
        or payload["layout"] != layout.as_dict()
    ):
        raise InstallerError("uninstall_layout_receipt_invalid", "layout receipt does not authorize this uninstall")


def _external_journal_path(layout: InstallLayout) -> Path:
    identity = hashlib.sha256(str(layout.dispatch_home).encode()).hexdigest()[:16]
    return layout.home / f".dispatch-uninstall-{identity}.json"


def _write_external_journal(
    layout: InstallLayout,
    *,
    source_layout: InstallLayout | None = None,
    root_descriptor: int | None = None,
) -> Path:
    source = source_layout or layout
    path = _external_journal_path(layout)
    if path.exists() or path.is_symlink():
        _safe_regular(path, mode=0o600)
    layout_payload = _read_json(source.layout_receipt)
    _validate_layout_payload(layout_payload, layout)
    root_details = os.fstat(root_descriptor) if root_descriptor is not None else os.lstat(layout.dispatch_home)
    if not stat.S_ISDIR(root_details.st_mode) or root_details.st_uid != os.geteuid():
        raise InstallerError("uninstall_root_unsafe", "DISPATCH_HOME is unsafe for purge journaling")
    payload = {
        "schema_version": 3,
        "dispatch_home": str(layout.dispatch_home),
        "dispatch_home_device": root_details.st_dev,
        "dispatch_home_inode": root_details.st_ino,
        "layout_receipt_sha256": sha256_file(source.layout_receipt),
        "layout_receipt": layout_payload,
        "mode": "purge",
        "phase": "removing",
        "contains_secrets": False,
    }
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=layout.home)
    temporary = Path(temporary_name)
    published = False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        published = True
        path.chmod(0o600)
        directory_descriptor = os.open(layout.home, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception as exc:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        if published:
            raise InstallerError(
                "uninstall_journal_publish_uncertain",
                "external uninstall journal is visible but durability confirmation failed",
            ) from exc
        raise
    return path


def _open_dispatch_root(layout: InstallLayout, payload: dict | None = None) -> int:
    flags = _directory_flags()
    parent_descriptor = -1
    root_descriptor = -1
    pinned = False
    try:
        parent_descriptor = os.open(layout.dispatch_home.parent, flags)
        root_descriptor = os.open(layout.dispatch_home.name, flags, dir_fd=parent_descriptor)
        root_details = os.fstat(root_descriptor)
        unsafe = (
            not stat.S_ISDIR(root_details.st_mode)
            or root_details.st_uid != os.geteuid()
            or stat.S_IMODE(root_details.st_mode) != 0o700
        )
        if payload is not None:
            unsafe = unsafe or (
                root_details.st_dev != payload["dispatch_home_device"]
                or root_details.st_ino != payload["dispatch_home_inode"]
            )
        if unsafe:
            code = "uninstall_journal_invalid" if payload is not None else "uninstall_root_unsafe"
            raise InstallerError(code, "DISPATCH_HOME differs from uninstall authority")
        pinned = True
        return root_descriptor
    except OSError as exc:
        code = "uninstall_journal_invalid" if payload is not None else "uninstall_root_unsafe"
        raise InstallerError(code, "cannot pin DISPATCH_HOME") from exc
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        if root_descriptor >= 0 and not pinned:
            os.close(root_descriptor)


def _layout_at_descriptor(layout: InstallLayout, root_descriptor: int) -> InstallLayout:
    root = Path(f"/proc/self/fd/{root_descriptor}")
    runtime = root / "runtime" if layout.runtime == layout.dispatch_home / "runtime" else layout.runtime
    return replace(
        layout,
        dispatch_home=root,
        releases=root / "releases",
        bin=root / "bin",
        config=root / "config",
        data=root / "data",
        state=root / "state",
        cache=root / "cache",
        staging=root / "staging",
        runtime=runtime,
    )


def _validate_external_journal_root(
    layout: InstallLayout,
    payload: dict,
    *,
    require_present: bool,
) -> None:
    if not layout.dispatch_home.exists() and not layout.dispatch_home.is_symlink():
        if require_present:
            raise InstallerError("uninstall_journal_invalid", "external journal DISPATCH_HOME is absent")
        return
    descriptor = _open_dispatch_root(layout, payload)
    os.close(descriptor)


def _load_external_journal(layout: InstallLayout) -> dict | None:
    path = _external_journal_path(layout)
    if path.is_symlink():
        raise InstallerError("uninstall_journal_unsafe", "external uninstall journal is unsafe")
    if not path.exists():
        return None
    _safe_regular(path, mode=0o600)
    payload = _read_json(path)
    if (
        set(payload) != {
            "schema_version",
            "dispatch_home",
            "dispatch_home_device",
            "dispatch_home_inode",
            "layout_receipt_sha256",
            "layout_receipt",
            "mode",
            "phase",
            "contains_secrets",
        }
        or payload.get("schema_version") != 3
        or payload.get("dispatch_home") != str(layout.dispatch_home)
        or type(payload.get("dispatch_home_device")) is not int
        or type(payload.get("dispatch_home_inode")) is not int
        or payload["dispatch_home_device"] < 0
        or payload["dispatch_home_inode"] <= 0
        or payload.get("mode") != "purge"
        or payload.get("phase") != "removing"
        or payload.get("contains_secrets") is not False
        or not isinstance(payload.get("layout_receipt_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload["layout_receipt_sha256"])
        or not isinstance(payload.get("layout_receipt"), dict)
    ):
        raise InstallerError("uninstall_journal_invalid", "external uninstall journal is invalid")
    try:
        _validate_layout_payload(payload["layout_receipt"], layout)
    except InstallerError as exc:
        raise InstallerError("uninstall_journal_invalid", "external journal does not contain valid receipt authority") from exc
    canonical_receipt = (json.dumps(payload["layout_receipt"], sort_keys=True, separators=(",", ":")) + "\n").encode()
    if hashlib.sha256(canonical_receipt).hexdigest() != payload["layout_receipt_sha256"]:
        raise InstallerError("uninstall_journal_invalid", "external journal receipt digest is invalid")
    if layout.layout_receipt.exists() and sha256_file(layout.layout_receipt) != payload["layout_receipt_sha256"]:
        raise InstallerError("uninstall_journal_invalid", "external journal differs from the layout receipt")
    _validate_external_journal_root(layout, payload, require_present=False)
    return payload


def _remove_external_journal(layout: InstallLayout) -> None:
    path = _external_journal_path(layout)
    if not path.exists() and not path.is_symlink():
        return
    _safe_regular(path, mode=0o600)
    path.unlink()
    directory_descriptor = os.open(layout.home, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_descriptor)
    finally:
        os.close(directory_descriptor)


def _safe_regular(path: Path, *, mode: int | None = None) -> os.stat_result:
    if path.is_symlink() or not path.is_file():
        raise InstallerError("uninstall_metadata_unsafe", f"uninstall metadata is unsafe: {path}")
    details = path.stat()
    if details.st_uid != os.geteuid() or details.st_nlink != 1:
        raise InstallerError("uninstall_metadata_unsafe", f"uninstall metadata ownership is unsafe: {path}")
    if mode is not None and stat.S_IMODE(details.st_mode) != mode:
        raise InstallerError("uninstall_metadata_unsafe", f"uninstall metadata mode is unsafe: {path}")
    return details


def _read_json(path: Path, *, maximum_size: int = 64 * 1024) -> dict:
    details = _safe_regular(path)
    if details.st_size > maximum_size:
        raise InstallerError("uninstall_metadata_size", f"uninstall metadata exceeds policy: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallerError("uninstall_metadata_invalid", f"uninstall metadata is invalid: {path}") from exc
    if not isinstance(payload, dict):
        raise InstallerError("uninstall_metadata_shape", f"uninstall metadata shape is invalid: {path}")
    return payload


def _validate_layout_receipt(
    layout: InstallLayout,
    *,
    authority_layout: InstallLayout | None = None,
) -> None:
    receipt = layout.layout_receipt
    _safe_regular(receipt, mode=0o600)
    payload = _read_json(receipt)
    _validate_layout_payload(payload, authority_layout or layout)


def _validate_uninstall_receipt(path: Path) -> bool:
    if not path.exists():
        return False
    _safe_regular(path, mode=0o600)
    payload = _read_json(path)
    if (
        set(payload) != {
            "schema_version",
            "status",
            "mode",
            "preserved",
            "system_dependencies",
            "contains_secrets",
        }
        or payload.get("schema_version") != 1
        or payload.get("status") != "uninstalled"
        or payload.get("mode") != "keep-data"
        or payload.get("preserved") != ["config", "data"]
        or payload.get("system_dependencies") != "preserved-shared"
        or payload.get("contains_secrets") is not False
    ):
        raise InstallerError("uninstall_receipt_invalid", "uninstall receipt is invalid")
    return True


def _directory_flags() -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def _descriptor_mount_id(descriptor: int) -> int:
    try:
        lines = Path(f"/proc/self/fdinfo/{descriptor}").read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise InstallerError("uninstall_mount_unverifiable", "cannot verify uninstall mount identity") from exc
    for line in lines:
        if line.startswith("mnt_id:"):
            value = line.partition(":")[2].strip()
            if value.isdecimal():
                return int(value)
    raise InstallerError("uninstall_mount_unverifiable", "uninstall mount identity is unavailable")


def _open_member(descriptor: int, name: str) -> int:
    if not hasattr(os, "O_PATH"):
        raise InstallerError("uninstall_mount_unverifiable", "safe mount inspection requires Linux O_PATH")
    flags = os.O_PATH
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(name, flags, dir_fd=descriptor)


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino and left.st_mode == right.st_mode


def _same_inode(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def _validate_owned_tree(
    path: Path,
    *,
    parent_descriptor: int | None = None,
) -> tuple[int, int, int]:
    flags = _directory_flags()
    owned_parent_descriptor = (
        os.open(path.parent, flags)
        if parent_descriptor is None
        else os.dup(parent_descriptor)
    )
    root_descriptor = -1
    try:
        root_descriptor = os.open(path.name, flags, dir_fd=owned_parent_descriptor)
        root = os.fstat(root_descriptor)
        if root.st_uid != os.geteuid() or not stat.S_ISDIR(root.st_mode):
            raise InstallerError("uninstall_tree_owner", f"uninstall tree has a different owner: {path}")
        mount_id = _descriptor_mount_id(root_descriptor)
        if mount_id != _descriptor_mount_id(owned_parent_descriptor):
            raise InstallerError("uninstall_tree_boundary", f"uninstall tree begins at a mount boundary: {path}")
        files = 0
        directories = 1
        size = 0

        def inspect(descriptor: int, current: Path) -> None:
            nonlocal files, directories, size
            try:
                entries = list(os.scandir(descriptor))
            except OSError as exc:
                raise InstallerError("uninstall_tree_unreadable", f"cannot inspect uninstall tree: {current}") from exc
            for entry in entries:
                details = entry.stat(follow_symlinks=False)
                member = current / entry.name
                if details.st_uid != os.geteuid() or details.st_dev != root.st_dev:
                    raise InstallerError(
                        "uninstall_tree_boundary",
                        f"uninstall tree crosses an ownership or device boundary: {member}",
                    )
                if stat.S_ISLNK(details.st_mode):
                    raise InstallerError("uninstall_tree_symlink", f"uninstall tree contains a symlink: {member}")
                if stat.S_ISDIR(details.st_mode):
                    child_descriptor = os.open(entry.name, flags, dir_fd=descriptor)
                    try:
                        opened = os.fstat(child_descriptor)
                        if not _same_identity(opened, details) or opened.st_uid != os.geteuid():
                            raise InstallerError("uninstall_tree_changed", f"uninstall directory changed: {member}")
                        if _descriptor_mount_id(child_descriptor) != mount_id:
                            raise InstallerError(
                                "uninstall_tree_boundary",
                                f"uninstall tree crosses a mount boundary: {member}",
                            )
                        directories += 1
                        inspect(child_descriptor, member)
                    finally:
                        os.close(child_descriptor)
                    continue
                member_descriptor = _open_member(descriptor, entry.name)
                try:
                    opened = os.fstat(member_descriptor)
                    if not _same_identity(opened, details) or opened.st_uid != os.geteuid():
                        raise InstallerError("uninstall_tree_changed", f"uninstall member changed: {member}")
                    if _descriptor_mount_id(member_descriptor) != mount_id:
                        raise InstallerError(
                            "uninstall_tree_boundary",
                            f"uninstall tree crosses a mount boundary: {member}",
                        )
                finally:
                    os.close(member_descriptor)
                if stat.S_ISREG(details.st_mode):
                    if details.st_nlink != 1:
                        raise InstallerError("uninstall_tree_hardlink", f"uninstall tree contains a hard-linked file: {member}")
                    files += 1
                    size += details.st_size
                elif stat.S_ISSOCK(details.st_mode) or stat.S_ISFIFO(details.st_mode):
                    files += 1
                else:
                    raise InstallerError("uninstall_tree_type", f"uninstall tree contains an unsupported file type: {member}")

        inspect(root_descriptor, path)
        return files, directories, size
    except OSError as exc:
        raise InstallerError("uninstall_tree_unreadable", f"cannot inspect uninstall tree: {path}") from exc
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)
        os.close(owned_parent_descriptor)


def _remove_owned_tree(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
    parent_descriptor: int | None = None,
) -> None:
    _validate_owned_tree(path, parent_descriptor=parent_descriptor)
    flags = _directory_flags()
    owned_parent_descriptor = (
        os.open(path.parent, flags)
        if parent_descriptor is None
        else os.dup(parent_descriptor)
    )
    root_descriptor = -1
    try:
        root_descriptor = os.open(path.name, flags, dir_fd=owned_parent_descriptor)
        root_details = os.fstat(root_descriptor)
        if root_details.st_uid != os.geteuid() or not stat.S_ISDIR(root_details.st_mode):
            raise InstallerError("uninstall_tree_unsafe", f"uninstall tree changed before removal: {path}")
        if expected_identity is not None and (root_details.st_dev, root_details.st_ino) != expected_identity:
            raise InstallerError(
                "uninstall_release_identity_changed",
                f"authorized uninstall tree identity changed before removal: {path}",
            )
        mount_id = _descriptor_mount_id(root_descriptor)
        if mount_id != _descriptor_mount_id(owned_parent_descriptor):
            raise InstallerError("uninstall_tree_boundary", f"uninstall tree changed to a mount boundary: {path}")

        def remove_contents(descriptor: int, device: int) -> None:
            if _descriptor_mount_id(descriptor) != mount_id:
                raise InstallerError("uninstall_tree_boundary", "uninstall tree crossed a mount boundary")
            os.fchmod(descriptor, 0o700)
            for entry in list(os.scandir(descriptor)):
                details = entry.stat(follow_symlinks=False)
                if details.st_uid != os.geteuid() or details.st_dev != device:
                    raise InstallerError("uninstall_tree_boundary", f"uninstall tree changed at: {entry.name}")
                if stat.S_ISLNK(details.st_mode):
                    raise InstallerError("uninstall_tree_symlink", f"uninstall tree changed to a symlink: {entry.name}")
                if stat.S_ISDIR(details.st_mode):
                    child_descriptor = os.open(entry.name, flags, dir_fd=descriptor)
                    try:
                        child_details = os.fstat(child_descriptor)
                        if not _same_identity(child_details, details) or child_details.st_uid != os.geteuid():
                            raise InstallerError("uninstall_tree_changed", f"uninstall directory changed: {entry.name}")
                        if _descriptor_mount_id(child_descriptor) != mount_id:
                            raise InstallerError(
                                "uninstall_tree_boundary",
                                f"uninstall directory crossed a mount boundary: {entry.name}",
                            )
                        remove_contents(child_descriptor, device)
                    finally:
                        os.close(child_descriptor)
                    current = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                    if not _same_inode(current, details) or not stat.S_ISDIR(current.st_mode):
                        raise InstallerError("uninstall_tree_changed", f"uninstall directory changed: {entry.name}")
                    os.rmdir(entry.name, dir_fd=descriptor)
                    continue
                member_descriptor = _open_member(descriptor, entry.name)
                try:
                    opened = os.fstat(member_descriptor)
                    if not _same_identity(opened, details) or opened.st_uid != os.geteuid():
                        raise InstallerError("uninstall_tree_changed", f"uninstall member changed: {entry.name}")
                    if _descriptor_mount_id(member_descriptor) != mount_id:
                        raise InstallerError(
                            "uninstall_tree_boundary",
                            f"uninstall member crossed a mount boundary: {entry.name}",
                        )
                finally:
                    os.close(member_descriptor)
                if stat.S_ISREG(details.st_mode):
                    if details.st_nlink != 1:
                        raise InstallerError("uninstall_tree_hardlink", f"uninstall file became hard linked: {entry.name}")
                    os.unlink(entry.name, dir_fd=descriptor)
                elif stat.S_ISSOCK(details.st_mode) or stat.S_ISFIFO(details.st_mode):
                    os.unlink(entry.name, dir_fd=descriptor)
                else:
                    raise InstallerError("uninstall_tree_type", f"unsupported uninstall member: {entry.name}")

        remove_contents(root_descriptor, root_details.st_dev)
        current_root = os.stat(path.name, dir_fd=owned_parent_descriptor, follow_symlinks=False)
        if not _same_inode(current_root, root_details) or not stat.S_ISDIR(current_root.st_mode):
            raise InstallerError("uninstall_tree_changed", f"uninstall root changed before removal: {path}")
        os.close(root_descriptor)
        root_descriptor = -1
        os.rmdir(path.name, dir_fd=owned_parent_descriptor)
    except OSError as exc:
        raise InstallerError("uninstall_tree_changed", f"uninstall tree changed during removal: {path}") from exc
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)
        os.close(owned_parent_descriptor)


def _validate_selector(layout: InstallLayout, verified_releases: dict[str, Path]) -> None:
    selector = layout.active_release_selector
    if not selector.exists():
        return
    _safe_regular(selector, mode=0o600)
    payload = _read_json(selector)
    if set(payload) != {
        "schema_version",
        "release_id",
        "tree_manifest_sha256",
        "release_receipt_sha256",
    } or payload.get("schema_version") != 1:
        raise InstallerError("uninstall_selector_invalid", "active release selector is invalid")
    release_id = payload.get("release_id")
    if not isinstance(release_id, str) or release_id not in verified_releases:
        raise InstallerError("uninstall_selector_invalid", "active release selector does not name a verified release")
    release = verified_releases[release_id]
    if (
        payload.get("tree_manifest_sha256") != sha256_file(release / "tree-manifest.json")
        or payload.get("release_receipt_sha256") != sha256_file(release / "release-receipt.json")
    ):
        raise InstallerError("uninstall_selector_invalid", "active release selector metadata differs")


def _system_authority_blockers(layout: InstallLayout) -> list[str]:
    blockers: list[str] = []
    try:
        os.lstat(layout.browser_selector)
    except FileNotFoundError:
        pass
    except OSError:
        blockers.append("browser_selector_authority_unverifiable")
    else:
        blockers.append("browser_selector_requires_privileged_uninstaller")
    try:
        generation_details = os.lstat(layout.browser_generations)
    except FileNotFoundError:
        pass
    except OSError:
        blockers.append("browser_generations_authority_unverifiable")
    else:
        populated = not stat.S_ISDIR(generation_details.st_mode)
        if not populated:
            try:
                with os.scandir(layout.browser_generations) as entries:
                    populated = next(entries, None) is not None
            except OSError:
                blockers.append("browser_generations_authority_unverifiable")
        if populated:
            blockers.append("browser_generations_require_privileged_uninstaller")
    return blockers


def _transaction_path(layout: InstallLayout) -> Path:
    return layout.state / "install" / "uninstall-transaction.json"


def _authorization_digest(layout: InstallLayout, journal: dict | None) -> str:
    if layout.layout_receipt.exists():
        return sha256_file(layout.layout_receipt)
    if journal is not None:
        return str(journal["layout_receipt_sha256"])
    raise InstallerError("uninstall_layout_receipt_missing", "layout receipt authority is unavailable")


def _release_authorization(release: Path) -> dict[str, object]:
    verify_core_release(release)
    details = os.lstat(release)
    if not stat.S_ISDIR(details.st_mode) or details.st_uid != os.geteuid():
        raise InstallerError("uninstall_release_identity_invalid", f"release identity is unsafe: {release.name}")
    return {
        "release_id": release.name,
        "device": details.st_dev,
        "inode": details.st_ino,
        "tree_manifest_sha256": sha256_file(release / "tree-manifest.json"),
        "release_receipt_sha256": sha256_file(release / "release-receipt.json"),
    }


def _validated_release_authorizations(payload: dict) -> dict[str, dict[str, object]]:
    records = payload.get("releases")
    if not isinstance(records, list):
        raise InstallerError("uninstall_transaction_invalid", "uninstall transaction release authority is invalid")
    authorized: dict[str, dict[str, object]] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "release_id",
            "device",
            "inode",
            "tree_manifest_sha256",
            "release_receipt_sha256",
        }:
            raise InstallerError("uninstall_transaction_invalid", "uninstall transaction release authority is invalid")
        release_id = record.get("release_id")
        if (
            not isinstance(release_id, str)
            or not _RELEASE_RE.fullmatch(release_id)
            or type(record.get("device")) is not int
            or int(record["device"]) < 0
            or type(record.get("inode")) is not int
            or int(record["inode"]) <= 0
            or not isinstance(record.get("tree_manifest_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(record["tree_manifest_sha256"]))
            or not isinstance(record.get("release_receipt_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", str(record["release_receipt_sha256"]))
            or release_id in authorized
        ):
            raise InstallerError("uninstall_transaction_invalid", "uninstall transaction release authority is invalid")
        authorized[release_id] = record
    return authorized


def _load_internal_transaction(
    layout: InstallLayout,
    *,
    purge: bool,
    journal: dict | None,
) -> dict | None:
    path = _transaction_path(layout)
    if path.is_symlink():
        raise InstallerError("uninstall_transaction_unsafe", "uninstall transaction is unsafe")
    if not path.exists():
        return None
    _safe_regular(path, mode=0o600)
    payload = _read_json(path)
    expected_mode = "purge" if purge else "keep-data"
    if (
        set(payload) != {
            "schema_version",
            "mode",
            "phase",
            "layout_receipt_sha256",
            "releases",
            "contains_secrets",
        }
        or payload.get("schema_version") != 3
        or payload.get("mode") != expected_mode
        or payload.get("phase") != "removing"
        or payload.get("contains_secrets") is not False
        or payload.get("layout_receipt_sha256") != _authorization_digest(layout, journal)
    ):
        if payload.get("mode") in {"purge", "keep-data"} and payload.get("mode") != expected_mode:
            raise InstallerError("uninstall_resume_mode", f"an interrupted {payload.get('mode')} uninstall must use the same mode")
        raise InstallerError("uninstall_transaction_invalid", "uninstall transaction is invalid")
    _validated_release_authorizations(payload)
    return payload


def _quarantined_release_path(layout: InstallLayout, release_name: str) -> Path:
    return layout.releases / f"{_RELEASE_QUARANTINE_PREFIX}{release_name}"


def _inspect_release_quarantine(layout: InstallLayout, transaction: dict | None) -> tuple[list[Path], list[str]]:
    if not layout.releases.exists() or not layout.releases.is_dir() or layout.releases.is_symlink():
        return [], []
    quarantined = [
        child
        for child in sorted(layout.releases.iterdir(), key=lambda item: item.name)
        if child.name.startswith(_RELEASE_QUARANTINE_PREFIX)
    ]
    if not quarantined:
        return [], []
    if transaction is None:
        return [], ["orphaned_uninstall_release_quarantine_requires_review"]
    authorized = _validated_release_authorizations(transaction)
    entries: list[Path] = []
    blockers: list[str] = []
    for child in quarantined:
        release_name = child.name.removeprefix(_RELEASE_QUARANTINE_PREFIX)
        record = authorized.get(release_name)
        if child.is_symlink() or not child.is_dir() or not _RELEASE_RE.fullmatch(release_name):
            blockers.append("unknown_uninstall_release_quarantine_entry")
            continue
        if record is None:
            blockers.append(f"unauthorized_uninstall_release_quarantine:{release_name}")
            continue
        try:
            details = os.lstat(child)
            if details.st_dev != record["device"] or details.st_ino != record["inode"]:
                raise InstallerError("uninstall_release_identity_changed", "quarantined release identity changed")
            _validate_owned_tree(child)
        except InstallerError as exc:
            blockers.append(f"unsafe_uninstall_release_quarantine:{release_name}:{exc.code}")
        else:
            entries.append(child)
    return entries, blockers


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, _directory_flags())
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_transaction(
    layout: InstallLayout,
    *,
    purge: bool,
    journal: dict | None,
    release_names: set[str],
) -> dict:
    existing = _load_internal_transaction(
        layout,
        purge=purge,
        journal=journal,
    )
    if existing is not None:
        return existing
    releases = [
        _release_authorization(layout.releases / release_name)
        for release_name in sorted(release_names)
    ]
    payload = {
        "schema_version": 3,
        "mode": "purge" if purge else "keep-data",
        "phase": "removing",
        "layout_receipt_sha256": _authorization_digest(layout, journal),
        "releases": releases,
        "contains_secrets": False,
    }
    atomic_json(_transaction_path(layout), payload)
    return payload


def _remove_verified_releases(
    layout: InstallLayout,
    transaction: dict,
) -> None:
    authorized = _validated_release_authorizations(transaction)
    quarantined, blockers = _inspect_release_quarantine(layout, transaction)
    if blockers:
        raise InstallerError("uninstall_release_quarantine_unsafe", "; ".join(blockers)[:512])
    for retained in quarantined:
        release_name = retained.name.removeprefix(_RELEASE_QUARANTINE_PREFIX)
        record = authorized[release_name]
        _remove_owned_tree(
            retained,
            expected_identity=(record["device"], record["inode"]),
        )

    for name, record in sorted(authorized.items()):
        release = layout.releases / name
        if not release.exists():
            continue
        current = _release_authorization(release)
        if current != record:
            raise InstallerError("uninstall_release_identity_changed", f"release identity changed: {name}")
        destination = _quarantined_release_path(layout, name)
        if destination.exists() or destination.is_symlink():
            raise InstallerError("uninstall_release_quarantine_unsafe", "uninstall release quarantine already exists")
        try:
            os.rename(release, destination)
            _fsync_directory(layout.releases)
        except OSError as exc:
            raise InstallerError("uninstall_release_quarantine_failed", f"cannot quarantine release: {name}") from exc
        moved = os.lstat(destination)
        if moved.st_dev != record["device"] or moved.st_ino != record["inode"]:
            raise InstallerError("uninstall_release_identity_changed", f"quarantined release identity changed: {name}")
        _remove_owned_tree(
            destination,
            expected_identity=(record["device"], record["inode"]),
        )


def _base_plan(
    layout: InstallLayout,
    *,
    purge: bool,
    resume_journal: dict | None = None,
    authority_layout: InstallLayout | None = None,
    root_descriptor: int | None = None,
) -> dict[str, object]:
    authority = authority_layout or layout
    mode = "purge" if purge else "keep-data"
    plan: dict[str, object] = {
        "schema_version": 1,
        "mode": mode,
        "status": "planned",
        "remove": [],
        "preserve": [],
        "blockers": [],
        "system_dependencies": "preserved-shared",
        "hermes": "untouched",
    }
    blockers = _system_authority_blockers(layout)
    if root_descriptor is None:
        if layout.dispatch_home.is_symlink():
            raise InstallerError("uninstall_root_unsafe", "DISPATCH_HOME is unsafe")
        if not layout.dispatch_home.exists():
            plan["blockers"] = sorted(set(blockers))
            plan["status"] = "blocked" if blockers else "already-absent"
            return plan
        if layout.dispatch_home.is_symlink() or not layout.dispatch_home.is_dir():
            raise InstallerError("uninstall_root_unsafe", "DISPATCH_HOME is unsafe")
        root = layout.dispatch_home.stat()
    else:
        root = os.fstat(root_descriptor)
        if not stat.S_ISDIR(root.st_mode):
            raise InstallerError("uninstall_root_unsafe", "pinned DISPATCH_HOME is unsafe")
    if root.st_uid != os.geteuid() or stat.S_IMODE(root.st_mode) != 0o700:
        raise InstallerError("uninstall_root_unsafe", "DISPATCH_HOME ownership or mode is unsafe")

    if not layout.layout_receipt.exists() and resume_journal is None:
        standard_names = {"releases", "bin", "state", "cache", "staging", "runtime"}
        present = {path.name for path in layout.dispatch_home.iterdir()}
        if present.isdisjoint(standard_names):
            plan["blockers"] = sorted(set(blockers))
            plan["status"] = "blocked" if blockers else "already-uninstalled"
            plan["preserve"] = sorted(str(path) for path in layout.dispatch_home.iterdir())
            return plan
        raise InstallerError("uninstall_layout_receipt_missing", "layout receipt is missing; uninstall ownership is unproven")

    if layout.layout_receipt.exists():
        _validate_layout_receipt(layout, authority_layout=authority)
    transaction = _load_internal_transaction(
        layout,
        purge=purge,
        journal=resume_journal,
    )
    uninstall_receipt = layout.state / "install" / "uninstall-receipt.json"
    already_uninstalled = _validate_uninstall_receipt(uninstall_receipt)

    expected_roots = {
        layout.releases,
        layout.bin,
        layout.config,
        layout.data,
        layout.state,
        layout.cache,
        layout.staging,
    }
    if layout.runtime.parent == layout.dispatch_home:
        expected_roots.add(layout.runtime)
    for root_path in expected_roots:
        if root_path.is_symlink():
            blockers.append(f"unsafe_layout_root:{root_path.name}")
        elif root_path.exists() and not root_path.is_dir():
            blockers.append(f"unsafe_layout_root:{root_path.name}")
        elif root_path.exists() and root_path.stat().st_uid != os.geteuid():
            blockers.append(f"unsafe_layout_owner:{root_path.name}")

    verified_releases: dict[str, Path] = {}
    preserved_release_entries: list[Path] = []
    if layout.releases.exists() and layout.releases.is_dir() and not layout.releases.is_symlink():
        for entry in sorted(layout.releases.iterdir()):
            if entry.name.startswith(_RELEASE_QUARANTINE_PREFIX):
                continue
            if entry.is_dir() and not entry.is_symlink() and _RELEASE_RE.fullmatch(entry.name):
                try:
                    verify_core_release(entry)
                except InstallerError as exc:
                    blockers.append(f"release_invalid:{entry.name}:{exc.code}")
                else:
                    verified_releases[entry.name] = entry
            else:
                preserved_release_entries.append(entry)
    if purge and preserved_release_entries:
        blockers.append("unknown_release_entries_require_review")
    quarantined_releases, quarantine_blockers = _inspect_release_quarantine(layout, transaction)
    blockers.extend(quarantine_blockers)
    if transaction is not None:
        authorized = _validated_release_authorizations(transaction)
        quarantined_names = {
            path.name.removeprefix(_RELEASE_QUARANTINE_PREFIX)
            for path in quarantined_releases
        }
        for release_name, release in verified_releases.items():
            record = authorized.get(release_name)
            if record is None:
                blockers.append(f"release_not_transaction_authorized:{release_name}")
                continue
            try:
                current = _release_authorization(release)
            except InstallerError as exc:
                blockers.append(f"release_invalid:{release_name}:{exc.code}")
            else:
                if current != record:
                    blockers.append(f"release_identity_changed:{release_name}")
            if release_name in quarantined_names:
                blockers.append(f"release_source_and_quarantine_both_present:{release_name}")
    try:
        _validate_selector(layout, verified_releases)
    except InstallerError as exc:
        blockers.append(exc.code)

    browser_state = layout.state / "browser-manager"
    if browser_state.exists():
        try:
            if browser_state.is_symlink() or not browser_state.is_dir() or any(browser_state.iterdir()):
                blockers.append("browser_manager_state_requires_shutdown_verification")
        except OSError:
            blockers.append("browser_manager_state_requires_shutdown_verification")
    if layout.runtime.is_symlink():
        blockers.append("runtime_requires_shutdown_verification")
    elif layout.runtime.exists():
        try:
            if layout.runtime.is_symlink() or not layout.runtime.is_dir() or any(layout.runtime.iterdir()):
                blockers.append("runtime_requires_shutdown_verification")
        except OSError:
            blockers.append("runtime_requires_shutdown_verification")

    targets = [layout.bin, layout.cache, layout.staging, layout.runtime]
    if purge:
        targets.extend((layout.config, layout.data))
    for target in targets:
        if target.exists() and target.is_dir() and not target.is_symlink():
            try:
                _validate_owned_tree(
                    target,
                    parent_descriptor=(
                        root_descriptor
                        if root_descriptor is not None and target.parent == layout.dispatch_home
                        else None
                    ),
                )
            except InstallerError as exc:
                blockers.append(f"unsafe_tree:{target.name}:{exc.code}")
    if layout.state.exists() and layout.state.is_dir() and not layout.state.is_symlink():
        try:
            _validate_owned_tree(layout.state, parent_descriptor=root_descriptor)
        except InstallerError as exc:
            blockers.append(f"unsafe_tree:state:{exc.code}")

    remove = [str(path) for path in verified_releases.values()]
    remove.extend(str(path) for path in quarantined_releases)
    remove.extend(str(path) for path in targets if path.exists())
    remove.append(str(layout.state) if purge else str(layout.state / "<operational-state>"))
    preserve = [str(path) for path in preserved_release_entries]
    if not purge:
        preserve.extend(str(path) for path in (layout.config, layout.data) if path.exists())
        install_directory = layout.state / "install"
        if install_directory.exists() and not install_directory.is_symlink() and install_directory.is_dir():
            preserve.extend(
                str(child)
                for child in sorted(install_directory.iterdir(), key=lambda item: item.name)
                if child.name not in _KNOWN_INSTALL_FILES
            )
    expected_names = {path.name for path in expected_roots}
    preserve.extend(
        str(path)
        for path in sorted(layout.dispatch_home.iterdir())
        if path.name not in expected_names
    )
    if blockers:
        plan["status"] = "blocked"
    elif already_uninstalled and not purge:
        plan["status"] = "planned" if transaction is not None else "already-uninstalled"
    plan["remove"] = sorted(set(remove))
    plan["preserve"] = sorted(set(preserve))
    plan["blockers"] = sorted(set(blockers))
    return plan


def plan_uninstall(layout: InstallLayout, *, purge: bool = False) -> dict[str, object]:
    """Return a non-mutating, receipt-bound uninstall plan."""

    with lifecycle_lock(layout):
        journal = _load_external_journal(layout)
        if journal is not None and not purge:
            raise InstallerError("uninstall_resume_mode", "an interrupted purge must be resumed with --purge")
        return _base_plan(layout, purge=purge, resume_journal=journal)


def _unlink_known_install_files(layout: InstallLayout, *, keep_receipts: bool) -> None:
    install = layout.state / "install"
    if not install.exists():
        return
    for child in list(install.iterdir()):
        if child.name not in _KNOWN_INSTALL_FILES:
            if not keep_receipts:
                if child.is_dir() and not child.is_symlink():
                    _remove_owned_tree(child)
                else:
                    _safe_regular(child)
                    child.unlink()
            continue
        if keep_receipts and child.name in {"installer.lock", "layout.json", "uninstall-receipt.json", "uninstall-transaction.json"}:
            continue
        if child.is_dir() and not child.is_symlink():
            _remove_owned_tree(child)
        else:
            _safe_regular(child)
            child.unlink()


def _remove_state_except_install(layout: InstallLayout) -> None:
    if not layout.state.exists():
        return
    for child in list(layout.state.iterdir()):
        if child.name == "install":
            continue
        if child.is_dir() and not child.is_symlink():
            _remove_owned_tree(child)
        else:
            _safe_regular(child)
            child.unlink()


def _remove_pinned_root_if_empty(layout: InstallLayout, root_descriptor: int) -> bool:
    if next(iter(os.scandir(root_descriptor)), None) is not None:
        return False
    parent_descriptor = os.open(layout.dispatch_home.parent, _directory_flags())
    try:
        try:
            current = os.stat(layout.dispatch_home.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise InstallerError("uninstall_root_changed", "DISPATCH_HOME moved during uninstall") from exc
        pinned = os.fstat(root_descriptor)
        if not _same_inode(current, pinned) or not stat.S_ISDIR(current.st_mode):
            raise InstallerError("uninstall_root_changed", "DISPATCH_HOME changed during uninstall")
        os.rmdir(layout.dispatch_home.name, dir_fd=parent_descriptor)
        return True
    except OSError as exc:
        raise InstallerError("uninstall_root_changed", "cannot remove pinned DISPATCH_HOME") from exc
    finally:
        os.close(parent_descriptor)


def _apply_uninstall_locked(
    layout: InstallLayout,
    *,
    purge: bool,
    journal: dict | None,
    plan: dict[str, object],
    authority_layout: InstallLayout | None = None,
    root_descriptor: int | None = None,
) -> dict[str, object]:
    authority = authority_layout or layout
    blockers = plan["blockers"]
    if blockers:
        raise InstallerError("uninstall_blocked", "; ".join(str(item) for item in blockers)[:512])
    if plan["status"] == "already-absent":
        return plan
    if plan["status"] == "already-uninstalled" and not purge:
        return plan

    if purge and journal is None:
        _write_external_journal(
            authority,
            source_layout=layout,
            root_descriptor=root_descriptor,
        )
        journal = _load_external_journal(authority)
    if purge:
        if journal is None or root_descriptor is None:
            raise InstallerError("uninstall_journal_invalid", "purge journal is unavailable")
        root_details = os.fstat(root_descriptor)
        if (
            root_details.st_dev != journal["dispatch_home_device"]
            or root_details.st_ino != journal["dispatch_home_inode"]
        ):
            raise InstallerError("uninstall_journal_invalid", "pinned DISPATCH_HOME differs from purge journal")

    transaction = _transaction_path(layout)
    verified_release_names = {
        path.name
        for path in layout.releases.iterdir()
        if path.is_dir() and not path.is_symlink() and _RELEASE_RE.fullmatch(path.name)
    } if layout.releases.exists() else set()
    transaction_payload = _ensure_transaction(
        layout,
        purge=purge,
        journal=journal,
        release_names=verified_release_names,
    )

    if layout.active_release_selector.exists():
        _safe_regular(layout.active_release_selector, mode=0o600)
        layout.active_release_selector.unlink()
        _fsync_directory(layout.active_release_selector.parent)
    quarantined_releases, _ = _inspect_release_quarantine(layout, transaction_payload)
    if verified_release_names or quarantined_releases:
        _remove_verified_releases(layout, transaction_payload)
    if layout.releases.exists() and not any(layout.releases.iterdir()):
        layout.releases.rmdir()

    for target in (layout.bin, layout.cache, layout.staging, layout.runtime):
        if target.exists():
            _remove_owned_tree(
                target,
                parent_descriptor=(
                    root_descriptor
                    if root_descriptor is not None and target.parent == layout.dispatch_home
                    else None
                ),
            )
    _remove_state_except_install(layout)

    if purge:
        for target in (layout.config, layout.data):
            if target.exists():
                _remove_owned_tree(target, parent_descriptor=root_descriptor)
        _unlink_known_install_files(layout, keep_receipts=False)
        install = layout.state / "install"
        if install.exists() and not any(install.iterdir()):
            install.rmdir()
        if layout.state.exists() and not any(layout.state.iterdir()):
            layout.state.rmdir()
        if root_descriptor is None:
            raise InstallerError("uninstall_root_unsafe", "purge root is not pinned")
        _remove_pinned_root_if_empty(authority, root_descriptor)
        _remove_external_journal(authority)
        result = _base_plan(authority, purge=True)
        if result["blockers"]:
            raise InstallerError("uninstall_incomplete", "; ".join(str(item) for item in result["blockers"])[:512])
        result["status"] = "purged" if not authority.dispatch_home.exists() else "purged-with-preserved-files"
        return result

    _unlink_known_install_files(layout, keep_receipts=True)
    receipt = layout.state / "install" / "uninstall-receipt.json"
    atomic_json(
        receipt,
        {
            "schema_version": 1,
            "status": "uninstalled",
            "mode": "keep-data",
            "preserved": ["config", "data"],
            "system_dependencies": "preserved-shared",
            "contains_secrets": False,
        },
    )
    transaction.unlink(missing_ok=True)
    _fsync_directory(transaction.parent)
    result = _base_plan(authority, purge=False)
    if result["blockers"]:
        raise InstallerError("uninstall_incomplete", "; ".join(str(item) for item in result["blockers"])[:512])
    result["status"] = "uninstalled"
    return result


def uninstall(layout: InstallLayout, *, purge: bool = False) -> dict[str, object]:
    """Apply a confirmed user-scope uninstall transaction."""

    with lifecycle_lock(layout):
        journal = _load_external_journal(layout)
        if journal is not None and not purge:
            raise InstallerError("uninstall_resume_mode", "an interrupted purge must be resumed with --purge")
        if layout.dispatch_home.is_symlink():
            raise InstallerError("uninstall_root_unsafe", "DISPATCH_HOME is unsafe")
        if not layout.dispatch_home.exists():
            result = _base_plan(layout, purge=purge, resume_journal=journal)
            if result["blockers"]:
                raise InstallerError("uninstall_blocked", "; ".join(str(item) for item in result["blockers"])[:512])
            if journal is not None:
                _remove_external_journal(layout)
                result = _base_plan(layout, purge=purge)
                if result["blockers"]:
                    raise InstallerError("uninstall_incomplete", "; ".join(str(item) for item in result["blockers"])[:512])
                result["status"] = "purged"
            return result
        root_descriptor = _open_dispatch_root(layout, journal)
        pinned_layout = _layout_at_descriptor(layout, root_descriptor)
        try:
            preliminary = _base_plan(
                pinned_layout,
                purge=purge,
                resume_journal=journal,
                authority_layout=layout,
                root_descriptor=root_descriptor,
            )
            if preliminary["status"] == "blocked" and not pinned_layout.layout_receipt.exists():
                raise InstallerError("uninstall_blocked", "; ".join(str(item) for item in preliminary["blockers"])[:512])
            if preliminary["status"] == "already-uninstalled":
                if journal is not None:
                    _validate_external_journal_root(layout, journal, require_present=True)
                    _remove_external_journal(layout)
                    preliminary = _base_plan(layout, purge=purge)
                    if preliminary["blockers"]:
                        raise InstallerError("uninstall_incomplete", "; ".join(str(item) for item in preliminary["blockers"])[:512])
                    preliminary["status"] = "purged-with-preserved-files"
                return preliminary
            with installation_transaction_lock(
                pinned_layout,
                allow_state_creation=journal is not None,
                strict_existing=True,
                root_descriptor=root_descriptor,
            ):
                plan = _base_plan(
                    pinned_layout,
                    purge=purge,
                    resume_journal=journal,
                    authority_layout=layout,
                    root_descriptor=root_descriptor,
                )
                return _apply_uninstall_locked(
                    pinned_layout,
                    purge=purge,
                    journal=journal,
                    plan=plan,
                    authority_layout=layout,
                    root_descriptor=root_descriptor,
                )
        finally:
            os.close(root_descriptor)
