from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]


def test_root_bootstrap_is_clone_based_and_prompts_on_tty() -> None:
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "Latest Stable" in script
    assert "Dev Branch" in script
    assert "/dev/tty" in script
    assert "--channel" in script
    assert "--version" in script
    assert "github.com/dillonlille/dispatch.git" in script
    assert "api.github.com/repos/dillonlille/dispatch/releases" in script
    assert "git clone" in script
    assert "refs/tags/$ref" in script
    assert "python3 -I -B -c" in script
    assert "sys.path.insert(0, sys.argv.pop(1))" in script
    assert "PYTHONPATH" not in script
    assert "--editable" not in script  # editable installer work is owned by Python package
    assert "pass --channel stable or --channel dev" in script
    assert "git clone --single-branch --branch dev" in script
    assert "git clone --quiet --no-checkout --depth 1" in script
    assert "fetch --quiet --depth 1 origin tag \"$ref\"" in script
    assert 'REQUESTED="$VERSION"' in script


def test_root_bootstrap_does_not_use_wheel_or_sudo() -> None:
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "wheel" not in script.lower()
    assert "sudo" not in script


def test_bootstrap_installer_import_ignores_current_directory(tmp_path: Path) -> None:
    shadow = tmp_path / "dispatch_installer"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("raise RuntimeError('shadow imported')\n")
    command = (
        sys.executable,
        "-I",
        "-B",
        "-c",
        "import sys; sys.path.insert(0, sys.argv.pop(1)); "
        "from dispatch_installer.cli import main; raise SystemExit(main())",
        str(ROOT / "installer" / "src"),
        "--help",
    )
    completed = subprocess.run(command, cwd=tmp_path, check=False, capture_output=True, text=True)
    assert completed.returncode == 0
    assert "usage: dispatch-installer" in completed.stdout
    assert "shadow imported" not in completed.stderr


def test_root_bootstrap_rejects_home_overlap_before_staging(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    environment = dict(os.environ, HOME=str(home), DISPATCH_HOME=str(home))
    completed = subprocess.run(
        ("sh", str(ROOT / "install.sh"), "--channel", "dev"),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode != 0
    assert "cannot equal HOME or contain HOME" in completed.stderr
    assert not (home / ".install-tmp").exists()


def test_root_bootstrap_rejects_traversal_before_staging(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    target = tmp_path / "target"
    traversal = str(tmp_path / "base" / ".." / "target")
    environment = dict(os.environ, HOME=str(home), DISPATCH_HOME=traversal)
    completed = subprocess.run(
        ("sh", str(ROOT / "install.sh"), "--channel", "dev"),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode != 0
    assert "traversal components are not allowed" in completed.stderr
    assert not (target / ".install-tmp").exists()


def test_root_bootstrap_rejects_nonprivate_existing_root_before_staging(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    root = tmp_path / "dispatch-home"
    root.mkdir(mode=0o777)
    root.chmod(0o777)
    environment = dict(os.environ, HOME=str(home), DISPATCH_HOME=str(root))
    completed = subprocess.run(
        ("sh", str(ROOT / "install.sh"), "--channel", "dev"),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode != 0
    assert "private user-owned directory" in completed.stderr
    assert not (root / ".install-tmp").exists()


def test_root_bootstrap_rejects_writable_grandparent_before_staging(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir()
    unsafe.chmod(0o777)
    parent = unsafe / "private-parent"
    parent.mkdir(mode=0o700)
    root = parent / "dispatch-home"
    environment = dict(os.environ, HOME=str(home), DISPATCH_HOME=str(root))
    completed = subprocess.run(
        ("sh", str(ROOT / "install.sh"), "--channel", "dev"),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode != 0
    assert "ownership or mode ancestor" in completed.stderr
    assert not root.exists()


def test_root_bootstrap_rejects_symlinked_home_before_staging(tmp_path: Path) -> None:
    real_home = tmp_path / "real-home"
    real_home.mkdir()
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    root = tmp_path / "dispatch-home"
    environment = dict(os.environ, HOME=str(linked_home), DISPATCH_HOME=str(root))
    completed = subprocess.run(
        ("sh", str(ROOT / "install.sh"), "--channel", "dev"),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode != 0
    assert "unsafe HOME symlink ancestor" in completed.stderr
    assert not root.exists()


def test_root_bootstrap_rejects_writable_home_before_staging(tmp_path: Path) -> None:
    home = tmp_path / "writable-home"
    home.mkdir(mode=0o700)
    home.chmod(0o777)
    root = tmp_path / "dispatch-home"
    environment = dict(os.environ, HOME=str(home), DISPATCH_HOME=str(root))
    try:
        completed = subprocess.run(
            ("sh", str(ROOT / "install.sh"), "--channel", "dev"),
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    finally:
        home.chmod(0o700)
    assert completed.returncode != 0
    assert "must not be writable by group or other" in completed.stderr
    assert not root.exists()


def test_root_bootstrap_rejects_writable_existing_staging_before_git(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    root = tmp_path / "dispatch-home"
    root.mkdir(mode=0o700)
    staging = root / ".install-tmp"
    staging.mkdir(mode=0o700)
    staging.chmod(0o777)
    environment = dict(os.environ, HOME=str(home), DISPATCH_HOME=str(root))
    try:
        completed = subprocess.run(
            ("sh", str(ROOT / "install.sh"), "--channel", "dev"),
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )
    finally:
        staging.chmod(0o700)
    assert completed.returncode != 0
    assert "unsafe temporary installation directory" in completed.stderr
    assert not any(staging.iterdir())
