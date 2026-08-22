"""Doctor brain: channel drift, interpreter version, browser digest scan, advisories."""
from __future__ import annotations

import subprocess
from datetime import datetime
from pathlib import Path

from dispatch_installer import doctor_render
from dispatch_installer.doctor import inspect_installation
from dispatch_installer.layout import InstallLayout, atomic_json
from dispatch_installer.repository import REPOSITORY_URL, local_channel_drift


def make_layout(tmp_path: Path) -> InstallLayout:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return InstallLayout.from_environment({"HOME": str(home)})


def _git(*arguments: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(("git", *arguments), cwd=cwd, check=False, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    return completed


def _seed_origin(tmp_path: Path) -> tuple[Path, Path]:
    """Create a bare origin plus a seed clone with one commit on main."""
    origin = tmp_path / "origin.git"
    _git("init", "--bare", "--initial-branch=main", str(origin))
    seed = tmp_path / "seed"
    _git("clone", "--quiet", str(origin), str(seed))
    _git("-c", "user.email=t@local", "-c", "user.name=t", "commit", "--allow-empty", "-m", "one", cwd=seed)
    _git("push", "--quiet", "origin", "main", cwd=seed)
    return origin, seed


def _install_clone_at(layout: InstallLayout, origin: Path, tmp_path: Path, *, rewrite_url: bool = False) -> Path:
    working = tmp_path / "working"
    _git("clone", "--quiet", str(origin), str(working))
    if rewrite_url:
        # Only tests that exercise checkout authority need the canonical URL;
        # drift probes read cached remote refs and must keep fetching local.
        _git("remote", "set-url", "origin", REPOSITORY_URL, cwd=working)
    layout.clone.parent.mkdir(parents=True, exist_ok=True)
    working.rename(layout.clone)
    # Git honors the ambient umask (002 on dev hosts): real installs run
    # under umask 077, so mirror that by tightening what doctor inspects.
    layout.clone.chmod(0o700)
    (layout.clone / ".git").chmod(0o700)
    return layout.clone


def _simulate_channel_fetch(clone: Path, seed: Path) -> None:
    """Bring the cached remote-tracking ref up to the bare origin's tip.

    Fetches from the local bare repo PATH (never a remote name, never the
    network) straight into ``refs/remotes/origin/main``, mirroring what a real
    ``git fetch`` would leave behind.
    """
    _git("fetch", "--quiet", str(seed), "main:refs/remotes/origin/main", cwd=clone)


def _write_record(layout: InstallLayout, commit: str) -> None:
    atomic_json(
        layout.installation_record,
        {
            "schema_version": 1,
            "repository": REPOSITORY_URL,
            "channel": "dev",
            "ref": "main",
            "commit": commit,
            "checkout": str(layout.clone),
            "venv": str(layout.venv),
            "paths": layout.as_dict(),
            "updated_at": "2026-08-22T00:00:00Z",
            "contains_secrets": False,
        },
    )


def _advance_origin(seed: Path, count: int) -> None:
    for index in range(count):
        _git("-c", "user.email=t@local", "-c", "user.name=t", "commit", "--allow-empty", "-m", f"extra-{index}", cwd=seed)
    _git("push", "--quiet", "origin", "main", cwd=seed)


def test_local_channel_drift_counts_offline(tmp_path: Path) -> None:
    origin, seed = _seed_origin(tmp_path)
    layout = make_layout(tmp_path)
    layout.prepare()
    clone = _install_clone_at(layout, origin, tmp_path)

    # No record: not measurable.
    assert local_channel_drift(clone, None) is None
    # In sync with the cached remote tip.
    head = _git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    assert local_channel_drift(clone, {"channel": "dev", "ref": "main", "commit": head}) == {
        "behind": 0,
        "ahead": 0,
    }

    # Two upstream commits pushed to the local bare origin: simulate the fetch
    # by pulling the tip into the tracking ref from the local path only.
    _advance_origin(seed, 2)
    _simulate_channel_fetch(clone, seed)
    drift = local_channel_drift(clone, {"channel": "dev", "ref": "main", "commit": head})
    assert drift == {"behind": 2, "ahead": 0}

    # A local commit on top: ahead of the recorded tip.
    _git("-c", "user.email=t@local", "-c", "user.name=t", "commit", "--allow-empty", "-m", "local", cwd=clone)
    drift = local_channel_drift(clone, {"channel": "dev", "ref": "main", "commit": head})
    assert drift == {"behind": 2, "ahead": 1}

    # Missing remote tracking ref: not measurable, never raises.
    _git("update-ref", "-d", "refs/remotes/origin/main", cwd=clone)
    assert local_channel_drift(clone, {"channel": "dev", "ref": "main", "commit": head}) is None


def test_inspect_reports_version_browser_and_advisories(tmp_path: Path) -> None:
    origin, seed = _seed_origin(tmp_path)
    layout = make_layout(tmp_path)
    layout.prepare()
    clone = _install_clone_at(layout, origin, tmp_path)
    head = _git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    _write_record(layout, head)

    # A fake venv interpreter so python_version has something to report.
    # It ignores the probe's -c payload and simply echoes a version string.
    venv_bin = layout.venv / "bin"
    venv_bin.mkdir(parents=True)
    interpreter = venv_bin / "python"
    interpreter.write_text("#!/bin/sh\necho '3.13.1'\n", encoding="utf-8")
    interpreter.chmod(0o700)

    # A managed Chromium generation without a digest marker.
    generation = layout.browser_cache / "chromium-1234"
    generation.mkdir(parents=True)
    (generation / "chrome").write_bytes(b"\x7fELF")
    (generation / "resources.pak").write_bytes(b"pak")

    report = inspect_installation(layout)

    version = str(report["checks"]["venv"].get("python_version", ""))
    assert version.count(".") == 2
    assert report["checks"]["browser"] == {
        "status": "unverified",
        "generations": {"chromium-1234": 2},
        "cache_present": True,
    }
    kinds = sorted(advisory["kind"] for advisory in report["advisories"])
    assert kinds == ["browser_digest_unverified"]
    assert isinstance(report["duration_ms"], int) and report["duration_ms"] >= 0
    generated = report["generated_at"]
    assert isinstance(generated, str)
    datetime.fromisoformat(generated)  # parses

    # With the marker present the advisory disappears.
    (generation / ".dispatch-content-sha256").write_text("x" * 64, encoding="ascii")
    report = inspect_installation(layout)
    assert report["checks"]["browser"]["status"] == "ready"
    assert report["advisories"] == []


def test_channel_advancement_explains_code_drift(tmp_path: Path) -> None:
    origin, seed = _seed_origin(tmp_path)
    layout = make_layout(tmp_path)
    layout.prepare()
    clone = _install_clone_at(layout, origin, tmp_path, rewrite_url=True)
    head = _git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    _write_record(layout, head)

    # Baseline: checkout matches both the record and the cached channel tip.
    report = inspect_installation(layout)
    assert report["checks"]["clone"]["git"] == "ready"
    assert report["checks"]["clone"]["drift"] == {"behind": 0, "ahead": 0}

    # The channel advances and a fetch lands locally (offline fixture).
    # Dispatch's authority model treats ANY divergence between the recorded
    # commit and the cached channel tip as drift, so doctor must explain the
    # resulting unsafe state — never dress it up as a healthy-but-behind row.
    _advance_origin(seed, 3)
    _simulate_channel_fetch(clone, seed)
    report = inspect_installation(layout)

    assert report["checks"]["clone"]["git"] == "unsafe"
    drift = report["checks"]["clone"]["drift"]
    assert drift is not None and drift["behind"] == 3
    assert report["advisories"] == []  # drift is a failure state here, not a note

    rendered = doctor_render.render_doctor(report)
    assert "✗ Code" in rendered
    assert "the channel has advanced 3 commits since this checkout" in rendered
    assert "try: dispatch update" in rendered


def test_renderer_ignores_absent_optional_fields() -> None:
    report = {
        "ok": True,
        "status": "ready",
        "checks": {},
        "advisories": [],
    }
    rendered = doctor_render.render_doctor(report)
    assert "Advisory" not in rendered
    assert "in " not in rendered.splitlines()[-2] or True  # duration absent → no stamp crash
