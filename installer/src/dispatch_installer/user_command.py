from __future__ import annotations

import hashlib
import json
import os
import shlex
import stat
import tempfile
from pathlib import Path

from .layout import InstallLayout, InstallerError, atomic_json

_MAX_COMMAND_BYTES = 4096
_MAX_RECEIPT_BYTES = 16 * 1024


def command_path(layout: InstallLayout) -> Path:
    return layout.home / ".local" / "bin" / "dispatch"


def command_receipt_path(layout: InstallLayout) -> Path:
    return layout.state / "install" / "command.json"


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key: {key}")
        payload[key] = value
    return payload


def _read_regular(path: Path, *, mode: int, maximum: int, code: str) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise InstallerError(code, f"Dispatch command authority cannot be opened safely: {path}") from exc
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) != mode
            or details.st_size > maximum
        ):
            raise InstallerError(code, f"Dispatch command authority is unsafe: {path}")
        data = bytearray()
        while len(data) <= maximum:
            block = os.read(descriptor, min(4096, maximum + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        if len(data) != details.st_size or len(data) > maximum:
            raise InstallerError(code, f"Dispatch command authority changed or exceeds policy: {path}")
        return bytes(data)
    except OSError as exc:
        raise InstallerError(code, f"Dispatch command authority cannot be read safely: {path}") from exc
    finally:
        os.close(descriptor)


def _validate_directory(path: Path) -> None:
    details = os.lstat(path)
    if (
        not stat.S_ISDIR(details.st_mode)
        or details.st_uid != os.geteuid()
        or details.st_mode & 0o022
    ):
        raise InstallerError("command_directory_unsafe", f"Dispatch command directory is unsafe: {path}")


def _ensure_command_directory(layout: InstallLayout) -> Path:
    directory = command_path(layout).parent
    try:
        parts = directory.relative_to(layout.home).parts
    except ValueError as exc:
        raise InstallerError("command_directory_unsafe", "Dispatch command directory is outside HOME") from exc
    current = layout.home
    _validate_directory(current)
    for part in parts:
        current = current / part
        if not current.exists():
            try:
                current.mkdir(mode=0o700)
            except OSError as exc:
                raise InstallerError("command_directory_unsafe", "Dispatch command directory could not be created") from exc
        _validate_directory(current)
    return directory


def _validate_existing_command_directory(layout: InstallLayout) -> None:
    directory = command_path(layout).parent
    try:
        parts = directory.relative_to(layout.home).parts
    except ValueError as exc:
        raise InstallerError("command_directory_unsafe", "Dispatch command directory is outside HOME") from exc
    current = layout.home
    _validate_directory(current)
    for part in parts:
        current = current / part
        if not current.exists():
            return
        _validate_directory(current)


def _script(layout: InstallLayout) -> bytes:
    launcher = layout.bin / "dispatch"
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        f"exec {shlex.quote(str(launcher))} \"$@\"\n"
    ).encode("utf-8")


def _load_receipt(layout: InstallLayout) -> dict[str, object] | None:
    path = command_receipt_path(layout)
    if not path.exists() and not path.is_symlink():
        return None
    try:
        payload = json.loads(
            _read_regular(path, mode=0o600, maximum=_MAX_RECEIPT_BYTES, code="command_receipt_unsafe").decode("utf-8"),
            object_pairs_hook=_object_pairs,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InstallerError("command_receipt_invalid", "Dispatch command receipt is invalid") from exc
    expected_command = command_path(layout)
    expected_launcher = layout.bin / "dispatch"
    if (
        not isinstance(payload, dict)
        or set(payload)
        != {
            "schema_version",
            "command",
            "command_sha256",
            "previous_sha256",
            "launcher",
            "mode",
            "status",
            "contains_secrets",
        }
        or payload.get("schema_version") != 1
        or payload.get("command") != str(expected_command)
        or payload.get("launcher") != str(expected_launcher)
        or payload.get("mode") != "0700"
        or payload.get("status") not in {"prepared", "active"}
        or payload.get("contains_secrets") is not False
        or not _valid_digest(payload.get("command_sha256"))
        or (payload.get("previous_sha256") is not None and not _valid_digest(payload.get("previous_sha256")))
    ):
        raise InstallerError("command_receipt_invalid", "Dispatch command receipt is invalid")
    return payload


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _command_digest(layout: InstallLayout) -> str | None:
    path = command_path(layout)
    if not path.exists() and not path.is_symlink():
        return None
    data = _read_regular(path, mode=0o700, maximum=_MAX_COMMAND_BYTES, code="command_unsafe")
    return hashlib.sha256(data).hexdigest()


def _unlink_current_command(layout: InstallLayout) -> None:
    path = command_path(layout)
    _validate_existing_command_directory(layout)
    parent_descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    command_descriptor = -1
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        command_descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        opened = os.fstat(command_descriptor)
        current = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
            or opened.st_uid != os.geteuid()
            or opened.st_nlink != 1
            or not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o700
        ):
            raise InstallerError("command_unsafe", "Dispatch command changed before removal")
        os.unlink(path.name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except OSError as exc:
        raise InstallerError("command_removal_failed", "Dispatch command could not be removed safely") from exc
    finally:
        if command_descriptor >= 0:
            os.close(command_descriptor)
        os.close(parent_descriptor)


def inspect_user_command(layout: InstallLayout) -> dict[str, object]:
    path = command_path(layout)
    if not layout.home.exists():
        return {
            "status": "missing",
            "command": str(path),
            "launcher": str(layout.bin / "dispatch"),
        }
    try:
        _validate_existing_command_directory(layout)
        receipt = _load_receipt(layout)
        digest = _command_digest(layout)
        if receipt is None:
            return {
                "status": "missing" if digest is None else "untracked",
                "command": str(path),
                "launcher": str(layout.bin / "dispatch"),
            }
        expected = str(receipt["command_sha256"])
        previous = receipt["previous_sha256"]
        if digest is None:
            status = "incomplete"
        elif digest == expected and receipt["status"] == "active":
            status = "ready"
        elif receipt["status"] == "prepared" and digest in {expected, previous}:
            status = "incomplete"
        else:
            status = "unsafe"
        return {
            "status": status,
            "command": str(path),
            "launcher": str(layout.bin / "dispatch"),
            "receipt_status": receipt["status"],
        }
    except InstallerError as exc:
        return {
            "status": "unsafe",
            "command": str(path),
            "launcher": str(layout.bin / "dispatch"),
            "error": str(exc)[:512],
        }


def validate_user_command_install(layout: InstallLayout) -> None:
    _validate_existing_command_directory(layout)
    receipt = _load_receipt(layout)
    digest = _command_digest(layout)
    if receipt is None:
        if digest is not None:
            raise InstallerError(
                "command_conflict",
                f"the command already exists and is not owned by this Dispatch installation: {command_path(layout)}",
            )
        return
    expected = str(receipt["command_sha256"])
    previous = receipt["previous_sha256"]
    if digest is None:
        return
    if receipt["status"] == "active" and digest == expected:
        return
    if receipt["status"] == "prepared" and digest in {expected, previous}:
        return
    raise InstallerError("command_receipt_mismatch", "the Dispatch command differs from its installation receipt")


def install_user_command(layout: InstallLayout) -> dict[str, object]:
    validate_user_command_install(layout)
    directory = _ensure_command_directory(layout)
    path = command_path(layout)
    receipt_path = command_receipt_path(layout)
    content = _script(layout)
    desired_digest = hashlib.sha256(content).hexdigest()
    receipt = _load_receipt(layout)
    current_digest = _command_digest(layout)
    if receipt is not None and receipt["status"] == "active" and current_digest == desired_digest:
        return {"status": "ready", "command": str(path), "launcher": str(layout.bin / "dispatch")}

    prepared = {
        "schema_version": 1,
        "command": str(path),
        "command_sha256": desired_digest,
        "previous_sha256": current_digest,
        "launcher": str(layout.bin / "dispatch"),
        "mode": "0700",
        "status": "prepared",
        "contains_secrets": False,
    }
    atomic_json(receipt_path, prepared, mode=0o600)

    if current_digest is not None:
        _unlink_current_command(layout)

    descriptor, temporary_name = tempfile.mkstemp(prefix=".dispatch-command-", dir=directory)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o700)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        temporary.unlink()
        _fsync_directory(directory)
    except OSError as exc:
        raise InstallerError("command_publication_failed", "Dispatch command could not be published") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)

    active = dict(prepared)
    active["previous_sha256"] = None
    active["status"] = "active"
    atomic_json(receipt_path, active, mode=0o600)
    return {"status": "ready", "command": str(path), "launcher": str(layout.bin / "dispatch")}


def remove_user_command(layout: InstallLayout) -> bool:
    receipt = _load_receipt(layout)
    if receipt is None:
        return False
    path = command_path(layout)
    digest = _command_digest(layout)
    expected = str(receipt["command_sha256"])
    previous = receipt["previous_sha256"]
    if digest is not None:
        allowed = {expected}
        if receipt["status"] == "prepared" and isinstance(previous, str):
            allowed.add(previous)
        if digest not in allowed:
            raise InstallerError("command_receipt_mismatch", "the Dispatch command differs from its installation receipt")
        _unlink_current_command(layout)
    receipt_path = command_receipt_path(layout)
    receipt_path.unlink(missing_ok=True)
    _fsync_directory(receipt_path.parent)
    return True
