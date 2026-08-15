from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
PREPARE = ROOT / "scripts" / "prepare-release"
VERIFY = ROOT / "scripts" / "verify-release-readiness"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", *arguments], cwd=root, check=True, capture_output=True, text=True)


def _fixture(tmp_path: Path, *, core_changed: bool = True) -> Path:
    root = tmp_path / "repository"
    root.mkdir()
    _write(root / "installer/pyproject.toml", '[project]\nname = "dispatch-installer"\nversion = "0.1.5"\n')
    _write(root / "installer/src/dispatch_installer/__init__.py", '__version__ = "0.1.5"\n')
    _write(root / "installer/src/dispatch_installer/module.py", "VALUE = 1\n")
    _write(root / "installer/LICENSE", "synthetic license\n")
    _write(root / "dispatch-core/pyproject.toml", '[project]\nname = "dispatch-core"\nversion = "1.0.0"\n')
    _write(root / "dispatch-core/src/dispatch_core/__init__.py", '__version__ = "1.0.0"\n')
    _write(root / "dispatch-core/src/dispatch_core/module.py", "VALUE = 1\n")
    _write(root / "dispatch-core/LICENSE", "synthetic license\n")
    _write(
        root / "dispatch-core/core-manifest.json",
        json.dumps({"schema_version": 1, "id": "dispatch-core", "version": "1.0.0", "features": []}) + "\n",
    )
    installer_files = [
        "installer/pyproject.toml",
        "installer/src/dispatch_installer/__init__.py",
        "installer/src/dispatch_installer/module.py",
        "installer/LICENSE",
    ]
    installer_plan = {
        "schema_version": 1,
        "online_only": True,
        "production_install_ready": True,
        "distribution": {"name": "dispatch-installer", "version": "0.1.5"},
        "files": [
            {"path": relative, "sha256": _digest(root / relative), "size": (root / relative).stat().st_size}
            for relative in installer_files
        ],
    }
    _write(root / "packaging/installer-package-plan.json", json.dumps(installer_plan) + "\n")
    core_files = [
        {"path": "dispatch_core/__init__.py", "source": "dispatch-core/src/dispatch_core/__init__.py"},
        {"path": "dispatch_core/module.py", "source": "dispatch-core/src/dispatch_core/module.py"},
    ]
    for item in core_files:
        item["sha256"] = _digest(root / item["source"])
    runtime = {
        "schema_version": 3,
        "distributions": [
            {
                "name": "dispatch-core",
                "version": "1.0.0",
                "files": core_files,
                "requires_dist": [],
                "optional_requires_dist": [],
                "license_file": {"source": "dispatch-core/LICENSE", "sha256": _digest(root / "dispatch-core/LICENSE")},
            }
        ],
    }
    _write(root / "packaging/runtime-package-plan.json", json.dumps(runtime) + "\n")
    manifest = {
        "schema_version": 1,
        "ready": False,
        "product": {"name": "dispatch", "version": "0.0.7"},
        "installer": {
            "name": "dispatch-installer",
            "version": "0.1.5",
            "artifact": {"url": None, "size": None, "sha256": None},
        },
        "core": {
            "name": "dispatch-core",
            "version": "1.0.0",
            "artifact": {"url": None, "size": None, "sha256": None},
            "package_files": [{"path": item["path"], "sha256": item["sha256"]} for item in core_files],
            "requires_dist": [],
        },
    }
    _write(root / "packaging/installation-release-manifest.json", json.dumps(manifest) + "\n")
    _write(root / "installer/deploy/cloudflare/public/install.sh", "#!/bin/sh\nPRODUCT_VERSION='0.0.7'\n")
    _write(root / "policy/public-source-scope.json", '{"schema_version":1,"files":{}}\n')
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "tests@example.invalid")
    _git(root, "config", "user.name", "Dispatch Tests")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "baseline")
    _git(root, "tag", "0.0.7")
    _git(root, "checkout", "-b", "dev")
    _write(root / "installer/src/dispatch_installer/module.py", "VALUE = 2\n")
    if core_changed:
        _write(root / "dispatch-core/src/dispatch_core/module.py", "VALUE = 2\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "development changes")
    return root


def _invoke(script: Path, root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(script), *arguments, "--root", str(root), "--json"],
        check=False,
        capture_output=True,
        text=True,
    )


def test_prepare_release_preview_is_non_mutating(tmp_path: Path) -> None:
    root = _fixture(tmp_path)
    before = subprocess.run(
        ["git", "status", "--porcelain=v2", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    result = _invoke(
        PREPARE,
        root,
        "--product-version",
        "0.0.8",
        "--installer-version",
        "0.1.6",
        "--core-version",
        "1.0.1",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "planned"
    assert payload["data"]["applied"] is False
    assert payload["data"]["changed_components"] == {"installer": True, "core": True}
    after = subprocess.run(
        ["git", "status", "--porcelain=v2", "--untracked-files=all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert after == before == ""


def test_prepare_release_apply_updates_consistent_draft(tmp_path: Path) -> None:
    root = _fixture(tmp_path)

    prepared = _invoke(
        PREPARE,
        root,
        "--product-version",
        "0.0.8",
        "--installer-version",
        "0.1.6",
        "--core-version",
        "1.0.1",
        "--apply",
    )

    assert prepared.returncode == 0, prepared.stdout + prepared.stderr
    payload = json.loads(prepared.stdout)
    assert payload["status"] == "prepared"
    assert payload["data"]["applied"] is True
    plan = json.loads((root / "packaging/installer-package-plan.json").read_text(encoding="utf-8"))
    assert "production_install_ready" not in plan
    manifest = json.loads((root / "packaging/installation-release-manifest.json").read_text(encoding="utf-8"))
    assert manifest["product"]["version"] == "0.0.8"
    assert manifest["installer"]["version"] == "0.1.6"
    assert manifest["core"]["version"] == "1.0.1"
    assert manifest["ready"] is False
    assert manifest["installer"]["artifact"] == {"url": None, "size": None, "sha256": None}

    verified = _invoke(VERIFY, root)
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert json.loads(verified.stdout)["status"] == "ready"


def test_prepare_release_rejects_bump_for_unchanged_component(tmp_path: Path) -> None:
    root = _fixture(tmp_path, core_changed=False)

    result = _invoke(
        PREPARE,
        root,
        "--product-version",
        "0.0.8",
        "--installer-version",
        "0.1.6",
        "--core-version",
        "1.0.1",
    )

    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "invalid_version_plan"
    assert "core did not change and must remain 1.0.0" in payload["error"]["message"]


def test_json_parser_error_is_one_document(tmp_path: Path) -> None:
    root = _fixture(tmp_path)

    result = _invoke(PREPARE, root, "--unknown")

    assert result.returncode == 2
    assert result.stderr == ""
    assert json.loads(result.stdout)["error"]["code"] == "invalid_arguments"
