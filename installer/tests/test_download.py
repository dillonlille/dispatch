from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import dispatch_installer.download as download_module
from dispatch_installer.download import (
    download_core_release_artifact,
    validate_core_release_asset_url,
    validate_github_https_url,
)
from dispatch_installer.layout import InstallerError

download_github_artifact = download_module._download_github_artifact


class FakeResponse:
    def __init__(self, payload: bytes, *, url: str, content_length: int | None = None) -> None:
        self._payload = payload
        self._offset = 0
        self._url = url
        self.headers = {}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        return None

    def geturl(self) -> str:
        return self._url

    def read(self, size: int) -> bytes:
        chunk = self._payload[self._offset : self._offset + size]
        self._offset += len(chunk)
        return chunk


def test_github_download_is_verified_atomic_and_reusable(tmp_path: Path, monkeypatch) -> None:
    payload = b"verified project artifact"
    digest = hashlib.sha256(payload).hexdigest()
    url = "https://github.com/example/dispatch/releases/download/v1/core.whl"
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    destination = parent / "core.whl"
    calls = 0

    def fake_open(requested_url: str, timeout: int) -> FakeResponse:
        nonlocal calls
        calls += 1
        assert requested_url == url
        assert timeout == 60
        return FakeResponse(payload, url=url, content_length=len(payload))

    monkeypatch.setattr(download_module, "_open", fake_open)
    first = download_github_artifact(
        url,
        destination,
        expected_size=len(payload),
        expected_sha256=digest,
    )
    second = download_github_artifact(
        url,
        destination,
        expected_size=len(payload),
        expected_sha256=digest,
    )
    destination.chmod(0o666)
    third = download_github_artifact(
        url,
        destination,
        expected_size=len(payload),
        expected_sha256=digest,
    )

    assert first["reused"] is False
    assert second["reused"] is True
    assert third["reused"] is False
    assert calls == 2
    assert destination.read_bytes() == payload
    assert destination.stat().st_mode & 0o777 == 0o600
    assert not list(parent.glob(".core.whl.*"))


def test_core_download_wrapper_rejects_non_core_url_before_network(tmp_path: Path, monkeypatch) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    monkeypatch.setattr(
        download_module,
        "_open",
        lambda requested_url, timeout: pytest.fail("network must not be opened"),
    )

    with pytest.raises(InstallerError) as error:
        download_core_release_artifact(
            "https://github.com/example/dispatch/archive/refs/heads/main.zip",
            parent / "core.whl",
            version="1.0.0",
            expected_size=1,
            expected_sha256="0" * 64,
        )

    assert error.value.code == "download_core_url_unapproved"


def test_download_rejects_size_digest_and_unapproved_hosts(tmp_path: Path, monkeypatch) -> None:
    payload = b"artifact"
    url = "https://release-assets.githubusercontent.com/example/artifact"
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    destination = parent / "artifact"
    monkeypatch.setattr(
        download_module,
        "_open",
        lambda requested_url, timeout: FakeResponse(payload, url=url, content_length=len(payload)),
    )

    with pytest.raises(InstallerError, match="Content-Length"):
        download_github_artifact(url, destination, expected_size=len(payload) + 1, expected_sha256="0" * 64)
    assert not destination.exists()
    assert not list(parent.iterdir())

    with pytest.raises(InstallerError, match="SHA-256"):
        download_github_artifact(url, destination, expected_size=len(payload), expected_sha256="0" * 64)
    assert not destination.exists()
    assert not list(parent.iterdir())

    for bad_url in (
        "http://github.com/example/artifact",
        "https://evil.example/artifact",
        "https://user" + "@github.com/example/artifact",
        "https://github.com:444/example/artifact",
        "https://github.com/example/artifact#fragment",
    ):
        with pytest.raises(InstallerError):
            validate_github_https_url(bad_url)


def test_core_release_authority_accepts_only_exact_versioned_wheel() -> None:
    approved = (
        "https://github.com/example/dispatch/releases/download/"
        "core-v1.0.0/dispatch_core-1.0.0-py3-none-any.whl"
    )
    assert validate_core_release_asset_url(approved, version="1.0.0") == approved

    for bad_url in (
        "https://github.com/example/dispatch/archive/refs/tags/core-v1.0.0.zip",
        "https://github.com/example/dispatch/zipball/core-v1.0.0",
        "https://github.com/example/dispatch/tarball/core-v1.0.0",
        "https://raw.githubusercontent.com/example/dispatch/main/plugins/handbook/plugin.py",
        "https://github.com/example/dispatch/releases/latest/download/dispatch_core-1.0.0-py3-none-any.whl",
        "https://github.com/example/dispatch/releases/download/plugin-handbook-v1.0.0/dispatch_core-1.0.0-py3-none-any.whl",
        "https://github.com/example/dispatch/releases/download/core-v1.0.0/dispatch_handbook-1.0.0-py3-none-any.whl",
        approved + "?token=must-not-appear-in-authority",
    ):
        with pytest.raises(InstallerError) as error:
            validate_core_release_asset_url(bad_url, version="1.0.0")
        assert error.value.code == "download_core_url_unapproved"


def test_download_requires_private_unaliased_parent(tmp_path: Path) -> None:
    parent = tmp_path / "unsafe"
    parent.mkdir(mode=0o777)
    with pytest.raises(InstallerError, match="ownership or mode"):
        download_github_artifact(
            "https://github.com/example/artifact",
            parent / "artifact",
            expected_size=1,
            expected_sha256="0" * 64,
        )


def test_download_rejects_broken_destination_symlink(tmp_path: Path) -> None:
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    destination = parent / "artifact"
    destination.symlink_to(tmp_path / "missing")

    with pytest.raises(InstallerError, match="destination is unsafe"):
        download_github_artifact(
            "https://github.com/example/artifact",
            destination,
            expected_size=1,
            expected_sha256="0" * 64,
        )


def test_download_reports_visible_but_uncertain_publication(tmp_path: Path, monkeypatch) -> None:
    payload = b"artifact"
    digest = hashlib.sha256(payload).hexdigest()
    url = "https://github.com/example/artifact"
    parent = tmp_path / "private"
    parent.mkdir(mode=0o700)
    destination = parent / "artifact"
    monkeypatch.setattr(
        download_module,
        "_open",
        lambda requested_url, timeout: FakeResponse(payload, url=url, content_length=len(payload)),
    )
    real_fsync = download_module.os.fsync
    calls = 0

    def fail_directory_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(download_module.os, "fsync", fail_directory_fsync)
    with pytest.raises(InstallerError) as error:
        download_github_artifact(
            url,
            destination,
            expected_size=len(payload),
            expected_sha256=digest,
        )

    assert error.value.code == "download_publish_uncertain"
    assert destination.read_bytes() == payload
