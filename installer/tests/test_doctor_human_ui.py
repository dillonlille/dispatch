"""Human-facing `dispatch doctor` rendering and enriched payload contracts."""
from __future__ import annotations

import json
from pathlib import Path

from dispatch_installer.cli import main as installer_main
from dispatch_installer import doctor_render
from dispatch_installer.doctor import inspect_installation
from dispatch_installer.layout import InstallLayout


def make_layout(tmp_path: Path) -> InstallLayout:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return InstallLayout.from_environment({"HOME": str(home)})


def ready_checks() -> dict[str, dict]:
    checks: dict[str, dict] = {
        name: {"status": "ready", "path": f"/users/demo/.dispatch/{name}"}
        for name in ("dispatch_home", "config", "secrets", "data", "state", "cache", "logs", "run")
    }
    checks["clone"] = {"status": "ready", "path": "/users/demo/.dispatch/dispatch", "git": "ready"}
    checks["venv"] = {"status": "ready", "path": "/users/demo/.dispatch/venv", "python": "ready"}
    checks["installation"] = {
        "status": "ready",
        "channel": "dev",
        "ref": "main",
        "commit": "11ff802d0b10b163ea25e7db20db127824361bec",
    }
    checks["command"] = {"status": "ready", "command": "/users/demo/.local/bin/dispatch"}
    checks["service"] = {
        "status": "ready",
        "service": "dispatch.service",
        "unit": "/users/demo/.config/systemd/user/dispatch.service",
        "active": True,
        "enabled": True,
    }
    checks["plugins"] = {"status": "ready", "path": "/users/demo/.dispatch/config/plugins.json"}
    checks["plugin_services"] = {"status": "ready", "services": {}}
    return checks


def test_ready_report_renders_one_line_per_group() -> None:
    report = {"ok": True, "status": "ready", "checks": ready_checks()}
    rendered = doctor_render.render_doctor(report)
    assert "✓ Installation" in rendered
    assert "dev @ main (11ff802d)" in rendered
    assert "10 roots owned & private" in rendered
    assert "Installation ready" in rendered
    assert "try:" not in rendered


def test_staged_plugin_services_are_informational_not_healthy() -> None:
    checks = ready_checks()
    checks["plugin_services"] = {
        "status": "ready",
        "services": {"companion-bridge": {"status": "prepared", "active": False, "enabled": False}},
    }
    rendered = doctor_render.render_doctor({"ok": True, "status": "ready", "checks": checks})
    assert "● Plugins" in rendered
    assert "services staged, not running: companion-bridge" in rendered


def test_degraded_report_defers_to_installer_and_collapses_directories() -> None:
    checks: dict[str, dict] = {
        name: {
            "status": "missing",
            "path": f"/users/demo/.dispatch/{name}",
            "reason": "directory has not been created",
            "hint": "dispatch repair",
        }
        for name in ("dispatch_home", "clone", "venv", "config", "secrets", "data", "state", "cache", "logs", "run")
    }
    checks["venv"].update({"python": "missing"})
    checks["clone"].update({"git": "missing"})
    checks["installation"] = {
        "status": "missing",
        "channel": None,
        "ref": None,
        "commit": None,
        "reason": "no installation record was found",
    }
    checks["command"] = {"status": "missing", "command": "/users/demo/.local/bin/dispatch"}
    checks["service"] = {"status": "missing", "service": "dispatch.service"}
    checks["plugins"] = {
        "status": "not_configured",
        "path": "/users/demo/.dispatch/config/plugins.json",
        "reason": "no plugin configuration exists yet",
        "hint": "dispatch setup",
    }
    checks["plugin_services"] = {"status": "ready", "services": {}}
    report = {"ok": False, "status": "incomplete", "checks": checks}
    rendered = doctor_render.render_doctor(report)

    # Before an installation exists, repair/update hints are wrong advice.
    assert rendered.count("run the Dispatch installer to complete setup") == 6
    assert "dispatch repair" not in rendered
    # Ten failing roots collapse into a single grouped note.
    assert "directory roots: 10 not created" in rendered
    assert rendered.count("Directories") == 1
    assert "Installation incomplete" in rendered


