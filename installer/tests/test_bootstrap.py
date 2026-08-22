from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tomllib

from dispatch_installer import __version__


ROOT = Path(__file__).resolve().parents[2]


def test_installer_component_version_matches_project_metadata() -> None:
    project = tomllib.loads((ROOT / "installer" / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["version"] == __version__


def test_root_bootstrap_is_clone_based_and_prompts_on_tty() -> None:
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "Latest Stable" in script
    assert "Development (main)" in script
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
    assert "tracks the main branch" in script
    assert "git clone --single-branch --branch main" in script
    assert "git clone --single-branch --branch dev" not in script
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


def test_root_bootstrap_rejects_external_private_home_before_root_or_staging(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    root = tmp_path / "dispatch-home"
    environment = dict(
        os.environ,
        HOME=str(home),
        DISPATCH_HOME=str(root),
        DISPATCH_CONFIG_ROOT=str(home),
    )

    completed = subprocess.run(
        ("sh", str(ROOT / "install.sh"), "--channel", "dev"),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert completed.returncode != 0
    assert "DISPATCH_CONFIG_ROOT" in completed.stderr
    assert "cannot equal or contain HOME" in completed.stderr
    assert not root.exists()


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


def test_vercel_bootstrap_contract_is_release_triggered_and_nonautomatic() -> None:
    config = json.loads((ROOT / "vercel.json").read_text(encoding="utf-8"))
    assert config["framework"] is None
    assert config["buildCommand"] == "sh scripts/build-vercel-bootstrap .vercel-bootstrap"
    assert config["outputDirectory"] == ".vercel-bootstrap"
    assert config["git"]["deploymentEnabled"] is False
    headers = config["headers"]
    assert isinstance(headers, list) and len(headers) == 2
    assert headers[0]["source"] == "/install.sh"
    assert {item["key"]: item["value"] for item in headers[0]["headers"]} == {
        "Content-Type": "text/plain; charset=utf-8",
        "Cache-Control": "no-store, max-age=0",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "X-Robots-Tag": "noindex, nofollow",
    }
    assert headers[1]["source"] == "/.well-known/dispatch-bootstrap.json"

    workflow = (ROOT / ".github" / "workflows" / "publish-bootstrap-vercel.yml").read_text(
        encoding="utf-8"
    )
    assert "types: [published]" in workflow
    assert "workflow_dispatch:" in workflow
    assert 'tag:' in workflow
    assert "pull_request:" not in workflow
    assert "persist-credentials: false" in workflow
    assert "if ! git status --porcelain=v1 --untracked-files=all" in workflow
    assert workflow.count("git fetch --no-tags origin main") >= 3
    assert "VERCEL_BOOTSTRAP_DEPLOY_HOOK" in workflow
    assert "VERCEL_BOOTSTRAP_URL" in workflow
    assert "dispatch-bootstrap.json" in workflow

    release_workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "actions: write" in release_workflow
    assert "gh workflow run publish-bootstrap-vercel.yml" in release_workflow
    assert '--field tag="$RELEASE_TAG"' in release_workflow


def test_vercel_bootstrap_builder_stages_exact_private_copy(tmp_path: Path) -> None:
    output = tmp_path / "vercel-output"
    completed = subprocess.run(
        (str(ROOT / "scripts" / "build-vercel-bootstrap"), str(output)),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    assert (output / "install.sh").read_bytes() == (ROOT / "install.sh").read_bytes()
    assert (output / "robots.txt").read_text(encoding="utf-8") == "User-agent: *\nDisallow: /\n"
    marker = json.loads((output / ".well-known" / "dispatch-bootstrap.json").read_text(encoding="utf-8"))
    assert marker == {"commit": subprocess.check_output(("git", "rev-parse", "HEAD"), cwd=ROOT, text=True).strip(), "schema_version": 1}
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert stat.S_IMODE((output / "install.sh").stat().st_mode) == 0o600
    assert stat.S_IMODE((output / "robots.txt").stat().st_mode) == 0o600

    repeated = subprocess.run(
        (str(ROOT / "scripts" / "build-vercel-bootstrap"), str(output)),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert repeated.returncode != 0
    assert "already exists" in repeated.stderr
    assert (output / "install.sh").read_bytes() == (ROOT / "install.sh").read_bytes()


def test_vercel_bootstrap_builder_rejects_symlinked_output_ancestor(tmp_path: Path) -> None:
    external = tmp_path / "external"
    external.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(external, target_is_directory=True)
    output = linked / "escaped"

    completed = subprocess.run(
        (str(ROOT / "scripts" / "build-vercel-bootstrap"), str(output)),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "ancestor is unsafe" in completed.stderr
    assert not output.exists()
    assert not any(external.iterdir())


def _write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _fake_git_body(main_sha: str) -> str:
    # install.sh's dev-channel gate runs `git ls-remote <url> refs/heads/main`
    # and compares the reported SHA against the installed commit.
    return (
        'if [ "$1" = "ls-remote" ]; then\n'
        f'    printf \'%s\\trefs/heads/main\\n\' "{main_sha}"\n'
        "    exit 0\n"
        "fi\n"
        "exit 0\n"
    )


def _installed_fixture(tmp_path: Path, *, launcher_body: str) -> Path:
    """A private HOME with an installed dev-channel Dispatch and a fake launcher."""

    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    root = tmp_path / "dispatch-home"
    root.mkdir(mode=0o700)
    (root / "installation.json").write_text(
        json.dumps({"commit": "a" * 40, "channel": "dev", "ref": "main"}),
        encoding="utf-8",
    )
    _write_executable(home / ".local" / "bin" / "dispatch", launcher_body)
    return home


def _run_delegated_bootstrap(
    home: Path,
    fake_bin: Path,
    *,
    main_sha: str,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run install.sh against the fake installed Dispatch (delegation path)."""

    _write_executable(fake_bin / "git", _fake_git_body(main_sha))
    environment = dict(
        os.environ,
        HOME=str(home),
        DISPATCH_HOME=str(home.parent / "dispatch-home"),
        PATH=f"{fake_bin}:{os.environ['PATH']}",
        **(extra_env or {}),
    )
    return subprocess.run(
        ("sh", str(ROOT / "install.sh"), "--channel", "dev"),
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
        start_new_session=True,  # detached: no controlling tty, offer_setup must skip
    )


def test_root_bootstrap_gate_failure_points_at_force_not_broken_repair(tmp_path: Path) -> None:
    """Regression: the gate-failure hint suggested `dispatch repair`, but the
    installed updater reruns the very gate that failed, so the suggestion could
    never succeed. The hint must send the user to `install.sh --force`, which
    re-clones and runs the fresh installer instead of the broken installed one."""

    home = _installed_fixture(
        tmp_path,
        launcher_body=(
            "printf '%s\\n' '{\"ok\":false,\"action\":\"update\",\"status\":\"error\","
            "\"data\":{},\"error\":{\"code\":\"core_help_gate_failed\","
            "\"message\":\"staged Core failed its non-mutating verification run: "
            "env: /tmp/x/venv: Permission denied\"}}'\n"
            "exit 1\n"
        ),
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()

    completed = _run_delegated_bootstrap(home, fake_bin, main_sha="b" * 40)

    assert completed.returncode != 0
    assert "core_help_gate_failed" in completed.stderr
    assert "install.sh --force" in completed.stderr
    assert "dispatch repair" not in completed.stderr


def test_root_bootstrap_generic_delegation_failure_suggests_repair_yes(tmp_path: Path) -> None:
    """Regression: the generic failure hint suggested bare `dispatch repair`,
    which the CLI rejects with confirmation_required because repair is a
    mutating action that requires --yes."""

    home = _installed_fixture(
        tmp_path,
        launcher_body=(
            "printf '%s\\n' '{\"ok\":false,\"action\":\"update\",\"status\":\"error\","
            "\"data\":{},\"error\":{\"code\":\"venv_probe_failed\","
            "\"message\":\"the updater reported a generic failure\"}}'\n"
            "exit 1\n"
        ),
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()

    completed = _run_delegated_bootstrap(home, fake_bin, main_sha="b" * 40)

    assert completed.returncode != 0
    assert "venv_probe_failed" in completed.stderr
    assert "repair --yes" in completed.stderr
    assert "install.sh --force" in completed.stderr


def test_root_bootstrap_successful_delegation_reports_ready(tmp_path: Path) -> None:
    """The delegation happy path: the installed CLI succeeds, install.sh reports
    ready without falling through to a fresh install."""

    home = _installed_fixture(
        tmp_path,
        launcher_body='printf \'%s\\n\' "$@" >> "$DISPATCH_TEST_CALLS"\nexit 0\n',
    )
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls_file = tmp_path / "launcher-calls.txt"
    calls_file.touch()

    completed = _run_delegated_bootstrap(
        home,
        fake_bin,
        main_sha="b" * 40,
        extra_env={"DISPATCH_TEST_CALLS": str(calls_file)},
    )

    assert completed.returncode == 0, completed.stderr
    assert "Dispatch is ready" in completed.stdout
    assert calls_file.read_text(encoding="utf-8").splitlines() == ["update", "--channel", "dev"]
