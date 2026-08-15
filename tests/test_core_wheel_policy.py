from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "packaging" / "runtime-package-plan.json"
VERIFIER = ROOT / "scripts" / "verify-core-wheel"


def _record(files: dict[str, bytes], record_name: str) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name in sorted(files):
        digest = base64.urlsafe_b64encode(hashlib.sha256(files[name]).digest()).rstrip(b"=").decode()
        writer.writerow((name, f"sha256={digest}", len(files[name])))
    writer.writerow((record_name, "", ""))
    return output.getvalue().encode()


def _make_wheel(
    path: Path,
    *,
    extra: dict[str, bytes] | None = None,
    mutate: str | None = None,
    extra_requires_dist: tuple[str, ...] = (),
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    distribution = plan["distributions"][0]
    version = distribution["version"]
    metadata_root = f"dispatch_core-{version}.dist-info"
    files = {
        entry["path"]: (ROOT / entry["source"]).read_bytes()
        for entry in distribution["files"]
    }
    if mutate is not None:
        files[mutate] += b"# unexpected mutation\n"
    requires_dist = "".join(
        f"Requires-Dist: {dependency}\n"
        for dependency in (
            *distribution["requires_dist"],
            *distribution["optional_requires_dist"],
            *extra_requires_dist,
        )
    )
    extras: set[str] = set()
    for dependency in distribution["optional_requires_dist"]:
        match = re.search(r'; extra == "([A-Za-z0-9_.-]+)"$', dependency)
        if match is not None:
            extras.add(match.group(1))
    provides_extra = "".join(f"Provides-Extra: {extra}\n" for extra in sorted(extras))
    files.update(
        {
            f"{metadata_root}/METADATA": (
                "Metadata-Version: 2.4\n"
                f"Name: {distribution['name']}\n"
                f"Version: {version}\n"
                f"Requires-Python: {plan['python_requires']}\n"
                f"{provides_extra}"
                f"{requires_dist}\n"
            ).encode(),
            f"{metadata_root}/WHEEL": (
                "Wheel-Version: 1.0\nGenerator: dispatch-policy-test\n"
                "Root-Is-Purelib: true\nTag: py3-none-any\n"
            ).encode(),
            f"{metadata_root}/entry_points.txt": (
                "[console_scripts]\n"
                + "".join(
                    f"{name} = {target}\n"
                    for name, target in distribution["console_scripts"].items()
                )
            ).encode(),
            f"{metadata_root}/top_level.txt": (
                "\n".join(distribution["top_level"]) + "\n"
            ).encode(),
        }
    )
    files.update(extra or {})
    record_name = f"{metadata_root}/RECORD"
    files[record_name] = _record(files, record_name)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(name, data)
    return path


def _verify(wheel: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFIER), str(wheel), "--json"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def test_exact_core_wheel_closure_is_accepted(tmp_path: Path) -> None:
    wheel = _make_wheel(tmp_path / "dispatch_core-1.0.0-py3-none-any.whl")
    result = _verify(wheel)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["status"] == "verified_core_only"


def test_plugin_bytes_inside_core_namespace_are_rejected(tmp_path: Path) -> None:
    wheel = _make_wheel(
        tmp_path / "dispatch_core-1.0.0-py3-none-any.whl",
        extra={"dispatch_core/plugins/handbook.py": b"PLUGIN = True\n"},
    )
    result = _verify(wheel)
    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "wheel_member_closure_mismatch"


def test_unplanned_dist_info_and_mutated_core_bytes_are_rejected(tmp_path: Path) -> None:
    extra_wheel = _make_wheel(
        tmp_path / "extra" / "dispatch_core-1.0.0-py3-none-any.whl",
        extra={"dispatch_core-1.0.0.dist-info/plugin-manifest.json": b"{}\n"},
    )
    extra_result = _verify(extra_wheel)
    assert extra_result.returncode == 1
    assert json.loads(extra_result.stdout)["error"]["code"] == "wheel_member_closure_mismatch"

    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    planned_name = plan["distributions"][0]["files"][0]["path"]
    mutated_wheel = _make_wheel(
        tmp_path / "mutated" / "dispatch_core-1.0.0-py3-none-any.whl",
        mutate=planned_name,
    )
    mutated_result = _verify(mutated_wheel)
    assert mutated_result.returncode == 1
    assert json.loads(mutated_result.stdout)["error"]["code"] == "wheel_package_digest_mismatch"


def test_active_dependency_cannot_hide_behind_extra_marker(tmp_path: Path) -> None:
    wheel = _make_wheel(
        tmp_path / "dispatch_core-1.0.0-py3-none-any.whl",
        extra_requires_dist=(
            'dispatch-local-handbook==0.1.0; extra == "dev" or python_version >= "3.11"',
        ),
    )

    result = _verify(wheel)

    assert result.returncode == 1
    assert json.loads(result.stdout)["error"]["code"] == "wheel_metadata_mismatch"
