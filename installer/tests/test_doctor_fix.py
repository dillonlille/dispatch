"""`dispatch doctor --fix`: plan determinism, executor safety, confirmation gating."""
from __future__ import annotations

import json
import os
import shlex
import subprocess
from pathlib import Path

from dispatch_installer.cli import main as installer_main
from dispatch_installer.doctor_fix import apply_action, build_fix_plan, summarize
from dispatch_installer.layout import InstallLayout


def shlex_quote(value: str) -> str:
    return shlex.quote(value)


def make_layout(tmp_path: Path) -> InstallLayout:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return InstallLayout.from_environment({"HOME": str(home)})


def completed(returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess((), returncode, "", stderr)


def test_plan_is_derived_only_from_safe_conditions() -> None:
    report = {
        "checks": {
            "service": {"status": "incomplete", "active": False, "enabled": False, "service": "dispatch.service"},
            "state": {
                "status": "unsafe",
                "path": "/users/demo/.dispatch/state",
                "reason": "permissions are too open (drwxrwxrwx)",
                "hint": "chmod 700 '/users/demo/.dispatch/state'",
            },
            # Not fixable: foreign-owned, missing, lifecycle-domain problems.
            "cache": {"status": "unsafe", "path": "/users/demo/.dispatch/cache", "reason": "directory is owned by another user"},
            "logs": {"status": "missing", "path": "/users/demo/.dispatch/logs"},
            "clone": {"status": "ready", "git": "unsafe"},
        }
    }
    plan = build_fix_plan(report)
    assert [action["kind"] for action in plan] == ["start_service", "enable_service", "repair_permissions"]
    assert plan[2]["command"] == f"chmod 700 {shlex_quote('/users/demo/.dispatch/state')}"


def test_plan_is_empty_for_healthy_report_and_stable_across_calls() -> None:
    healthy = {"checks": {"service": {"status": "ready", "active": True, "enabled": True}}}
    assert build_fix_plan(healthy) == []
    report = {
        "checks": {
            "service": {"status": "incomplete", "active": True, "enabled": False, "service": "dispatch.service"},
            "run": {"status": "unsafe", "path": "/users/demo/.dispatch/run", "reason": "permissions are too open (drwxrwx---)", "hint": "chmod 700 '/users/demo/.dispatch/run'"},
        }
    }
    assert build_fix_plan(report) == build_fix_plan(report)


def test_chmod_executor_reverifies_and_repairs_user_owned_directory(tmp_path: Path) -> None:
    target = tmp_path / "state"
    target.mkdir()
    os.chmod(target, 0o777)
    action = {"kind": "repair_permissions", "target": str(target), "command": f"chmod 700 {target}"}
    result = apply_action(action)
    assert result["status"] == "fixed"
    assert stat_mode(target) == 0o700


def stat_mode(path: Path) -> int:
    import stat as stat_module

    return stat_module.S_IMODE(path.stat().st_mode)


def test_chmod_executor_skips_when_target_changed_underneath(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    link = tmp_path / "sneaky"
    link.symlink_to(outside)
    action = {"kind": "repair_permissions", "target": str(link), "command": f"chmod 700 {link}"}
    result = apply_action(action)
    assert result["status"] == "skipped"
    assert stat_mode(outside) == 0o700  # untouched: the symlink was never followed


def test_service_executors_use_systemctl_and_report_failure(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(command, cwd=None):
        calls.append(tuple(command))
        return completed(returncode=1, stderr="bus problem")

    action = {"kind": "start_service", "target": "dispatch.service", "command": "systemctl --user start dispatch.service"}
    result = apply_action(action, run=fake_run)
    assert result["status"] == "failed"
    assert "bus problem" in result["error"]
    assert calls == [("systemctl", "--user", "start", "dispatch.service")]


def test_summarize_counts_outcomes() -> None:
    results = [
        {"kind": "a", "status": "fixed"},
        {"kind": "b", "status": "failed"},
        {"kind": "c", "status": "skipped"},
    ]
    assert summarize(results) == {"fixed": 1, "failed": 1, "skipped": 1}


def test_json_fix_without_yes_is_plan_only(tmp_path: Path, capsys) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    loose = layout.dispatch_home / "cache"
    os.chmod(loose, 0o755)

    code = installer_main(["--dispatch-home", str(layout.dispatch_home), "--json", "doctor", "--fix"])
    out = capsys.readouterr().out
    assert code == 1
    payload = json.loads(out)
    kinds = [action["kind"] for action in payload["data"]["fix_plan"]]
    assert kinds == ["repair_permissions"]
    assert "fix_results" not in payload["data"]
    # Plan-only means no mutation happened.
    assert stat_mode(loose) == 0o755


def test_json_fix_with_yes_applies_and_reports(tmp_path: Path, capsys) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    loose = layout.dispatch_home / "cache"
    os.chmod(loose, 0o755)

    code = installer_main(["--dispatch-home", str(layout.dispatch_home), "--json", "doctor", "--fix", "--yes"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["data"]["fix_summary"] == {"fixed": 1, "failed": 0, "skipped": 0}
    assert payload["data"]["fix_results"][0]["kind"] == "repair_permissions"
    assert stat_mode(loose) == 0o700
    # Still exit 1: the fixture install is incomplete (no record/launcher/service),
    # but the permission problem itself was repaired and no longer reported.
    checks = payload["data"]["checks"]
    assert checks["cache"]["status"] == "ready"
    assert code == 1


def test_interactive_fix_declines_on_eof_and_never_mutates(tmp_path: Path, capsys) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    loose = layout.dispatch_home / "cache"
    os.chmod(loose, 0o755)

    # Non-TTY stdin under pytest capture → guidance line, zero mutation.
    code = installer_main(["--dispatch-home", str(layout.dispatch_home), "doctor", "--fix"])
    assert code == 1
    assert stat_mode(loose) == 0o755
    assert "requires an interactive terminal" in capsys.readouterr().out


def test_doctor_without_fix_flag_stays_read_only(tmp_path: Path, capsys) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    code = installer_main(["--dispatch-home", str(layout.dispatch_home), "doctor"])
    assert code == 1
    assert "Dispatch Doctor" in capsys.readouterr().out
