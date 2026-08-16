#!/usr/bin/env python3
"""Read-only conformance audit for Dispatch Plugin Standard v1 owners."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any
import zipfile

import yaml
from jsonschema import Draft202012Validator

CORE_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = CORE_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from dispatch_core.paths import DispatchPaths

PATHS = DispatchPaths.from_environment(code_root=os.environ.get("DISPATCH_CODE_ROOT") or CORE_ROOT.parent)
WORKSPACE = PATHS.code
SCHEMA_PATH = WORKSPACE / "docs/schemas/dispatch-plugin-v1.schema.json"
SLUG = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
COMPONENT_KINDS = {
    "hermes-tool", "collector", "service", "auth-provider", "library", "control-plane", "retired"
}
PRIVILEGES = {"read", "health", "mutation", "administration", "direct-delivery"}
CAPABILITIES = {
    "read_local_data", "mutate_data", "collect", "network", "authentication", "direct_delivery", "long_running"
}
VOLATILE_PARTS = {"__pycache__", ".pytest_cache", ".mypy_cache", "logs", "locks", "browser-state", "chrome-profile"}
VOLATILE_SUFFIXES = {".pyc", ".pyo", ".tmp", ".swp", ".wal", ".shm"}
ENVELOPE_KEYS = {"ok", "action", "status", "data", "freshness", "delivery", "error"}
HEALTH_PLANES = {
    "registration", "runtime_integrity", "query", "data", "freshness",
    "collector", "authentication", "delivery", "overall",
}


class UniqueLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader: UniqueLoader, node: yaml.nodes.MappingNode, deep: bool = False) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"duplicate YAML key: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


class Audit:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.failures: list[str] = []
        self.passes: list[str] = []

    def fail(self, message: str) -> None:
        self.failures.append(message)

    def passed(self, message: str) -> None:
        self.passes.append(message)

    def require(self, condition: bool, message: str) -> bool:
        if condition:
            return True
        self.fail(message)
        return False

    def relative(self, value: Any, label: str, *, must_exist: bool = True) -> Path | None:
        if not isinstance(value, str) or not value or value.startswith("/"):
            self.fail(f"{label} must be a non-empty owner-relative path")
            return None
        candidate = (self.root / value).resolve(strict=False)
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError:
            self.fail(f"{label} escapes the owner root")
            return None
        if must_exist and not candidate.exists():
            self.fail(f"{label} does not exist: {value}")
        return candidate

    def workspace_path(self, value: Any, label: str, expected_prefix: str, owner: str) -> Path | None:
        expected = f"{expected_prefix}/{owner}"
        if not isinstance(value, str) or value != expected and not value.startswith(expected + "/"):
            self.fail(f"{label} must be owned beneath {expected}")
            return None
        candidate = (WORKSPACE / value).resolve(strict=False)
        try:
            candidate.relative_to((WORKSPACE / expected).resolve(strict=False))
        except ValueError:
            self.fail(f"{label} escapes {expected}")
            return None
        return candidate


def _load_yaml(path: Path) -> Any:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _registration_probe(adapter: Path, owner_root: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
    helper = r'''
import contextlib, importlib.util, io, json, pathlib, sys
sys.dont_write_bytecode = True
adapter = pathlib.Path(sys.argv[1])
spec = importlib.util.spec_from_file_location("_dispatch_conformance_target", adapter)
if spec is None or spec.loader is None:
    raise RuntimeError("adapter_import_unavailable")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
class Context:
    def __init__(self): self.items = []
    def register_tool(self, **kwargs):
        check = kwargs.get("check_fn")
        available = None
        if callable(check):
            try: available = bool(check())
            except Exception: available = False
        schema = kwargs.get("schema")
        handler = kwargs.get("handler")
        actions = None
        try: actions = schema["parameters"]["properties"]["action"]["enum"]
        except (KeyError, TypeError): pass
        def invoke(args):
            if not callable(handler): return {"__probe_error__": "handler_missing"}
            try:
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    value = handler(args)
                if isinstance(value, str): value = json.loads(value)
                json.dumps(value)
                return value
            except Exception as exc:
                return {"__probe_error__": type(exc).__name__}
        self.items.append({
            "name": kwargs.get("name"),
            "toolset": kwargs.get("toolset"),
            "schema": schema,
            "available": available,
            "invalid_response": invoke({}),
            "health_response": invoke({"action": "health"}) if isinstance(actions, list) and "health" in actions else None,
        })
ctx = Context()
module.register(ctx)
print(json.dumps(ctx.items, sort_keys=True, separators=(",", ":")))
'''
    env = {
        "HOME": str(PATHS.home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-B", "-c", helper, str(adapter)],
            cwd=owner_root,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "registration probe could not execute"
    if completed.returncode != 0 or len(completed.stdout.encode()) > 64 * 1024:
        return None, "registration probe failed"
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None, "registration probe returned invalid JSON"
    return value if isinstance(value, list) else None, None if isinstance(value, list) else "registration result is not a list"


def _check_error_object(audit: Audit, value: Any, label: str) -> None:
    if not audit.require(isinstance(value, dict) and set(value) == {"code", "message"}, f"{label}.error must contain exactly code and message"):
        return
    audit.require(isinstance(value.get("code"), str) and bool(re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value["code"])), f"{label}.error.code is invalid")
    audit.require(isinstance(value.get("message"), str) and 1 <= len(value["message"]) <= 512, f"{label}.error.message is invalid")


def _check_envelope(audit: Audit, value: Any, label: str, *, health: bool = False) -> None:
    if not audit.require(isinstance(value, dict), f"{label} is not a JSON object"):
        return
    if "__probe_error__" in value:
        audit.fail(f"{label} could not be exercised")
        return
    if not audit.require(set(value) == ENVELOPE_KEYS, f"{label} must use the exact seven-field response envelope"):
        return
    audit.require(type(value.get("ok")) is bool, f"{label}.ok must be boolean")
    audit.require(isinstance(value.get("action"), str) and bool(value["action"]), f"{label}.action must be non-empty")
    audit.require(isinstance(value.get("status"), str) and bool(value["status"]), f"{label}.status must be non-empty")
    audit.require(isinstance(value.get("data"), dict), f"{label}.data must be an object")
    audit.require(value.get("freshness") is None or isinstance(value.get("freshness"), dict), f"{label}.freshness must be null or an object")
    audit.require(value.get("delivery") is None or isinstance(value.get("delivery"), dict), f"{label}.delivery must be null or an object")
    if value.get("ok") is True:
        audit.require(value.get("error") is None, f"{label}.error must be null on success")
    else:
        _check_error_object(audit, value.get("error"), label)
    if health:
        audit.require(value.get("action") == "health", f"{label}.action must be health")
        data = value.get("data")
        if isinstance(data, dict):
            missing = HEALTH_PLANES - set(data)
            audit.require(not missing, f"{label}.data is missing readiness planes: {', '.join(sorted(missing))}")
            audit.require(isinstance(data.get("overall"), str) and bool(data.get("overall")), f"{label}.data.overall is invalid")
    else:
        audit.require(value.get("ok") is False, f"{label} must fail closed")


def _manifest_actions(schema: Any) -> set[str] | None:
    if not isinstance(schema, dict):
        return None
    parameters = schema.get("parameters")
    if not isinstance(parameters, dict):
        return None
    if parameters.get("type") != "object" or parameters.get("additionalProperties") is not False:
        return None
    if parameters.get("required") != ["action"]:
        return None
    properties = parameters.get("properties")
    action = properties.get("action") if isinstance(properties, dict) else None
    values = action.get("enum") if isinstance(action, dict) else None
    if not isinstance(values, list) or not values or any(not isinstance(item, str) for item in values):
        return None
    return set(values)


def _check_runtime_payload(audit: Audit, payload: dict[str, Any], label: str) -> None:
    entries = payload.get("files") or payload.get("members") or payload.get("entries")
    if isinstance(entries, list):
        for item in entries:
            value = item.get("path") if isinstance(item, dict) else None
            if not isinstance(value, str):
                continue
            member = Path(value)
            if any(part in VOLATILE_PARTS for part in member.parts) or member.suffix in VOLATILE_SUFFIXES:
                audit.fail(f"{label} includes volatile member: {value}")
                return
    runtime = payload.get("runtime")
    if isinstance(runtime, dict) and isinstance(runtime.get("path"), str) and isinstance(runtime.get("sha256"), str):
        active = Path(runtime["path"])
        active_is_directory = active.is_dir() and not active.is_symlink()
        if active_is_directory:
            manifests = [candidate for candidate in (active / "release-manifest.json", active / "runtime-manifest.json") if candidate.is_file() and not candidate.is_symlink()]
            if len(manifests) != 1:
                audit.fail(f"{label} active runtime directory has no safe manifest")
                return
            manifest = manifests[0]
            actual = _sha256(manifest)
            release = runtime.get("release")
            if isinstance(release, str) and active.name != release:
                audit.fail(f"{label} active release directory does not match recorded identity")
            _check_runtime_manifest(audit, manifest, f"{label}.runtime_manifest")
        elif active.is_file() and not active.is_symlink():
            actual = _sha256(active)
            release = runtime.get("release")
        else:
            audit.fail(f"{label} active runtime is missing or unsafe")
            return
        if stat.S_IMODE(active.lstat().st_mode) & stat.S_IWUSR:
            audit.fail(f"{label} active runtime is owner-writable")
        if actual != runtime["sha256"]:
            audit.fail(f"{label} active runtime digest mismatch")
        runtime_manifest = payload.get("runtime_manifest")
        release_root: Path | None = None
        if isinstance(runtime_manifest, dict):
            manifest_path = runtime_manifest.get("path")
            manifest_digest = runtime_manifest.get("sha256")
            if not isinstance(manifest_path, str) or not isinstance(manifest_digest, str):
                audit.fail(f"{label} runtime manifest record is invalid")
            else:
                manifest = Path(manifest_path)
                if not manifest.is_file() or manifest.is_symlink() or _sha256(manifest) != manifest_digest:
                    audit.fail(f"{label} runtime manifest is missing or inconsistent")
                else:
                    release_root = manifest.parent
                    if stat.S_IMODE(manifest.lstat().st_mode) & stat.S_IWUSR:
                        audit.fail(f"{label} runtime manifest is owner-writable")
                    if stat.S_IMODE(release_root.lstat().st_mode) & stat.S_IWUSR:
                        audit.fail(f"{label} release root is owner-writable")
                    _check_runtime_manifest(audit, manifest, f"{label}.runtime_manifest")
        if isinstance(release, str):
            if release_root is not None:
                try:
                    active.relative_to(release_root)
                except ValueError:
                    audit.fail(f"{label} active launcher is outside its release root")
                if release_root.name != release:
                    audit.fail(f"{label} active release directory does not match recorded identity")
            elif active_is_directory:
                if active.name != release:
                    audit.fail(f"{label} active release identity does not converge")
            elif active.parent.name != release or not actual.startswith(release):
                audit.fail(f"{label} active release identity does not converge")
        if zipfile.is_zipfile(active):
            try:
                with zipfile.ZipFile(active) as archive:
                    for name in archive.namelist():
                        member = Path(name)
                        if any(part in VOLATILE_PARTS for part in member.parts) or member.suffix in VOLATILE_SUFFIXES:
                            audit.fail(f"{label} active runtime includes volatile member: {name}")
                            break
            except (OSError, zipfile.BadZipFile):
                audit.fail(f"{label} active runtime archive is invalid")
        rollback = payload.get("rollback")
        if isinstance(rollback, dict):
            rollback_path = rollback.get("path")
            rollback_digest = rollback.get("sha256")
            rollback_release = rollback.get("release")
            if not all(isinstance(value, str) and value for value in (rollback_path, rollback_digest, rollback_release)):
                audit.fail(f"{label} rollback record is invalid")
            else:
                candidate = Path(rollback_path)
                if candidate.is_dir() and not candidate.is_symlink():
                    rollback_manifests = [value for value in (candidate / "release-manifest.json", candidate / "runtime-manifest.json") if value.is_file() and not value.is_symlink()]
                    rollback_manifest = rollback_manifests[0] if len(rollback_manifests) == 1 else candidate / "release-manifest.json"
                    valid_rollback = (
                        len(rollback_manifests) == 1
                        and candidate.name == rollback_release
                        and rollback_manifest.is_file()
                        and not rollback_manifest.is_symlink()
                        and _sha256(rollback_manifest) == rollback_digest
                    )
                    if valid_rollback:
                        _check_runtime_manifest(audit, rollback_manifest, f"{label}.rollback_manifest")
                else:
                    release_ancestor = next((parent for parent in candidate.parents if parent.name == rollback_release), None)
                    valid_rollback = (
                        candidate.is_file()
                        and not candidate.is_symlink()
                        and release_ancestor is not None
                        and _sha256(candidate) == rollback_digest
                    )
                if not valid_rollback:
                    audit.fail(f"{label} rollback release is missing or inconsistent")
                elif stat.S_IMODE(candidate.lstat().st_mode) & stat.S_IWUSR:
                    audit.fail(f"{label} rollback runtime is owner-writable")
        elif isinstance(payload.get("rollback_release"), str) and release_root is None:
            rollback_release = payload["rollback_release"]
            candidate = active.parents[1] / rollback_release / active.name
            if not candidate.is_file() or candidate.is_symlink() or not _sha256(candidate).startswith(rollback_release):
                audit.fail(f"{label} rollback release is missing or inconsistent")
    units = payload.get("units")
    if isinstance(units, list):
        for item in units:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str):
                audit.fail(f"{label} has an invalid installed-unit record")
                continue
            unit = Path(item["path"])
            if not unit.is_file() or unit.is_symlink() or _sha256(unit) != item["sha256"]:
                audit.fail(f"{label} installed-unit digest mismatch: {item.get('unit', item['path'])}")


def _check_runtime_manifest(audit: Audit, path: Path, label: str) -> None:
    if not path.is_file():
        return
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        audit.fail(f"{label} is not valid JSON")
        return
    if not isinstance(payload, dict):
        audit.fail(f"{label} must be a JSON object")
        return
    interfaces = payload.get("interfaces")
    if isinstance(interfaces, dict) and interfaces:
        for name, interface in interfaces.items():
            if not isinstance(name, str) or not isinstance(interface, dict):
                audit.fail(f"{label} has an invalid interface activation record")
                continue
            _check_runtime_payload(audit, interface, f"{label}.interfaces.{name}")
        return
    _check_runtime_payload(audit, payload, label)


def audit_owner(root: Path) -> Audit:
    audit = Audit(root)
    manifest_path = root / "dispatch-plugin.yaml"
    if not audit.require(root.is_dir(), f"owner root is not a directory: {root}"):
        return audit
    if not audit.require((root / "README.md").is_file(), "missing root README.md"):
        pass
    if not audit.require(manifest_path.is_file(), "missing dispatch-plugin.yaml"):
        return audit
    if not audit.require(SCHEMA_PATH.is_file(), "workspace schema is missing"):
        return audit
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        payload = _load_yaml(manifest_path)
    except (OSError, ValueError, yaml.YAMLError, json.JSONDecodeError) as exc:
        audit.fail(f"manifest/schema parse failed: {type(exc).__name__}")
        return audit
    if not isinstance(payload, dict):
        audit.fail("root manifest must be an object")
        return audit
    schema_errors = sorted(Draft202012Validator(schema).iter_errors(payload), key=lambda item: list(item.absolute_path))
    for error in schema_errors:
        location = ".".join(str(part) for part in error.absolute_path) or "<root>"
        audit.fail(f"schema {location}: {error.message}")

    required = {"schema_version", "id", "display_name", "version", "summary", "owner", "paths", "commands", "components"}
    allowed = required | {"retention"}
    audit.require(set(payload) >= required, "root manifest is missing required fields")
    audit.require(set(payload) <= allowed, "root manifest has unknown fields")
    audit.require(payload.get("schema_version") == 1, "schema_version must be 1")
    owner_id = payload.get("id")
    if not audit.require(isinstance(owner_id, str) and bool(SLUG.fullmatch(owner_id)), "id is not a valid slug"):
        return audit
    audit.require(root.name == owner_id, f"manifest id {owner_id} does not match directory {root.name}")
    audit.require(isinstance(payload.get("version"), str) and bool(VERSION.fullmatch(payload["version"])), "version is not semantic v1 format")
    audit.require(isinstance(payload.get("summary"), str) and 10 <= len(payload["summary"]) <= 300, "summary must be 10-300 characters")
    owner = payload.get("owner")
    audit.require(isinstance(owner, dict) and set(owner) == {"data", "team"}, "owner must contain exactly data and team")
    if isinstance(owner, dict):
        audit.require(owner.get("data") == owner_id, "owner.data must equal id")
        audit.require(isinstance(owner.get("team"), str) and bool(owner["team"]), "owner.team is required")

    paths = payload.get("paths")
    required_paths = {"source", "tests", "state", "staging", "locks", "receipts"}
    allowed_paths = required_paths | {"database", "databases", "artifacts", "artifact_stores"}
    if audit.require(isinstance(paths, dict), "paths must be an object"):
        audit.require(set(paths) >= required_paths, "paths is missing required roots")
        audit.require(set(paths) <= allowed_paths, "paths has unknown fields")
        audit.relative(paths.get("source"), "paths.source")
        audit.relative(paths.get("tests"), "paths.tests")
        for key in ("state", "staging", "locks", "receipts"):
            audit.workspace_path(paths.get(key), f"paths.{key}", f"plugins", owner_id)
            audit.require(paths.get(key) == f"plugins/{owner_id}/{key}", f"paths.{key} must use the canonical owner root")
        if "database" in paths:
            audit.workspace_path(paths["database"], "paths.database", "db", owner_id)
        if "databases" in paths:
            databases = paths["databases"]
            if audit.require(isinstance(databases, dict) and bool(databases), "paths.databases must be a non-empty object"):
                audit.require("database" not in paths, "paths.database and paths.databases are mutually exclusive")
                for name, value in databases.items():
                    audit.workspace_path(value, f"paths.databases.{name}", "db", name)
        if "artifacts" in paths:
            audit.workspace_path(paths["artifacts"], "paths.artifacts", "artifacts", owner_id)
        if "artifact_stores" in paths:
            stores = paths["artifact_stores"]
            if audit.require(isinstance(stores, dict) and bool(stores), "paths.artifact_stores must be a non-empty object"):
                audit.require("artifacts" not in paths, "paths.artifacts and paths.artifact_stores are mutually exclusive")
                for name, value in stores.items():
                    audit.workspace_path(value, f"paths.artifact_stores.{name}", "artifacts", name)

    commands = payload.get("commands")
    expected_commands = {name: f"./scripts/{name}" for name in ("test", "build", "verify", "health")}
    if audit.require(commands == expected_commands, "commands must be the four canonical script entrypoints"):
        for name in expected_commands:
            script = root / "scripts" / name
            if audit.require(script.is_file() and not script.is_symlink(), f"missing regular scripts/{name}"):
                mode = stat.S_IMODE(script.stat().st_mode)
                audit.require(bool(mode & stat.S_IXUSR), f"scripts/{name} is not owner-executable")
                audit.require(not bool(mode & 0o022), f"scripts/{name} is group/world writable")
                first = script.open("rb").readline(256).decode(errors="replace")
                audit.require(first.startswith("#!"), f"scripts/{name} has no shebang")

    components = payload.get("components")
    if not audit.require(isinstance(components, list) and bool(components), "components must be a non-empty list"):
        return audit
    identifiers: set[str] = set()
    for index, component in enumerate(components):
        label = f"components[{index}]"
        if not audit.require(isinstance(component, dict), f"{label} must be an object"):
            continue
        allowed_component = {"id", "kind", "source", "capabilities", "hermes", "actions", "runtime", "coordinator_mode", "service_units"}
        audit.require(set(component) <= allowed_component, f"{label} has unknown fields")
        component_id = component.get("id")
        audit.require(isinstance(component_id, str) and bool(SLUG.fullmatch(component_id)), f"{label}.id is invalid")
        audit.require(component_id not in identifiers, f"duplicate component id: {component_id}")
        if isinstance(component_id, str):
            identifiers.add(component_id)
        kind = component.get("kind")
        audit.require(kind in COMPONENT_KINDS, f"{label}.kind is invalid")
        audit.relative(component.get("source"), f"{label}.source")
        capabilities = component.get("capabilities")
        if audit.require(isinstance(capabilities, dict) and set(capabilities) == CAPABILITIES, f"{label}.capabilities must declare all capability booleans"):
            audit.require(all(isinstance(value, bool) for value in capabilities.values()), f"{label}.capabilities values must be booleans")
        actions = component.get("actions")
        declared_actions: set[str] = set()
        if actions is not None:
            if audit.require(isinstance(actions, list) and bool(actions), f"{label}.actions must be a non-empty list"):
                for action in actions:
                    valid = isinstance(action, dict) and set(action) == {"name", "privilege"}
                    if not audit.require(valid, f"{label} action entries require name and privilege"):
                        continue
                    name = action["name"]
                    audit.require(isinstance(name, str) and bool(re.fullmatch(r"[a-z][a-z0-9_]*", name)), f"{label} action name is invalid")
                    audit.require(name not in declared_actions, f"{label} has duplicate action {name}")
                    declared_actions.add(name)
                    privilege = action["privilege"]
                    audit.require(privilege in PRIVILEGES, f"{label} action privilege is invalid")
                    if privilege in {"mutation", "administration"} and isinstance(capabilities, dict):
                        audit.require(capabilities.get("mutate_data") or capabilities.get("collect"), f"{label} mutating action lacks mutation/collection capability")
                    if privilege == "direct-delivery" and isinstance(capabilities, dict):
                        audit.require(capabilities.get("direct_delivery") is True, f"{label} delivery action lacks direct_delivery capability")
        runtime = component.get("runtime")
        if kind in {"hermes-tool", "collector", "service"}:
            if audit.require(isinstance(runtime, dict), f"{label}.runtime is required"):
                allowed_runtime = {"releases", "activation_record", "current_pointer", "launcher_manifest"}
                audit.require(set(runtime) >= {"releases", "activation_record"} and set(runtime) <= allowed_runtime, f"{label}.runtime fields are invalid")
                audit.relative(runtime.get("releases"), f"{label}.runtime.releases", must_exist=False)
                activation = audit.relative(runtime.get("activation_record"), f"{label}.runtime.activation_record", must_exist=False)
                if activation and activation.is_file() and activation.suffix == ".json":
                    _check_runtime_manifest(audit, activation, f"{label}.runtime.activation_record")
                if "current_pointer" in runtime:
                    audit.relative(runtime["current_pointer"], f"{label}.runtime.current_pointer", must_exist=False)
                if "launcher_manifest" in runtime:
                    launcher_manifest = audit.relative(runtime["launcher_manifest"], f"{label}.runtime.launcher_manifest", must_exist=False)
                    if launcher_manifest:
                        _check_runtime_manifest(audit, launcher_manifest, f"{label}.runtime.launcher_manifest")
        if kind == "service":
            units = component.get("service_units")
            audit.require(isinstance(units, list) and bool(units), f"{label}.service_units is required")
            if isinstance(units, list):
                for unit in units:
                    audit.relative(unit, f"{label}.service_units")
        if kind == "hermes-tool":
            hermes = component.get("hermes")
            if not audit.require(isinstance(hermes, dict), f"{label}.hermes is required"):
                continue
            required_hermes = {"package", "manifest", "adapter", "tool", "toolset"}
            if not audit.require(set(hermes) == required_hermes, f"{label}.hermes fields are invalid"):
                continue
            plugin_manifest = audit.relative(hermes["manifest"], f"{label}.hermes.manifest")
            adapter = audit.relative(hermes["adapter"], f"{label}.hermes.adapter")
            if plugin_manifest and plugin_manifest.is_file():
                try:
                    plugin_payload = _load_yaml(plugin_manifest)
                except (OSError, ValueError, yaml.YAMLError):
                    audit.fail(f"{label} Hermes manifest is invalid YAML")
                else:
                    provides = plugin_payload.get("provides_tools") if isinstance(plugin_payload, dict) else None
                    audit.require(provides == [hermes["tool"]], f"{label} plugin.yaml provides_tools does not match declared tool")
            if adapter and adapter.is_file():
                try:
                    ast.parse(adapter.read_text(encoding="utf-8"), filename=str(adapter))
                except (OSError, SyntaxError):
                    audit.fail(f"{label} adapter syntax is invalid")
                registrations, error = _registration_probe(adapter, root)
                if error:
                    audit.fail(f"{label} {error}")
                elif audit.require(len(registrations or []) == 1, f"{label} must register exactly one tool"):
                    registration = registrations[0]
                    audit.require(registration.get("name") == hermes["tool"], f"{label} registered tool name does not match")
                    audit.require(registration.get("toolset") == hermes["toolset"], f"{label} registered toolset does not match")
                    actual_actions = _manifest_actions(registration.get("schema"))
                    audit.require(actual_actions is not None, f"{label} schema must require action and reject additional properties")
                    if actual_actions is not None:
                        audit.require(actual_actions == declared_actions, f"{label} declared actions do not match registered schema")
                    audit.require(isinstance(registration.get("available"), bool), f"{label} availability check did not return a boolean")
                    _check_envelope(audit, registration.get("invalid_response"), f"{label} invalid-input response")
                    if actual_actions is not None and "health" in actual_actions:
                        _check_envelope(audit, registration.get("health_response"), f"{label} health response", health=True)

    retention = payload.get("retention")
    if retention is not None:
        expected = {"keep_current", "keep_rollback", "preserve_pinned"}
        audit.require(isinstance(retention, dict) and set(retention) == expected, "retention fields are invalid")
        if isinstance(retention, dict):
            audit.require(retention.get("keep_current") is True and retention.get("preserve_pinned") is True, "retention must preserve current and pinned releases")
            rollback = retention.get("keep_rollback")
            audit.require(isinstance(rollback, int) and not isinstance(rollback, bool) and 1 <= rollback <= 10, "retention.keep_rollback must be 1-10")

    if not audit.failures:
        audit.passed("root manifest and README")
        audit.passed("canonical ownership paths and lifecycle commands")
        audit.passed("component classes and capabilities")
        audit.passed("Hermes declaration, registration, schema, actions, and availability")
        audit.passed("runtime references and retention policy")
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("owner", type=Path)
    args = parser.parse_args()
    root = args.owner if args.owner.is_absolute() else (Path.cwd() / args.owner)
    audit = audit_owner(root.resolve())
    for message in audit.passes:
        print(f"PASS: {message}")
    for message in audit.failures:
        print(f"FAIL: {message}")
    if audit.failures:
        print(f"RESULT: nonconforming ({len(audit.failures)} failure(s))")
        return 1
    print("RESULT: conforming to Dispatch Plugin Standard v1")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
