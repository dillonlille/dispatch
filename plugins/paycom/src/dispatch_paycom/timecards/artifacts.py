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
    if create:
        path.mkdir(parents=True, exist_ok=True)
        os.chmod(path, 0o700)
    details = path.lstat()
    if not stat.S_ISDIR(details.st_mode) or stat.S_ISLNK(details.st_mode) or details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
        raise ValueError("artifact_root_invalid")
    return path


def _write(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if descriptor != -1:
            os.close(descriptor)


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
        _private_dir(self.root / ".staging", create=True)
        self.stage = _private_dir(self.root / ".staging" / secrets.token_hex(16), create=True)
        self._sealed = False

    def add(self, capture: TimecardCapture) -> None:
        if not isinstance(capture, TimecardCapture) or capture.employee_code not in self.expected or capture.employee_code in self._codes:
            raise ValueError("artifact_membership_mismatch")
        if capture.period_key != self.period.key or not is_captured_timecard_url(capture.source_url, employee_code=capture.employee_code, period=self.period):
            raise ValueError("artifact_url_invalid")
        validate_timecard_record(capture.record, employee_code=capture.employee_code, period=self.period, source_url=capture.source_url)
        if not isinstance(capture.source_html, bytes) or not 1 <= len(capture.source_html) <= 2 * 1024 * 1024:
            raise ValueError("artifact_invalid")
        json_bytes = (json.dumps(capture.record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        html_name, json_name = f"{capture.employee_code}.html", f"{capture.employee_code}.json"
        _write(self.stage / html_name, capture.source_html)
        _write(self.stage / json_name, json_bytes)
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
        _write(self.stage / "manifest.json", manifest_bytes)
        parent = _private_dir(self.root / "artifacts" / self.period.key, create=True)
        digest = _sha(manifest_bytes)
        destination = parent / digest
        if destination.exists():
            verify_artifact_run(destination, self.expected)
            self._remove_stage()
        else:
            os.replace(self.stage, destination)
        self._sealed = True
        self.stage = destination
        return TimecardArtifact(destination, digest, manifest, entries)

    def _remove_stage(self) -> None:
        if self.stage.exists():
            for path in self.stage.iterdir():
                path.unlink()
            self.stage.rmdir()

    def cleanup(self) -> None:
        if not self._sealed:
            self._remove_stage()


def verify_artifact_run(directory: Path | str, expected_codes: Iterable[str]) -> TimecardArtifact:
    root = _private_dir(Path(directory))
    manifest_path = root / "manifest.json"
    details = manifest_path.lstat()
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode) or stat.S_IMODE(details.st_mode) & 0o177:
        raise ValueError("artifact_invalid")
    raw = manifest_path.read_bytes()
    try:
        manifest = json.loads(raw)
        period = parse_period_key(manifest["periodKey"])
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        raise ValueError("artifact_invalid") from exc
    expected = tuple(sorted(validate_code(code) for code in expected_codes))
    entries = tuple(sorted(manifest.get("entries", []), key=lambda item: item.get("employeeCode", "")))
    if manifest.get("version") != 2 or manifest.get("employeeCount") != len(entries) or manifest.get("expectedEmployeeCount") != len(entries) or tuple(item.get("employeeCode") for item in entries) != expected or manifest.get("dayCount") != sum(item.get("dayCount", -1) for item in entries):
        raise ValueError("artifact_membership_mismatch")
    allowed = {"manifest.json"}
    for entry in entries:
        code = entry["employeeCode"]
        if entry.get("dayCount") != 14 or entry.get("timecardUrl") != canonical_timecard_url(code, period):
            raise ValueError("artifact_invalid")
        for kind in ("html", "json"):
            item = entry.get(kind)
            name = item.get("path") if isinstance(item, dict) else None
            if not isinstance(name, str) or Path(name).name != name:
                raise ValueError("artifact_invalid")
            path = root / name
            details = path.lstat()
            data = path.read_bytes()
            if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode) or stat.S_IMODE(details.st_mode) & 0o177 or item.get("bytes") != len(data) or item.get("sha256") != _sha(data):
                raise ValueError("artifact_invalid")
            allowed.add(name)
    if {path.name for path in root.iterdir()} != allowed:
        raise ValueError("artifact_invalid")
    return TimecardArtifact(root, _sha(raw), manifest, entries)


def discard_artifact_run(artifact: TimecardArtifact, *, root: Path | str, period: Period) -> None:
    base = Path(root).resolve()
    expected_parent = (base / "artifacts" / parse_period_key(period.key).key).resolve()
    directory = artifact.directory.resolve()
    if directory.parent != expected_parent or artifact.directory.is_symlink():
        raise ValueError("artifact_cleanup_failed")
    verified = verify_artifact_run(directory, [entry["employeeCode"] for entry in artifact.entries])
    names = {"manifest.json"}
    for entry in verified.entries:
        names.add(entry["html"]["path"])
        names.add(entry["json"]["path"])
    if {path.name for path in directory.iterdir()} != names:
        raise ValueError("artifact_cleanup_failed")
    for name in sorted(names):
        path = directory / name
        details = path.lstat()
        if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode) or details.st_nlink != 1:
            raise ValueError("artifact_cleanup_failed")
        path.unlink()
    directory.rmdir()
    if expected_parent.exists() and not any(expected_parent.iterdir()):
        expected_parent.rmdir()


__all__ = ["TimecardArtifact", "TimecardArtifactWriter", "discard_artifact_run", "verify_artifact_run"]
