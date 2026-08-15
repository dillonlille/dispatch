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

sys.dont_write_bytecode = True

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
PREPARATION_OUTPUTS = {
    INSTALLER_PROJECT,
    INSTALLER_INIT,
    INSTALLER_PLAN,
    CORE_PROJECT,
    CORE_INIT,
    CORE_MANIFEST,
    RUNTIME_PLAN,
    RELEASE_MANIFEST,
    PUBLIC_SCOPE,
}
REQUIRED_ACCEPTANCE_CHECKS = {
    "literal_staged_install",
    "interrupted_install_recovery",
    "same_release_repair",
    "setup_incomplete_before_setup",
    "setup_ready_after_setup",
    "service_restart_persistence",
    "reboot_persistence",
    "corrupt_artifact_rejected",
    "keep_data_uninstall",
    "reinstall_preserved_data",
    "confirmed_purge_data",
    "path_command_ready",
    "command_collision_rejected",
    "command_uninstall_removed",
}


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
        "schema_version": 1,
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
        print(f"BLOCKED: {payload['error']['message']}", file=sys.stderr)
        issues = payload.get("data", {}).get("issues", [])
        for issue in issues[1:]:
            print(f"- {issue}", file=sys.stderr)
        return
    data = payload["data"]
    if payload["action"] == "prepare-release":
        print(f"{'PREPARED' if data['applied'] else 'PLANNED'}: Dispatch release preparation")
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
    print("READY: Dispatch release readiness")
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


