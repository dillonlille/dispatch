#!/usr/bin/env python3
"""Shared release-version preparation and readiness checks for Dispatch."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Sequence

SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
PRODUCT_PATTERN = re.compile(r"^PRODUCT_VERSION='([^']+)'$", re.MULTILINE)
VERSION_LINE = re.compile(r'^(version\s*=\s*)"[^"]+"$', re.MULTILINE)
PYTHON_VERSION = re.compile(r'^(__version__\s*=\s*)"[^"]+"$', re.MULTILINE)

INSTALLER_PROJECT = "installer/pyproject.toml"
INSTALLER_INIT = "installer/src/dispatch_installer/__init__.py"
INSTALLER_PLAN = "packaging/installer-package-plan.json"
CORE_PROJECT = "dispatch-core/pyproject.toml"
CORE_INIT = "dispatch-core/src/dispatch_core/__init__.py"
CORE_MANIFEST = "dispatch-core/core-manifest.json"
RUNTIME_PLAN = "packaging/runtime-package-plan.json"
RELEASE_MANIFEST = "packaging/installation-release-manifest.json"
PUBLIC_BOOTSTRAP = "installer/deploy/cloudflare/public/install.sh"
PUBLIC_SCOPE = "policy/public-source-scope.json"


class ReleaseVersionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ReleaseArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise ReleaseVersionError("invalid_arguments", message)


@dataclass(frozen=True)
class Versions:
    product: str
    installer: str
    core: str

    def as_dict(self) -> dict[str, str]:
        return {"product": self.product, "installer": self.installer, "core": self.core}


def envelope(
    *,
    ok: bool,
    action: str,
    status: str,
    data: dict[str, Any],
    error: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "action": action,
        "status": status,
        "data": data,
        "freshness": None,
        "delivery": None,
        "error": error,
    }


def _emit(payload: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, sort_keys=True))
        return
    if payload["ok"] is not True:
        print(f"Release preparation is not ready: {payload['error']['message']}", file=sys.stderr)
        issues = payload.get("data", {}).get("issues", [])
        for issue in issues[1:]:
            print(f"- {issue}", file=sys.stderr)
        return
    data = payload["data"]
    if payload["action"] == "prepare-release":
        print("Dispatch release preparation")
        print()
        print(f"Mode: {'Apply changes' if data['applied'] else 'Preview only'}")
        print(f"Published product: {data['baseline']['product']}")
        print(f"Proposed product: {data['proposed']['product']}")
        print(f"Installer: {data['baseline']['installer']} -> {data['proposed']['installer']}")
        print(f"Core: {data['baseline']['core']} -> {data['proposed']['core']}")
        print()
        print(f"Installer source changed: {'Yes' if data['changed_components']['installer'] else 'No'}")
        print(f"Core source changed: {'Yes' if data['changed_components']['core'] else 'No'}")
        print()
        if data["applied"]:
            print("Version metadata was prepared on dev. Nothing was tagged, published, or promoted.")
        else:
            print("No files were changed. Run again with --apply after approving these versions.")
        return
    print("Dispatch release readiness")
    print()
    print(f"Product: {data['current']['product']}")
    print(f"Installer: {data['current']['installer']}")
    print(f"Core: {data['current']['core']}")
    print(f"Baseline tag: {data['baseline_ref']}")
    print("Status: Ready for candidate build and acceptance")


def _json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseVersionError("invalid_metadata", f"cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReleaseVersionError("invalid_metadata", f"{path} must contain a JSON object")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _toml_version(content: str, path: str) -> str:
    try:
        payload = tomllib.loads(content)
        value = payload["project"]["version"]
    except (tomllib.TOMLDecodeError, KeyError, TypeError) as exc:
        raise ReleaseVersionError("invalid_metadata", f"{path} has invalid project version metadata") from exc
    return _validated_version(str(value), path)


def _python_version(content: str, path: str) -> str:
    match = re.search(r'^__version__\s*=\s*"([^"]+)"$', content, re.MULTILINE)
    if match is None:
        raise ReleaseVersionError("invalid_metadata", f"{path} has no canonical __version__ assignment")
    return _validated_version(match.group(1), path)


def _validated_version(value: str, label: str) -> str:
    if SEMVER.fullmatch(value) is None:
        raise ReleaseVersionError("invalid_version", f"{label} must be a three-part semantic version")
    return value


def _version_key(value: str) -> tuple[int, int, int]:
    _validated_version(value, "version")
    return tuple(int(part) for part in value.split("."))  # type: ignore[return-value]


def _git(root: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise ReleaseVersionError("git_unavailable", "git is required for release preparation") from exc
    if check and result.returncode != 0:
        message = (result.stderr or result.stdout).strip() or "git command failed"
        raise ReleaseVersionError("git_failed", message[:512])
    return result


def _git_bytes(root: Path, ref: str, relative: str) -> bytes | None:
    result = subprocess.run(
        ["git", "show", f"{ref}:{relative}"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode == 0:
        return result.stdout
    if result.returncode in {1, 128}:
        return None
    raise ReleaseVersionError("git_failed", result.stderr.decode("utf-8", errors="replace")[:512])


def _git_json(root: Path, ref: str, relative: str) -> dict[str, Any]:
    content = _git_bytes(root, ref, relative)
    if content is None:
        raise ReleaseVersionError("baseline_missing", f"{relative} is missing from baseline {ref}")
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ReleaseVersionError("baseline_invalid", f"{relative} is invalid in baseline {ref}") from exc
    if not isinstance(payload, dict):
        raise ReleaseVersionError("baseline_invalid", f"{relative} is invalid in baseline {ref}")
    return payload


def current_versions(root: Path) -> Versions:
    manifest = _json(root / RELEASE_MANIFEST)
    try:
        product = _validated_version(str(manifest["product"]["version"]), RELEASE_MANIFEST)
        installer_manifest = _validated_version(str(manifest["installer"]["version"]), RELEASE_MANIFEST)
        core_manifest = _validated_version(str(manifest["core"]["version"]), RELEASE_MANIFEST)
    except (KeyError, TypeError) as exc:
        raise ReleaseVersionError("invalid_metadata", "release manifest version declarations are incomplete") from exc
    installer_project = _toml_version((root / INSTALLER_PROJECT).read_text(encoding="utf-8"), INSTALLER_PROJECT)
    installer_init = _python_version((root / INSTALLER_INIT).read_text(encoding="utf-8"), INSTALLER_INIT)
    installer_plan = _json(root / INSTALLER_PLAN)
    core_project = _toml_version((root / CORE_PROJECT).read_text(encoding="utf-8"), CORE_PROJECT)
    core_init = _python_version((root / CORE_INIT).read_text(encoding="utf-8"), CORE_INIT)
    core_identity = _validated_version(str(_json(root / CORE_MANIFEST).get("version")), CORE_MANIFEST)
    runtime = _json(root / RUNTIME_PLAN)
    try:
        runtime_core = next(item for item in runtime["distributions"] if item["name"] == "dispatch-core")
        runtime_version = _validated_version(str(runtime_core["version"]), RUNTIME_PLAN)
        installer_plan_version = _validated_version(str(installer_plan["distribution"]["version"]), INSTALLER_PLAN)
    except (KeyError, TypeError, StopIteration) as exc:
        raise ReleaseVersionError("invalid_metadata", "component package plan versions are incomplete") from exc
    installer_values = {installer_manifest, installer_project, installer_init, installer_plan_version}
    core_values = {core_manifest, core_project, core_init, core_identity, runtime_version}
    if len(installer_values) != 1:
        raise ReleaseVersionError("version_mismatch", "installer version declarations do not match")
    if len(core_values) != 1:
        raise ReleaseVersionError("version_mismatch", "Core version declarations do not match")
    return Versions(product=product, installer=installer_values.pop(), core=core_values.pop())


def published_product_version(root: Path) -> str:
    try:
        content = (root / PUBLIC_BOOTSTRAP).read_text(encoding="utf-8")
    except OSError as exc:
        raise ReleaseVersionError("bootstrap_missing", "tracked production bootstrap is missing") from exc
    match = PRODUCT_PATTERN.search(content)
    if match is None:
        raise ReleaseVersionError("bootstrap_invalid", "tracked production bootstrap has no product version")
    return _validated_version(match.group(1), PUBLIC_BOOTSTRAP)


def baseline_versions(root: Path, ref: str) -> Versions:
    manifest = _git_json(root, ref, RELEASE_MANIFEST)
    try:
        return Versions(
            product=_validated_version(str(manifest["product"]["version"]), ref),
            installer=_validated_version(str(manifest["installer"]["version"]), ref),
            core=_validated_version(str(manifest["core"]["version"]), ref),
        )
    except (KeyError, TypeError) as exc:
        raise ReleaseVersionError("baseline_invalid", f"baseline {ref} has incomplete version metadata") from exc


def _normalized(relative: str, content: bytes, version: str) -> bytes:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return content
    if relative in {INSTALLER_PROJECT, CORE_PROJECT}:
        text, count = VERSION_LINE.subn(r'\1"<VERSION>"', text)
        if count != 1:
            raise ReleaseVersionError("invalid_metadata", f"{relative} has no unique project version")
    elif relative in {INSTALLER_INIT, CORE_INIT}:
        text, count = PYTHON_VERSION.subn(r'\1"<VERSION>"', text)
        if count != 1:
            raise ReleaseVersionError("invalid_metadata", f"{relative} has no unique __version__")
    else:
        return content
    if version not in content.decode("utf-8"):
        raise ReleaseVersionError("version_mismatch", f"{relative} does not contain expected version {version}")
    return text.encode("utf-8")


def _component_paths(root: Path, ref: str | None, component: str) -> set[str]:
    if component == "installer":
        plan = _json(root / INSTALLER_PLAN) if ref is None else _git_json(root, ref, INSTALLER_PLAN)
        try:
            return {str(item["path"]) for item in plan["files"]}
        except (KeyError, TypeError) as exc:
            raise ReleaseVersionError("invalid_metadata", "installer package plan files are invalid") from exc
    plan = _json(root / RUNTIME_PLAN) if ref is None else _git_json(root, ref, RUNTIME_PLAN)
    try:
        distribution = next(item for item in plan["distributions"] if item["name"] == "dispatch-core")
        return {CORE_PROJECT, *(str(item["source"]) for item in distribution["files"])}
    except (KeyError, TypeError, StopIteration) as exc:
        raise ReleaseVersionError("invalid_metadata", "Core runtime package plan files are invalid") from exc


def component_changed(root: Path, baseline_ref: str, component: str, baseline_version: str, current_version: str) -> bool:
    paths = _component_paths(root, None, component) | _component_paths(root, baseline_ref, component)
    for relative in sorted(paths):
        current_path = root / relative
        current = current_path.read_bytes() if current_path.is_file() and not current_path.is_symlink() else None
        baseline = _git_bytes(root, baseline_ref, relative)
        if current is None or baseline is None:
            return True
        if _normalized(relative, current, current_version) != _normalized(relative, baseline, baseline_version):
            return True
    return False


def analyze(
    root: Path,
    proposed: Versions | None = None,
    baseline_ref: str | None = None,
    *,
    reject_existing_tag: bool = False,
) -> dict[str, Any]:
    current = current_versions(root)
    published = published_product_version(root)
    ref = baseline_ref or published
    baseline = baseline_versions(root, ref)
    if baseline.product != published:
        raise ReleaseVersionError("baseline_mismatch", "production bootstrap and baseline tag identify different products")
    changed = {
        "installer": component_changed(root, ref, "installer", baseline.installer, current.installer),
        "core": component_changed(root, ref, "core", baseline.core, current.core),
    }
    candidate = proposed or current
    issues: list[str] = []
    if _version_key(candidate.product) <= _version_key(baseline.product):
        issues.append(f"product version must be newer than published {baseline.product}")
    for component in ("installer", "core"):
        baseline_value = getattr(baseline, component)
        candidate_value = getattr(candidate, component)
        if changed[component] and _version_key(candidate_value) <= _version_key(baseline_value):
            issues.append(f"{component} changed and must be newer than {baseline_value}")
        if not changed[component] and candidate_value != baseline_value:
            issues.append(f"{component} did not change and must remain {baseline_value}")
    tag = _git(root, "show-ref", "--verify", f"refs/tags/{candidate.product}", check=False)
    if reject_existing_tag and candidate.product != baseline.product and tag.returncode == 0:
        issues.append(f"tag {candidate.product} already exists")
    return {
        "baseline_ref": ref,
        "baseline": baseline.as_dict(),
        "current": current.as_dict(),
        "proposed": candidate.as_dict(),
        "changed_components": changed,
        "issues": issues,
    }


def _replace_text_version(path: Path, pattern: re.Pattern[str], new_version: str, label: str) -> None:
    content = path.read_text(encoding="utf-8")
    replacement = r'\1"' + new_version + '"'
    updated, count = pattern.subn(replacement, content)
    if count != 1:
        raise ReleaseVersionError("invalid_metadata", f"{label} has no unique version assignment")
    path.write_text(updated, encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _refresh_metadata(root: Path, versions: Versions) -> list[str]:
    changed: set[str] = set()
    installer_plan = _json(root / INSTALLER_PLAN)
    installer_plan["distribution"]["version"] = versions.installer
    installer_plan.pop("production_install_ready", None)
    for item in installer_plan["files"]:
        source = root / item["path"]
        item["sha256"] = _sha256(source)
        item["size"] = source.stat().st_size
    _write_json(root / INSTALLER_PLAN, installer_plan)
    changed.add(INSTALLER_PLAN)

    runtime = _json(root / RUNTIME_PLAN)
    core_plan = next(item for item in runtime["distributions"] if item["name"] == "dispatch-core")
    core_plan["version"] = versions.core
    for item in core_plan["files"]:
        item["sha256"] = _sha256(root / item["source"])
    core_plan["license_file"]["sha256"] = _sha256(root / core_plan["license_file"]["source"])
    _write_json(root / RUNTIME_PLAN, runtime)
    changed.add(RUNTIME_PLAN)

    manifest = _json(root / RELEASE_MANIFEST)
    manifest["product"]["version"] = versions.product
    manifest["installer"]["version"] = versions.installer
    manifest["core"]["version"] = versions.core
    manifest["ready"] = False
    manifest["installer"]["artifact"] = {"url": None, "size": None, "sha256": None}
    manifest["core"]["artifact"] = {"url": None, "size": None, "sha256": None}
    manifest["core"]["package_files"] = [
        {"path": item["path"], "sha256": item["sha256"]} for item in core_plan["files"]
    ]
    manifest["core"]["requires_dist"] = [*core_plan["requires_dist"], *core_plan["optional_requires_dist"]]
    _write_json(root / RELEASE_MANIFEST, manifest)
    changed.add(RELEASE_MANIFEST)
    return sorted(changed)


def _refresh_public_scope(root: Path) -> None:
    result = _git(root, "ls-files", "-z")
    files: dict[str, dict[str, str]] = {}
    for relative in sorted(value for value in result.stdout.split("\0") if value and value != PUBLIC_SCOPE):
        path = root / relative
        if path.is_symlink():
            files[relative] = {"kind": "symlink", "origin": "generated_public", "target": os.readlink(path)}
        elif path.is_file():
            files[relative] = {"kind": "file", "origin": "generated_public", "sha256": _sha256(path)}
        else:
            raise ReleaseVersionError("source_scope", f"tracked source path is missing: {relative}")
    _write_json(root / PUBLIC_SCOPE, {"schema_version": 1, "files": files})


def apply_versions(root: Path, versions: Versions) -> list[str]:
    branch = _git(root, "branch", "--show-current").stdout.strip()
    if branch != "dev":
        raise ReleaseVersionError("wrong_branch", "release preparation must run on dev")
    if _git(root, "status", "--porcelain", "--untracked-files=all").stdout:
        raise ReleaseVersionError("dirty_worktree", "release preparation requires a clean worktree")
    current = current_versions(root)
    _replace_text_version(root / INSTALLER_PROJECT, VERSION_LINE, versions.installer, INSTALLER_PROJECT)
    _replace_text_version(root / INSTALLER_INIT, PYTHON_VERSION, versions.installer, INSTALLER_INIT)
    _replace_text_version(root / CORE_PROJECT, VERSION_LINE, versions.core, CORE_PROJECT)
    _replace_text_version(root / CORE_INIT, PYTHON_VERSION, versions.core, CORE_INIT)
    core_manifest = _json(root / CORE_MANIFEST)
    core_manifest["version"] = versions.core
    _write_json(root / CORE_MANIFEST, core_manifest)
    changed = {
        INSTALLER_PROJECT,
        INSTALLER_INIT,
        CORE_PROJECT,
        CORE_INIT,
        CORE_MANIFEST,
        *_refresh_metadata(root, versions),
    }
    _refresh_public_scope(root)
    changed.add(PUBLIC_SCOPE)
    observed = current_versions(root)
    if observed != versions:
        raise ReleaseVersionError("write_verification", "written version metadata did not verify")
    if current == versions:
        raise ReleaseVersionError("no_change", "proposed versions are already present")
    return sorted(changed)


def consistency_issues(root: Path) -> list[str]:
    issues: list[str] = []
    manifest = _json(root / RELEASE_MANIFEST)
    if manifest.get("ready") is not False:
        issues.append("source release manifest must remain an unfinalized draft")
    for component in ("installer", "core"):
        if manifest.get(component, {}).get("artifact") != {"url": None, "size": None, "sha256": None}:
            issues.append(f"{component} artifact identity must be empty before finalization")
    installer_plan = _json(root / INSTALLER_PLAN)
    if "production_install_ready" in installer_plan:
        issues.append("source package plan must not carry mutable production approval state")
    for item in installer_plan.get("files", []):
        source = root / str(item.get("path"))
        if not source.is_file() or source.is_symlink():
            issues.append(f"installer source is missing or unsafe: {item.get('path')}")
            continue
        if item.get("sha256") != _sha256(source) or item.get("size") != source.stat().st_size:
            issues.append(f"installer package metadata is stale: {item.get('path')}")
    runtime = _json(root / RUNTIME_PLAN)
    try:
        core_plan = next(item for item in runtime["distributions"] if item["name"] == "dispatch-core")
    except (KeyError, TypeError, StopIteration):
        return [*issues, "Core runtime plan is missing"]
    for item in core_plan.get("files", []):
        source = root / str(item.get("source"))
        if not source.is_file() or source.is_symlink() or item.get("sha256") != _sha256(source):
            issues.append(f"Core runtime metadata is stale: {item.get('source')}")
    expected_files = [{"path": item["path"], "sha256": item["sha256"]} for item in core_plan["files"]]
    if manifest.get("core", {}).get("package_files") != expected_files:
        issues.append("release manifest Core package files differ from the runtime plan")
    return issues


def prepare_main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in arguments
    parser = ReleaseArgumentParser(description="Prepare consistent Dispatch product and component versions")
    parser.add_argument("--product-version", required=True)
    parser.add_argument("--installer-version", required=True)
    parser.add_argument("--core-version", required=True)
    parser.add_argument("--baseline-ref")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help=argparse.SUPPRESS)
    if json_output and any(value in arguments for value in ("-h", "--help")):
        _emit(envelope(ok=True, action="prepare-release", status="help", data={"help": parser.format_help()}), json_output=True)
        return 0
    try:
        args = parser.parse_args(arguments)
        root = args.root.resolve()
        proposed = Versions(
            product=_validated_version(args.product_version, "product version"),
            installer=_validated_version(args.installer_version, "installer version"),
            core=_validated_version(args.core_version, "Core version"),
        )
        analysis = analyze(root, proposed, args.baseline_ref, reject_existing_tag=True)
        if analysis["issues"]:
            raise ReleaseVersionError("invalid_version_plan", str(analysis["issues"][0]))
        updated = apply_versions(root, proposed) if args.apply else []
        payload = envelope(
            ok=True,
            action="prepare-release",
            status="prepared" if args.apply else "planned",
            data={**analysis, "applied": bool(args.apply), "updated_paths": updated},
        )
    except ReleaseVersionError as exc:
        payload = envelope(
            ok=False,
            action="prepare-release",
            status="error",
            data={"issues": [str(exc)]},
            error={"code": exc.code, "message": str(exc)},
        )
        _emit(payload, json_output=json_output)
        return 2 if exc.code == "invalid_arguments" else 1
    _emit(payload, json_output=bool(args.json))
    return 0


def verify_main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in arguments
    parser = ReleaseArgumentParser(description="Verify that Dispatch version metadata is ready for release acceptance")
    parser.add_argument("--baseline-ref")
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1], help=argparse.SUPPRESS)
    if json_output and any(value in arguments for value in ("-h", "--help")):
        _emit(envelope(ok=True, action="verify-release-readiness", status="help", data={"help": parser.format_help()}), json_output=True)
        return 0
    try:
        args = parser.parse_args(arguments)
        root = args.root.resolve()
        analysis = analyze(root, baseline_ref=args.baseline_ref)
        issues = [*analysis["issues"], *consistency_issues(root)]
        if args.require_clean and _git(root, "status", "--porcelain", "--untracked-files=all").stdout:
            issues.append("release readiness requires a clean worktree")
        data = {**analysis, "issues": issues, "worktree_clean": not bool(_git(root, "status", "--porcelain", "--untracked-files=all").stdout)}
        if issues:
            payload = envelope(
                ok=False,
                action="verify-release-readiness",
                status="not_ready",
                data=data,
                error={"code": "release_not_ready", "message": issues[0]},
            )
            _emit(payload, json_output=bool(args.json))
            return 1
        payload = envelope(ok=True, action="verify-release-readiness", status="ready", data=data)
    except ReleaseVersionError as exc:
        payload = envelope(
            ok=False,
            action="verify-release-readiness",
            status="error",
            data={"issues": [str(exc)]},
            error={"code": exc.code, "message": str(exc)},
        )
        _emit(payload, json_output=json_output)
        return 2 if exc.code == "invalid_arguments" else 1
    _emit(payload, json_output=bool(args.json))
    return 0
