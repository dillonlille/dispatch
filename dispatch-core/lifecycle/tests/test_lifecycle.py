from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run(script: str, *, env: dict[str, str] | None = None, args: list[str] | None = None):
    merged = os.environ.copy()
    merged["PYTHONDONTWRITEBYTECODE"] = "1"
    if env:
        merged.update(env)
    return subprocess.run(
        [str(ROOT / "scripts" / script), *(args or [])],
        check=False,
        capture_output=True,
        text=True,
        env=merged,
    )


def test_build_is_deterministic_and_release_verifies(tmp_path: Path) -> None:
    environment = {"DISPATCH_BUILD_OUTPUT": str(tmp_path / "releases")}
    first = run("build", env=environment)
    second = run("build", env=environment)
    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    first_payload = json.loads(first.stdout)
    second_payload = json.loads(second.stdout)
    assert first_payload["data"]["release_id"] == second_payload["data"]["release_id"]
    assert first_payload["data"]["reused"] is False
    assert second_payload["data"]["reused"] is True

    release = first_payload["data"]["path"]
    verified = run("verify", args=[release])
    assert verified.returncode == 0, verified.stdout + verified.stderr
    verify_payload = json.loads(verified.stdout)
    assert verify_payload["ok"] is True
    assert verify_payload["data"]["release"]["release_id"] == first_payload["data"]["release_id"]


def test_health_is_non_mutating_and_uses_standard_envelope() -> None:
    before = {
        path.relative_to(ROOT).as_posix(): path.stat().st_mtime_ns
        for path in ROOT.rglob("*")
        if path.is_file()
    }
    completed = run("health")
    after = {
        path.relative_to(ROOT).as_posix(): path.stat().st_mtime_ns
        for path in ROOT.rglob("*")
        if path.is_file()
    }
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payload = json.loads(completed.stdout)
    assert set(payload) == {
        "ok",
        "action",
        "status",
        "data",
        "freshness",
        "delivery",
        "error",
    }
    assert payload["action"] == "health"
    assert payload["data"]["planes"]["overall"] == "setup_incomplete"
    assert payload["data"]["browser_manager"]["ready"] is False
    assert before == after


def test_health_rejects_relative_private_roots() -> None:
    completed = run("health", env={"DISPATCH_DATA_ROOT": "relative/private-data"})
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["ok"] is False
    assert payload["status"] == "degraded"
    assert payload["data"]["planes"]["configuration"] == "unavailable"
    assert payload["error"]["code"] == "invalid_path_configuration"
