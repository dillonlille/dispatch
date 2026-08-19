"""All-or-nothing timecard artifact staging."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
from typing import Any, Iterable

from dispatch_paycom.filesystem import (
    FilesystemError,
    _mkdir_private_directory_at,
    _open_private_directory_at,
    _write_private_file_at,
    ensure_private_directory,
    fsync_open_directory,
    pinned_private_directory,
    validate_private_directory,
)

from .browser import TimecardCapture
from .models import validate_timecard_record
from .period import Period, canonical_timecard_url, is_captured_timecard_url, parse_period_key, validate_code


@dataclass(frozen=True, slots=True)
class TimecardArtifact:
    directory: Path
    manifest_sha256: str
    manifest: dict[str, Any]
    entries: tuple[dict[str, Any], ...]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _private_dir(path: Path, *, create: bool = False) -> Path:
    try:
        return ensure_private_directory(path) if create else validate_private_directory(path)
    except FilesystemError as exc:
        raise ValueError("artifact_root_invalid") from exc


def _read_private_file_at(directory_descriptor: int, name: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    except OSError as exc:
        raise ValueError("artifact_invalid") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.geteuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o177
            or before.st_size > 2 * 1024 * 1024
        ):
            raise ValueError("artifact_invalid")
        data = bytearray()
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 2 * 1024 * 1024:
                raise ValueError("artifact_invalid")
        after = os.fstat(descriptor)
        if (
            (after.st_dev, after.st_ino, after.st_uid, after.st_nlink, stat.S_IMODE(after.st_mode), after.st_size)
            != (before.st_dev, before.st_ino, before.st_uid, before.st_nlink, stat.S_IMODE(before.st_mode), len(data))
        ):
            raise ValueError("artifact_invalid")
        return bytes(data)
    except OSError as exc:
        raise ValueError("artifact_invalid") from exc
    finally:
        os.close(descriptor)


def _remove_unsealed_stage(
    stage_descriptor: int,
    parent_descriptor: int,
    stage_name: str,
    stage_details: os.stat_result,
) -> None:
    names = set(os.listdir(stage_descriptor))
    before: dict[str, tuple[int, int]] = {}
    for name in names:
        details = os.stat(name, dir_fd=stage_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(details.st_mode)
            or details.st_uid != os.geteuid()
            or details.st_nlink != 1
            or stat.S_IMODE(details.st_mode) & 0o177
        ):
            raise ValueError("artifact_cleanup_failed")
        before[name] = (details.st_dev, details.st_ino)
    for name in sorted(names):
        current = os.stat(name, dir_fd=stage_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_uid != os.geteuid()
            or current.st_nlink != 1
            or stat.S_IMODE(current.st_mode) & 0o177
            or (current.st_dev, current.st_ino) != before[name]
        ):
            raise ValueError("artifact_cleanup_failed")
        os.unlink(name, dir_fd=stage_descriptor)
    fsync_open_directory(stage_descriptor)
    current = os.stat(stage_name, dir_fd=parent_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISDIR(current.st_mode)
        or current.st_uid != os.geteuid()
        or stat.S_IMODE(current.st_mode) & 0o077
        or (current.st_dev, current.st_ino) != (stage_details.st_dev, stage_details.st_ino)
    ):
        raise ValueError("artifact_cleanup_failed")
    os.rmdir(stage_name, dir_fd=parent_descriptor)
    fsync_open_directory(parent_descriptor)


def _verify_open_run(directory_descriptor: int, expected_codes: Iterable[str]) -> TimecardArtifact:
    raw = _read_private_file_at(directory_descriptor, "manifest.json")
    try:
        manifest = json.loads(raw)
        period = parse_period_key(manifest["periodKey"])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("artifact_invalid") from exc
    expected = tuple(sorted(validate_code(code) for code in expected_codes))
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError("artifact_invalid")
    try:
        entries = tuple(sorted(raw_entries, key=lambda item: item.get("employeeCode", "")))
    except AttributeError as exc:
        raise ValueError("artifact_invalid") from exc
    if (
        manifest.get("version") != 2
        or manifest.get("employeeCount") != len(entries)
        or manifest.get("expectedEmployeeCount") != len(entries)
        or tuple(item.get("employeeCode") for item in entries) != expected
        or manifest.get("dayCount") != sum(item.get("dayCount", -1) for item in entries)
    ):
        raise ValueError("artifact_membership_mismatch")
    allowed = {"manifest.json"}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("artifact_invalid")
        code = entry["employeeCode"]
        if entry.get("dayCount") != 14 or entry.get("timecardUrl") != canonical_timecard_url(code, period):
            raise ValueError("artifact_invalid")
        for kind in ("html", "json"):
            item = entry.get(kind)
            if not isinstance(item, dict):
                raise ValueError("artifact_invalid")
            name = item.get("path")
            if not isinstance(name, str) or Path(name).name != name or not name or "/" in name or "\\" in name:
                raise ValueError("artifact_invalid")
            data = _read_private_file_at(directory_descriptor, name)
            if item.get("bytes") != len(data) or item.get("sha256") != _sha(data):
                raise ValueError("artifact_invalid")
            allowed.add(name)
    if set(os.listdir(directory_descriptor)) != allowed:
        raise ValueError("artifact_invalid")
    return TimecardArtifact(Path(f"/proc/self/fd/{directory_descriptor}"), _sha(raw), manifest, entries)


class TimecardArtifactWriter:
    def __init__(self, root: Path | str, period: Period, expected_codes: Iterable[str]) -> None:
        self.root = Path(root)
        self.period = parse_period_key(period.key)
        self.expected = tuple(sorted(validate_code(code) for code in expected_codes))
        if not self.expected or len(set(self.expected)) != len(self.expected):
            raise ValueError("artifact_expected_invalid")
        self._codes: set[str] = set()
        self._entries: list[dict[str, Any]] = []
        _private_dir(self.root, create=True)
        staging = _private_dir(self.root / ".staging", create=True)
        self._staging_context = pinned_private_directory(staging)
        self._stage_parent_descriptor, _anchor = self._staging_context.__enter__()
        self._stage_name = secrets.token_hex(16)
        self._stage_descriptor: int | None = None
        self._stage_details: os.stat_result | None = None
        self._sealed = False
        try:
            self._stage_descriptor = _mkdir_private_directory_at(self._stage_parent_descriptor, self._stage_name)
            self._stage_details = os.fstat(self._stage_descriptor)
        except BaseException:
            self._staging_context.__exit__(None, None, None)
            raise
        self.stage = staging / self._stage_name

    def add(self, capture: TimecardCapture) -> None:
        if self._stage_descriptor is None or not isinstance(capture, TimecardCapture) or capture.employee_code not in self.expected or capture.employee_code in self._codes:
            raise ValueError("artifact_membership_mismatch")
        if capture.period_key != self.period.key or not is_captured_timecard_url(capture.source_url, employee_code=capture.employee_code, period=self.period):
            raise ValueError("artifact_url_invalid")
        validate_timecard_record(capture.record, employee_code=capture.employee_code, period=self.period, source_url=capture.source_url)
        if not isinstance(capture.source_html, bytes) or not 1 <= len(capture.source_html) <= 2 * 1024 * 1024:
            raise ValueError("artifact_invalid")
        json_bytes = (json.dumps(capture.record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        html_name, json_name = f"{capture.employee_code}.html", f"{capture.employee_code}.json"
        _write_private_file_at(self._stage_descriptor, html_name, capture.source_html)
        _write_private_file_at(self._stage_descriptor, json_name, json_bytes)
        self._codes.add(capture.employee_code)
        day_count = len(capture.record["days"])
        punch_count = sum(len(day["punches"]) for day in capture.record["days"])
        missing_count = sum(bool(day["missingPunch"]) for day in capture.record["days"])
        self._entries.append({
            "employeeCode": capture.employee_code,
            "timecardUrl": canonical_timecard_url(capture.employee_code, self.period),
            "html": {"path": html_name, "sha256": _sha(capture.source_html), "bytes": len(capture.source_html)},
            "json": {"path": json_name, "sha256": _sha(json_bytes), "bytes": len(json_bytes)},
            "dayCount": day_count,
            "punchCount": punch_count,
            "missingDayCount": missing_count,
        })

    def seal(self) -> TimecardArtifact:
        if self._stage_descriptor is None or self._stage_details is None:
            raise ValueError("artifact_invalid")
        if self._codes != set(self.expected) or len(self._entries) != len(self.expected):
            raise ValueError("artifact_membership_mismatch")
        entries = tuple(sorted(self._entries, key=lambda item: item["employeeCode"]))
        manifest = {
            "version": 2,
            "sourceFormat": "paycom-timecard-html.v1",
            "projectionFormat": "paycom-timecard-dom.v1",
            "periodKey": self.period.key,
            "expectedEmployeeCount": len(entries),
            "employeeCount": len(entries),
            "timecardUrlCount": len(entries),
            "dayCount": sum(item["dayCount"] for item in entries),
            "punchCount": sum(item["punchCount"] for item in entries),
            "missingDayCount": sum(item["missingDayCount"] for item in entries),
            "entries": list(entries),
        }
        manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
        _write_private_file_at(self._stage_descriptor, "manifest.json", manifest_bytes)
        digest = _sha(manifest_bytes)
        destination_parent = _private_dir(self.root / "artifacts" / self.period.key, create=True)
        destination_name = digest
        destination = destination_parent / destination_name
        stage_removed = False
        try:
            with pinned_private_directory(destination_parent) as (destination_descriptor, _destination_anchor):
                try:
                    destination_details = os.stat(destination_name, dir_fd=destination_descriptor, follow_symlinks=False)
                except FileNotFoundError:
                    os.rename(self._stage_name, destination_name, src_dir_fd=self._stage_parent_descriptor, dst_dir_fd=destination_descriptor)
                    stage_removed = True
                    fsync_open_directory(self._stage_parent_descriptor)
                    fsync_open_directory(destination_descriptor)
                else:
                    if not stat.S_ISDIR(destination_details.st_mode):
                        raise ValueError("artifact_invalid")
                    existing_descriptor = _open_private_directory_at(destination_descriptor, destination_name)
                    try:
                        _verify_open_run(existing_descriptor, self.expected)
                    finally:
                        os.close(existing_descriptor)
                    _remove_unsealed_stage(self._stage_descriptor, self._stage_parent_descriptor, self._stage_name, self._stage_details)
                    stage_removed = True
            self._sealed = True
            self.stage = destination
            self._close_stage_handles()
            return TimecardArtifact(destination, digest, manifest, entries)
        except BaseException:
            if not stage_removed:
                try:
                    self._remove_stage()
                except BaseException:
                    pass
            raise

    def _remove_stage(self) -> None:
        if self._stage_descriptor is None or self._stage_details is None:
            return
        _remove_unsealed_stage(self._stage_descriptor, self._stage_parent_descriptor, self._stage_name, self._stage_details)
        self._stage_descriptor = None

    def _close_stage_handles(self) -> None:
        if self._stage_descriptor is not None:
            os.close(self._stage_descriptor)
            self._stage_descriptor = None
        self._staging_context.__exit__(None, None, None)

    def cleanup(self) -> None:
        if not self._sealed:
            try:
                self._remove_stage()
            finally:
                self._close_stage_handles()


def verify_artifact_run(directory: Path | str, expected_codes: Iterable[str]) -> TimecardArtifact:
    root = _private_dir(Path(directory))
    with pinned_private_directory(root) as (descriptor, _anchor):
        artifact = _verify_open_run(descriptor, expected_codes)
        return TimecardArtifact(root, artifact.manifest_sha256, artifact.manifest, artifact.entries)


def discard_artifact_run(artifact: TimecardArtifact, *, root: Path | str, period: Period) -> None:
    base = _private_dir(Path(root))
    expected_parent = _private_dir(base / "artifacts" / parse_period_key(period.key).key)
    directory = _private_dir(artifact.directory)
    if directory.parent != expected_parent:
        raise ValueError("artifact_cleanup_failed")
    with pinned_private_directory(expected_parent) as (parent_descriptor, _parent_anchor):
        try:
            directory_details = os.stat(directory.name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISDIR(directory_details.st_mode):
            raise ValueError("artifact_cleanup_failed")
        descriptor = _open_private_directory_at(parent_descriptor, directory.name)
        try:
            verified = _verify_open_run(descriptor, [entry["employeeCode"] for entry in artifact.entries])
            names = {"manifest.json"}
            for entry in verified.entries:
                names.add(entry["html"]["path"])
                names.add(entry["json"]["path"])
            if set(os.listdir(descriptor)) != names:
                raise ValueError("artifact_cleanup_failed")
            expected: dict[str, tuple[int, int]] = {}
            for name in names:
                details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISREG(details.st_mode)
                    or details.st_uid != os.geteuid()
                    or details.st_nlink != 1
                    or stat.S_IMODE(details.st_mode) & 0o177
                ):
                    raise ValueError("artifact_cleanup_failed")
                expected[name] = (details.st_dev, details.st_ino)
            for name in sorted(names):
                current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (current.st_dev, current.st_ino) != expected[name] or not stat.S_ISREG(current.st_mode):
                    raise ValueError("artifact_cleanup_failed")
                os.unlink(name, dir_fd=descriptor)
            fsync_open_directory(descriptor)
            current = os.stat(directory.name, dir_fd=parent_descriptor, follow_symlinks=False)
            if (current.st_dev, current.st_ino) != (directory_details.st_dev, directory_details.st_ino):
                raise ValueError("artifact_cleanup_failed")
            os.rmdir(directory.name, dir_fd=parent_descriptor)
            fsync_open_directory(parent_descriptor)
        finally:
            os.close(descriptor)


__all__ = ["TimecardArtifact", "TimecardArtifactWriter", "discard_artifact_run", "verify_artifact_run"]