def _product_version_from_bytes(content: bytes) -> str | None:
    try:
        match = PRODUCT_PATTERN.search(content.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    return match.group(1) if match is not None else None


def _exact_ref(root: Path, ref: str) -> tuple[str, str]:
    if SEMVER.fullmatch(ref):
        resolved_ref = f"refs/tags/{ref}"
    elif ref.startswith("refs/tags/") or re.fullmatch(r"[0-9a-f]{40}", ref):
        resolved_ref = ref
    else:
        raise ReleaseVersionError("baseline_invalid", "baseline must be a full commit or refs/tags/<version>")
    result = _git(root, "rev-parse", "--verify", f"{resolved_ref}^{{commit}}", check=False)
    if result.returncode != 0:
        raise ReleaseVersionError("baseline_missing", f"baseline {resolved_ref} does not exist")
    return resolved_ref, result.stdout.strip()


def production_baseline(root: Path, product_version: str, baseline_ref: str | None) -> dict[str, str]:
    ref, tag_commit = _exact_ref(root, baseline_ref or f"refs/tags/{product_version}")
    if _git(root, "merge-base", "--is-ancestor", tag_commit, "HEAD", check=False).returncode != 0:
        raise ReleaseVersionError("baseline_not_ancestor", f"baseline {ref} is not an ancestor of HEAD")
    candidates = [tag_commit]
    history = _git(root, "rev-list", "--reverse", f"{tag_commit}..HEAD", "--", PUBLIC_BOOTSTRAP).stdout.splitlines()
    candidates.extend(value for value in history if value)
    baseline_commit = ""
    baseline_bytes: bytes | None = None
    for commit in candidates:
        content = _git_bytes(root, commit, PUBLIC_BOOTSTRAP)
        if content is not None and _product_version_from_bytes(content) == product_version:
            baseline_commit = commit
            baseline_bytes = content
            break
    if baseline_bytes is None:
        raise ReleaseVersionError("bootstrap_baseline_missing", "no immutable production bootstrap baseline matches the published product")
    current = (root / PUBLIC_BOOTSTRAP).read_bytes()
    expected_sha256 = hashlib.sha256(baseline_bytes).hexdigest()
    if current != baseline_bytes:
        raise ReleaseVersionError(
            "bootstrap_changed",
            f"production bootstrap differs from baseline commit {baseline_commit}",
        )
    return {
        "ref": ref,
        "tag_commit": tag_commit,
        "bootstrap_commit": baseline_commit,
        "bootstrap_sha256": expected_sha256,
    }


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
    elif relative == CORE_MANIFEST:
        try:
            payload = json.loads(text)
            payload["version"] = "<VERSION>"
        except (json.JSONDecodeError, TypeError, KeyError) as exc:
            raise ReleaseVersionError("invalid_metadata", f"{relative} is invalid") from exc
        if version not in text:
            raise ReleaseVersionError("version_mismatch", f"{relative} does not contain expected version {version}")
        return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    else:
        return content
    if version not in content.decode("utf-8"):
        raise ReleaseVersionError("version_mismatch", f"{relative} does not contain expected version {version}")
    return text.encode("utf-8")


def _tracked_paths(root: Path, ref: str | None, prefixes: list[str]) -> set[str]:
    if ref is None:
        result = _git(root, "ls-files", "-z", "--", *prefixes)
    else:
        result = _git(root, "ls-tree", "-r", "--name-only", "-z", ref, "--", *prefixes)
    return {value for value in result.stdout.split("\0") if value}


def _project(root: Path, ref: str | None, relative: str) -> dict[str, Any]:
    content = (root / relative).read_bytes() if ref is None else _git_bytes(root, ref, relative)
    if content is None:
        raise ReleaseVersionError("baseline_missing", f"{relative} is missing from {ref}")
    try:
        payload = tomllib.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseVersionError("invalid_metadata", f"{relative} is invalid") from exc
    return payload


def _component_paths(root: Path, ref: str | None, component: str) -> set[str]:
    project_path = INSTALLER_PROJECT if component == "installer" else CORE_PROJECT
    payload = _project(root, ref, project_path)
    project_root = project_path.split("/", 1)[0]
    package_dirs = payload.get("tool", {}).get("setuptools", {}).get("package-dir", {})
    if not isinstance(package_dirs, dict):
        raise ReleaseVersionError("invalid_metadata", f"{project_path} package-dir is invalid")
    prefixes = [f"{project_root}/{value}" for value in package_dirs.values() if isinstance(value, str)]
    if not prefixes:
        prefixes = [
            "installer/src/dispatch_installer"
            if component == "installer"
            else "dispatch-core/src/dispatch_core"
        ]
    paths = _tracked_paths(root, ref, prefixes)
    paths.add(project_path)
    license_files = payload.get("project", {}).get("license-files", ["LICENSE"])
    if not isinstance(license_files, list):
        raise ReleaseVersionError("invalid_metadata", f"{project_path} license-files is invalid")
    paths.update(f"{project_root}/{value}" for value in license_files if isinstance(value, str))
    if component == "core":
        paths.add(CORE_MANIFEST)
    return paths


def _working_mode(path: Path) -> str:
    if path.is_symlink():
        return "120000"
    return "100755" if path.stat().st_mode & 0o111 else "100644"


def _baseline_mode(root: Path, ref: str, relative: str) -> str | None:
    result = _git(root, "ls-tree", ref, "--", relative, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.split(None, 1)[0]


def component_changed(root: Path, baseline_ref: str, component: str, baseline_version: str, current_version: str) -> bool:
    paths = _component_paths(root, None, component) | _component_paths(root, baseline_ref, component)
    for relative in sorted(paths):
        current_path = root / relative
        current = current_path.read_bytes() if current_path.is_file() and not current_path.is_symlink() else None
        baseline = _git_bytes(root, baseline_ref, relative)
        if current is None or baseline is None:
            return True
        if _working_mode(current_path) != _baseline_mode(root, baseline_ref, relative):
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
    production = production_baseline(root, published, baseline_ref)
    ref = production["ref"]
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
        "baseline_commit": production["tag_commit"],
        "production_bootstrap_commit": production["bootstrap_commit"],
        "production_bootstrap_sha256": production["bootstrap_sha256"],
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


def _project_package_files(root: Path, project_path: str) -> tuple[dict[str, Any], list[dict[str, str]], list[str]]:
    payload = _project(root, None, project_path)
    project_root = project_path.split("/", 1)[0]
    package_dirs = payload.get("tool", {}).get("setuptools", {}).get("package-dir", {})
    if not isinstance(package_dirs, dict) or not package_dirs:
        raise ReleaseVersionError("invalid_metadata", f"{project_path} must declare package-dir mappings")
    files: dict[str, dict[str, str]] = {}
    source_roots: set[str] = set()
    for package, directory in package_dirs.items():
        if not isinstance(package, str) or not isinstance(directory, str):
            raise ReleaseVersionError("invalid_metadata", f"{project_path} package-dir mappings are invalid")
        prefix = f"{project_root}/{directory}"
        package_parts = package.split(".")
        directory_parts = Path(directory).parts
        if len(directory_parts) < len(package_parts):
            raise ReleaseVersionError("invalid_metadata", f"{project_path} package-dir mapping is invalid for {package}")
        source_root = Path(project_root, *directory_parts[: len(directory_parts) - len(package_parts)])
        source_roots.add(source_root.as_posix())
        for source in _tracked_paths(root, None, [prefix]):
            path = root / source
            if path.is_symlink() or not path.is_file() or path.suffix != ".py":
                continue
            remainder = Path(source).relative_to(prefix)
            member = Path(*package_parts, remainder).as_posix()
            existing = files.get(member)
            if existing is not None and existing["source"] != source:
                raise ReleaseVersionError("invalid_metadata", f"multiple sources map to {member}")
            files[member] = {"path": member, "source": source, "sha256": _sha256(path)}
    return payload, [files[name] for name in sorted(files)], sorted(source_roots)


def _project_license(project: dict[str, Any], project_path: str) -> str:
    value = project.get("project", {}).get("license")
    if not isinstance(value, str) or not value:
        raise ReleaseVersionError("invalid_metadata", f"{project_path} license metadata is invalid")
    return value


def _optional_dependencies(project: dict[str, Any]) -> list[str]:
    result: list[str] = []
    groups = project.get("project", {}).get("optional-dependencies", {})
    if not isinstance(groups, dict):
        raise ReleaseVersionError("invalid_metadata", "optional dependency metadata is invalid")
    for extra, dependencies in groups.items():
        if not isinstance(extra, str) or not isinstance(dependencies, list):
            raise ReleaseVersionError("invalid_metadata", "optional dependency metadata is invalid")
        for dependency in dependencies:
            if not isinstance(dependency, str):
                raise ReleaseVersionError("invalid_metadata", "optional dependency metadata is invalid")
            result.append(f'{dependency}; extra == "{extra}"')
    return result


def _refresh_metadata(root: Path, versions: Versions) -> list[str]:
    changed: set[str] = set()
    installer_project, installer_package_files, _ = _project_package_files(root, INSTALLER_PROJECT)
    installer_metadata = installer_project.get("project", {})
    installer_paths = {
        INSTALLER_PROJECT,
        *(item["source"] for item in installer_package_files),
        *(f"installer/{value}" for value in installer_metadata.get("license-files", ["LICENSE"])),
    }
    installer_plan = {
        "schema_version": 1,
        "online_only": True,
        "distribution": {
            "name": installer_metadata.get("name"),
            "version": versions.installer,
            "python_requires": installer_metadata.get("requires-python"),
            "license": _project_license(installer_project, INSTALLER_PROJECT),
            "dependencies": installer_metadata.get("dependencies", []),
            "pyproject": INSTALLER_PROJECT,
        },
        "files": [
            {"path": relative, "sha256": _sha256(root / relative), "size": (root / relative).stat().st_size}
            for relative in sorted(installer_paths)
        ],
    }
    _write_json(root / INSTALLER_PLAN, installer_plan)
    changed.add(INSTALLER_PLAN)

    runtime = _json(root / RUNTIME_PLAN)
    core_project, core_files, source_roots = _project_package_files(root, CORE_PROJECT)
    core_metadata = core_project.get("project", {})
    license_files = core_metadata.get("license-files", ["LICENSE"])
    if not isinstance(license_files, list) or len(license_files) != 1 or not isinstance(license_files[0], str):
        raise ReleaseVersionError("invalid_metadata", "Core must declare exactly one license file")
    license_source = f"dispatch-core/{license_files[0]}"
    scripts = core_metadata.get("scripts", {})
    if not isinstance(scripts, dict):
        raise ReleaseVersionError("invalid_metadata", "Core console scripts are invalid")
    core_plan = {
        "name": core_metadata.get("name"),
        "version": versions.core,
        "summary": core_metadata.get("description"),
        "license": _project_license(core_project, CORE_PROJECT),
        "license_file": {"source": license_source, "sha256": _sha256(root / license_source)},
        "pyproject": CORE_PROJECT,
        "source_roots": source_roots,
        "files": core_files,
        "requires_dist": core_metadata.get("dependencies", []),
        "optional_requires_dist": _optional_dependencies(core_project),
        "console_scripts": scripts,
        "top_level": sorted({item["path"].split("/", 1)[0] for item in core_files}),
    }
    runtime["python_requires"] = core_metadata.get("requires-python")
    runtime["distributions"] = [core_plan]
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
    existing = _json(root / PUBLIC_SCOPE).get("files", {})
    if not isinstance(existing, dict):
        raise ReleaseVersionError("source_scope", "public source scope files are invalid")
    result = _git(root, "ls-files", "-z")
    files: dict[str, dict[str, str]] = {}
    for relative in sorted(value for value in result.stdout.split("\0") if value and value != PUBLIC_SCOPE):
        path = root / relative
        previous = existing.get(relative, {})
        origin = previous.get("origin", "generated_public") if isinstance(previous, dict) else "generated_public"
        if path.is_symlink():
            files[relative] = {"kind": "symlink", "origin": origin, "target": os.readlink(path)}
        elif path.is_file():
            files[relative] = {"kind": "file", "origin": origin, "sha256": _sha256(path)}
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
    if current == versions:
        raise ReleaseVersionError("no_change", "proposed versions are already present")
    snapshots = {relative: (root / relative).read_bytes() for relative in PREPARATION_OUTPUTS}
    modes = {relative: (root / relative).stat().st_mode for relative in PREPARATION_OUTPUTS}
    bootstrap = (root / PUBLIC_BOOTSTRAP).read_bytes()
    try:
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
        if (root / PUBLIC_BOOTSTRAP).read_bytes() != bootstrap:
            raise ReleaseVersionError("bootstrap_changed", "release preparation changed the production bootstrap")
        modified = set(_git(root, "diff", "--name-only").stdout.splitlines())
        if not modified <= PREPARATION_OUTPUTS:
            unexpected = sorted(modified - PREPARATION_OUTPUTS)
            raise ReleaseVersionError("unexpected_write", f"release preparation modified unexpected paths: {unexpected}")
        issues = consistency_issues(root)
        if issues:
            raise ReleaseVersionError("write_verification", issues[0])
        return sorted(changed)
    except Exception as exc:
        try:
            for relative, content in snapshots.items():
                path = root / relative
                path.write_bytes(content)
                os.chmod(path, modes[relative])
            if (root / PUBLIC_BOOTSTRAP).read_bytes() != bootstrap:
                raise OSError("production bootstrap changed during rollback")
        except Exception as rollback_exc:
            raise ReleaseVersionError("rollback_failed", f"release preparation failed and rollback was incomplete: {rollback_exc}") from exc
        if isinstance(exc, ReleaseVersionError):
            raise
        raise ReleaseVersionError("apply_failed", f"release preparation failed without changing the worktree: {exc}") from exc


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
    expected_installer_paths = _component_paths(root, None, "installer")
    planned_installer_paths = {str(item.get("path")) for item in installer_plan.get("files", [])}
    if planned_installer_paths != expected_installer_paths:
        issues.append("installer package plan does not match the complete distributable closure")
    runtime = _json(root / RUNTIME_PLAN)
    try:
        core_plan = next(item for item in runtime["distributions"] if item["name"] == "dispatch-core")
    except (KeyError, TypeError, StopIteration):
        return [*issues, "Core runtime plan is missing"]
    for item in core_plan.get("files", []):
        source = root / str(item.get("source"))
        if not source.is_file() or source.is_symlink() or item.get("sha256") != _sha256(source):
            issues.append(f"Core runtime metadata is stale: {item.get('source')}")
    _, expected_core_files, _ = _project_package_files(root, CORE_PROJECT)
    if core_plan.get("files") != expected_core_files:
        issues.append("Core runtime plan does not match the complete distributable closure")
    expected_files = [{"path": item["path"], "sha256": item["sha256"]} for item in core_plan["files"]]
    if manifest.get("core", {}).get("package_files") != expected_files:
        issues.append("release manifest Core package files differ from the runtime plan")
    return issues


def validate_acceptance_evidence(root: Path, evidence_path: Path, product_version: str) -> dict[str, Any]:
    evidence = _json(evidence_path)
    source_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    if (
        set(evidence)
        != {
            "schema_version",
            "product_version",
            "source_commit",
            "host",
            "completed_at",
            "checks",
            "contains_secrets",
        }
        or evidence.get("schema_version") != 1
        or evidence.get("product_version") != product_version
        or evidence.get("source_commit") != source_commit
        or evidence.get("host") != "dispatch-testing"
        or not isinstance(evidence.get("completed_at"), str)
        or evidence.get("contains_secrets") is not False
        or not isinstance(evidence.get("checks"), dict)
        or set(evidence["checks"]) != REQUIRED_ACCEPTANCE_CHECKS
        or any(value is not True for value in evidence["checks"].values())
    ):
        raise ReleaseVersionError("acceptance_invalid", "acceptance evidence is incomplete or does not match this release")
    return evidence


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
    except Exception as exc:
        payload = envelope(
            ok=False,
            action="prepare-release",
            status="error",
            data={"issues": ["unexpected release preparation failure"]},
            error={"code": "internal_error", "message": f"unexpected release preparation failure: {exc}"},
        )
        _emit(payload, json_output=json_output)
        return 2
    _emit(payload, json_output=json_output)
    return 0


def verify_main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    json_output = "--json" in arguments
    parser = ReleaseArgumentParser(description="Verify that Dispatch version metadata is ready for release acceptance")
    parser.add_argument("--baseline-ref")
    parser.add_argument("--phase", choices=("prepared", "release"), default="release")
    parser.add_argument("--acceptance-evidence", type=Path)
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
        dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=all").stdout)
        if args.phase == "release" and dirty:
            issues.append("release readiness requires a clean worktree")
        if args.phase == "release":
            if args.acceptance_evidence is None:
                issues.append("release readiness requires exact acceptance evidence")
            else:
                validate_acceptance_evidence(root, args.acceptance_evidence, analysis["current"]["product"])
        data = {**analysis, "phase": args.phase, "issues": issues, "worktree_clean": not dirty}
        if issues:
            payload = envelope(
                ok=False,
                action="verify-release-readiness",
                status="not_ready",
                data=data,
                error={"code": "release_not_ready", "message": issues[0]},
            )
            _emit(payload, json_output=json_output)
            return 1
        payload = envelope(
            ok=True,
            action="verify-release-readiness",
            status="prepared" if args.phase == "prepared" else "ready",
            data=data,
        )
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
    except Exception as exc:
        payload = envelope(
            ok=False,
            action="verify-release-readiness",
            status="error",
            data={"issues": ["unexpected release readiness failure"]},
            error={"code": "internal_error", "message": f"unexpected release readiness failure: {exc}"},
        )
        _emit(payload, json_output=json_output)
        return 2
    _emit(payload, json_output=json_output)
    return 0