def test_broken_report_names_reasons_with_copy_pasteable_remedies() -> None:
    checks = ready_checks()
    checks["state"] = {
        "status": "unsafe",
        "path": "/users/demo/.dispatch/state",
        "reason": "permissions are too open (drwxrwxrwx)",
        "hint": "chmod 700 '/users/demo/.dispatch/state'",
    }
    checks["clone"]["git"] = "unsafe"
    checks["venv"].update(
        {
            "python": "unsafe",
            "python_reason": "interpreter failed ownership or executability checks",
            "python_hint": "dispatch repair",
        }
    )
    checks["service"].update({"status": "incomplete", "active": False})
    report = {"ok": False, "status": "unsafe", "checks": checks}
    rendered = doctor_render.render_doctor(report)

    assert "✗ Code" in rendered
    assert "the checkout does not match the installation record" in rendered
    assert "try: dispatch update" in rendered
    assert "✗ Runtime" in rendered
    assert "interpreter failed ownership or executability checks" in rendered
    assert "try: dispatch repair" in rendered
    assert "⚠ Service" in rendered
    assert "try: systemctl --user start dispatch.service  ·  or: dispatch repair" in rendered
    assert "✗ Directories" in rendered
    assert "try: chmod 700 '/users/demo/.dispatch/state'" in rendered
    assert "Installation needs attention · 3 problems, 1 warning" in rendered


def test_long_reasons_are_elided_but_remedies_stay_copy_pasteable(monkeypatch) -> None:
    checks = ready_checks()
    long_reason = "this reason line is deliberately far longer than any narrow terminal row"
    checks["clone"]["git"] = "unsafe"
    checks["clone"]["reason"] = long_reason
    report = {"ok": False, "status": "unsafe", "checks": checks}

    monkeypatch.setattr(doctor_render, "terminal_width", lambda default=72: 40)
    rendered = doctor_render.render_doctor(report)

    assert long_reason not in rendered  # details may be elided…
    assert "…" in rendered  # …but visibly marked as truncated
    assert "try: dispatch update" in rendered  # remedies never are


def test_directory_payload_carries_reason_and_hint(tmp_path: Path) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    (layout.dispatch_home / "cache").chmod(0o755)
    (layout.dispatch_home / "logs").rmdir()

    report = inspect_installation(layout)
    cache = report["checks"]["cache"]
    assert cache["status"] == "unsafe"
    assert "too open" in str(cache["reason"])
    assert str(cache["hint"]).startswith("chmod 700 ")

    logs = report["checks"]["logs"]
    assert logs["status"] == "missing"
    assert str(logs["hint"]) == "dispatch repair"

    healthy = report["checks"]["config"]
    assert healthy["status"] == "ready"
    assert "reason" not in healthy


def test_doctor_json_envelope_is_single_line_and_structured(tmp_path: Path, capsys) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    code = installer_main(["--dispatch-home", str(layout.dispatch_home), "--json", "doctor"])
    out = capsys.readouterr().out

    assert code == 1  # nothing activated yet
    assert out.count("\n") == 1  # exactly one JSON line, byte-stable shape
    payload = json.loads(out)
    assert payload["action"] == "doctor"
    assert payload["ok"] is False
    assert payload["error"] is None
    checks = payload["data"]["checks"]
    assert checks["dispatch_home"]["status"] == "ready"
    assert checks["installation"]["status"] == "missing"


def test_doctor_without_json_flag_prints_human_report(tmp_path: Path, capsys) -> None:
    layout = make_layout(tmp_path)
    layout.prepare()
    code = installer_main(["--dispatch-home", str(layout.dispatch_home), "doctor"])
    out = capsys.readouterr().out

    assert code == 1
    assert "Dispatch Doctor" in out
    assert not out.lstrip().startswith("{")
    assert json.loads('{"sentinel": true}')  # sanity: json module usable
    assert "machine detail: dispatch --json doctor" in out
