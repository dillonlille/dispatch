from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .layout import InstallerError

APPROVED_GITHUB_HOSTS = frozenset(
    {
        "github.com",
        "objects.githubusercontent.com",
        "raw.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)
_MAX_URL_LENGTH = 2048
_MAX_ARTIFACT_SIZE = 4 * 1024 * 1024 * 1024
_CHUNK_SIZE = 64 * 1024


def validate_github_https_url(url: str) -> str:
    if not isinstance(url, str) or not url or len(url) > _MAX_URL_LENGTH:
        raise InstallerError("download_url_invalid", "artifact URL is invalid")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError as exc:
        raise InstallerError("download_url_invalid", "artifact URL port is invalid") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in APPROVED_GITHUB_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        raise InstallerError("download_url_unapproved", "artifact URL is not an approved GitHub HTTPS URL")
    return url


def validate_core_release_asset_url(url: str, *, version: str) -> str:
    """Validate the immutable, public-facing URL for one Core wheel.

    Redirect targets use the broader GitHub transport policy above. The
    authority stored in a release manifest is deliberately narrower: it may
    identify only the versioned Core wheel release asset, never repository
    source, a moving alias, or a plugin artifact.
    """

    validate_github_https_url(url)
    if not isinstance(version, str) or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise InstallerError("download_core_version", "Core artifact version is invalid")
    parsed = urlsplit(url)
    expected = re.compile(
        rf"^/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/"
        rf"core-v{re.escape(version)}/dispatch_core-{re.escape(version)}-py3-none-any\.whl$"
    )
    if parsed.hostname != "github.com" or parsed.query or expected.fullmatch(parsed.path) is None:
        raise InstallerError(
            "download_core_url_unapproved",
            "Core artifact must be an immutable versioned GitHub release wheel",
        )
    return url


class _ApprovedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        validate_github_https_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: int):
    opener = urllib.request.build_opener(_ApprovedRedirectHandler())
    request = urllib.request.Request(
        url,
        headers={"Accept-Encoding": "identity", "User-Agent": "dispatch-installer/0.1"},
        method="GET",
    )
    return opener.open(request, timeout=timeout)


def _validate_private_parent(parent: Path) -> None:
    if parent.is_symlink() or not parent.is_dir() or parent != parent.resolve(strict=True):
        raise InstallerError("download_parent_unsafe", "download parent must be a private directory")
    details = parent.stat()
    if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) != 0o700:
        raise InstallerError("download_parent_unsafe", "download parent ownership or mode is unsafe")


def _download_github_artifact(
    url: str,
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    timeout: int = 60,
) -> dict[str, str | int | bool]:
    validate_github_https_url(url)
    if (
        not isinstance(expected_size, int)
        or isinstance(expected_size, bool)
        or expected_size <= 0
        or expected_size > _MAX_ARTIFACT_SIZE
    ):
        raise InstallerError("download_size_invalid", "expected artifact size is invalid")
    if len(expected_sha256) != 64 or any(character not in "0123456789abcdef" for character in expected_sha256):
        raise InstallerError("download_digest_invalid", "expected artifact SHA-256 is invalid")
    if timeout < 1 or timeout > 300:
        raise InstallerError("download_timeout_invalid", "download timeout is invalid")
    if (
        not destination.is_absolute()
        or destination.name in {"", ".", ".."}
        or any(part in {".", ".."} for part in destination.parts)
    ):
        raise InstallerError("download_destination_invalid", "download destination must be an absolute file path")
    _validate_private_parent(destination.parent)
    if destination.is_symlink():
        raise InstallerError("download_destination_unsafe", "download destination is unsafe")
    if destination.exists():
        if not destination.is_file():
            raise InstallerError("download_destination_unsafe", "download destination is unsafe")
        details = destination.stat()
        if (
            details.st_uid == os.geteuid()
            and details.st_nlink == 1
            and stat.S_IMODE(details.st_mode) == 0o600
            and details.st_size == expected_size
            and hashlib.sha256(destination.read_bytes()).hexdigest() == expected_sha256
        ):
            return {
                "url": url,
                "path": str(destination),
                "size": expected_size,
                "sha256": expected_sha256,
                "reused": True,
            }

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    observed_size = 0
    published = False
    try:
        os.fchmod(descriptor, 0o600)
        try:
            with _open(url, timeout) as response:
                validate_github_https_url(response.geturl())
                content_encoding = response.headers.get("Content-Encoding")
                if content_encoding not in {None, "identity"}:
                    raise InstallerError("download_encoding", "artifact response encoding is not identity")
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError as exc:
                        raise InstallerError("download_length_invalid", "artifact Content-Length is invalid") from exc
                    if declared_size != expected_size:
                        raise InstallerError("download_size_mismatch", "artifact Content-Length differs")
                while True:
                    chunk = response.read(min(_CHUNK_SIZE, expected_size - observed_size + 1))
                    if not chunk:
                        break
                    observed_size += len(chunk)
                    if observed_size > expected_size:
                        raise InstallerError("download_size_mismatch", "artifact exceeds expected size")
                    digest.update(chunk)
                    remaining = memoryview(chunk)
                    while remaining:
                        written = os.write(descriptor, remaining)
                        if written <= 0:
                            raise InstallerError("download_write_failed", "artifact staging write failed")
                        remaining = remaining[written:]
        except (urllib.error.URLError, TimeoutError) as exc:
            raise InstallerError("download_failed", "artifact download failed") from exc
        if observed_size != expected_size:
            raise InstallerError("download_size_mismatch", "artifact size differs")
        if digest.hexdigest() != expected_sha256:
            raise InstallerError("download_digest_mismatch", "artifact SHA-256 differs")
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, destination)
        published = True
        destination.chmod(0o600)
        directory_descriptor = os.open(destination.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
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
                "download_publish_uncertain",
                "downloaded artifact is visible but durability confirmation failed; inspect before retry",
            ) from exc
        raise
    return {
        "url": url,
        "path": str(destination),
        "size": expected_size,
        "sha256": expected_sha256,
        "reused": False,
    }


def download_core_release_artifact(
    url: str,
    destination: Path,
    *,
    version: str,
    expected_size: int,
    expected_sha256: str,
    timeout: int = 60,
) -> dict[str, str | int | bool]:
    """Download one exact manifest-authorized Core wheel without broadening policy."""

    validate_core_release_asset_url(url, version=version)
    return _download_github_artifact(
        url,
        destination,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
        timeout=timeout,
    )
