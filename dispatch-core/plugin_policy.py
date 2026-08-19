#!/usr/bin/env python3
"""Read-only source conformance audit for Dispatch plugins."""
from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tomllib
from typing import Any

import yaml

CORE_ROOT = Path(__file__).resolve().parent
WORKSPACE = CORE_ROOT.parent
PLUGIN_ID = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
ACTION_ID = re.compile(r"^[a-z][a-z0-9_]*$")
ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CAPABILITIES = {
    "read_local_data",
    "mutate_data",
    "collect",
    "network",
    "authentication",
    "direct_delivery",
    "long_running",
}
ENVELOPE_KEYS = {"ok", "action", "status", "data", "freshness", "delivery", "error"}
HEALTH_PLANES = {
    "registration",
    "runtime_integrity",
    "query",
    "data",
    "freshness",
    "collector",
    "authentication",
    "delivery",
    "overall",
}


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
            self.fail(f"{label} must be an owner-relative path")
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


def _load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _source_root(root: Path, project: dict[str, Any]) -> Path:
    package_dir = project.get("tool", {}).get("setuptools", {}).get("package-dir", {})
    value = package_dir.get("") if isinstance(package_dir, dict) else None
    return root / value if isinstance(value, str) and value else root / "src"


def _check_error(audit: Audit, value: Any, label: str) -> None:
    if not audit.require(isinstance(value, dict) and set(value) == {"code", "message"}, f"{label}.error must contain exactly code and message"):
        return
    audit.require(
        isinstance(value.get("code"), str) and bool(ERROR_CODE.fullmatch(value["code"])),
        f"{label}.error.code is invalid",
    )
    audit.require(
        isinstance(value.get("message"), str) and 1 <= len(value["message"]) <= 512,
        f"{label}.error.message is invalid",
    )


def _check_envelope(audit: Audit, value: Any, label: str, *, health: bool = False) -> None:
    if not audit.require(isinstance(value, dict), f"{label} is not a JSON object"):
        return
    if not audit.require(set(value) == ENVELOPE_KEYS, f"{label} must use the exact seven-field response envelope"):
        return
    audit.require(type(value.get("ok")) is bool, f"{label}.ok must be boolean")
    audit.require(isinstance(value.get("action"), str) and bool(value["action"]), f"{label}.action must be non-empty")
    audit.require(isinstance(value.get("status"), str) and bool(value["status"]), f"{label}.status must be non-empty")
    audit.require(isinstance(value.get("data"), dict), f"{label}.data must be an object")
    audit.require(value.get("freshness") is None or isinstance(value["freshness"], dict), f"{label}.freshness must be null or an object")
    audit.require(value.get("delivery") is None or isinstance(value["delivery"], dict), f"{label}.delivery must be null or an object")
    if value.get("ok") is True:
        audit.require(value.get("error") is None, f"{label}.error must be null on success")
    else:
        _check_error(audit, value.get("error"), label)
    if health:
        audit.require(value.get("action") == "health", f"{label}.action must be health")
        data = value.get("data")
        if isinstance(data, dict):
            missing = HEALTH_PLANES - set(data)
            audit.require(not missing, f"{label}.data is missing readiness planes: {', '.join(sorted(missing))}")
            audit.require(isinstance(data.get("overall"), str) and bool(data["overall"]), f"{label}.data.overall is invalid")
    else:
        audit.require(value.get("ok") is False, f"{label} must fail closed")


def _target_path(root: Path, source_root: Path, target: str) -> tuple[Path, str] | None:
    if not isinstance(target, str) or target.count(":") != 1:
        return None
    module_name, attribute = target.split(":", 1)
    if not module_name or not attribute or not all(part.isidentifier() for part in module_name.split(".")):
        return None
    if not all(part.isidentifier() for part in attribute.split(".")):
        return None
    relative = Path(*module_name.split("."))
    module_path = source_root / f"{relative}.py"
    if not module_path.is_file():
        module_path = source_root / relative / "__init__.py"
    if not module_path.is_file() or module_path.is_symlink():
        return None
    return module_path, attribute


def _load_target(root: Path, source_root: Path, target: str) -> Any | None:
    resolved = _target_path(root, source_root, target)
    if resolved is None:
        return None
    _module_path, attribute = resolved
    module_name, _ = target.split(":", 1)
    package_name = module_name.split(".", 1)[0]
    for loaded_name, loaded_module in list(sys.modules.items()):
        loaded_file = getattr(loaded_module, "__file__", None)
        if loaded_name == package_name or loaded_name.startswith(package_name + ".") or (
            isinstance(loaded_file, str) and Path(loaded_file).is_relative_to(source_root)
        ):
            sys.modules.pop(loaded_name, None)
    sys.path.insert(0, str(source_root))
    try:
        importlib.invalidate_caches()
        value: Any = importlib.import_module(module_name)
        for part in attribute.split("."):
            value = getattr(value, part)
        return value
    except (AttributeError, ImportError, ModuleNotFoundError, OSError, TypeError, ValueError):
        return None
    finally:
        try:
            sys.path.remove(str(source_root))
        except ValueError:
            pass


def _check_entry_point(audit: Audit, project: dict[str, Any], source_root: Path, plugin_id: str) -> None:
    groups = project.get("project", {}).get("entry-points", {})
    entry_points = groups.get("dispatch.plugins") if isinstance(groups, dict) else None
    if not audit.require(isinstance(entry_points, dict), "pyproject must declare [project.entry-points.\"dispatch.plugins\"]"):
        return
    assert isinstance(entry_points, dict)
    if not audit.require(set(entry_points) == {plugin_id}, "dispatch.plugins must contain exactly the declared plugin id"):
        return
    target = entry_points.get(plugin_id)
    if not audit.require(isinstance(target, str) and bool(target), "dispatch.plugins entry point target is invalid"):
        return
    assert isinstance(target, str)
    handler = _load_target(audit.root, source_root, target)
    if not audit.require(callable(handler), "dispatch.plugins entry point target is not a source callable"):
        return
    try:
        response = handler({"action": "health"})
        invalid_response = handler({"action": "__invalid__"})
    except Exception as exc:  # pragma: no cover - exercised by plugin-specific failures
        audit.fail(f"dispatch.plugins request probe failed: {type(exc).__name__}")
        return
    _check_envelope(audit, response, "dispatch.plugins health response", health=True)
    _check_envelope(audit, invalid_response, "dispatch.plugins invalid-input response")


def _check_auxiliary_entry_points(
    audit: Audit,
    project: dict[str, Any],
    source_root: Path,
    plugin_id: str,
    capabilities: list[str],
) -> None:
    groups = project.get("project", {}).get("entry-points", {})
    if not isinstance(groups, dict):
        return
    service_points = groups.get("dispatch.services", {})
    if not isinstance(service_points, dict):
        audit.fail("dispatch.services entry points are invalid")
        return
    long_running = "long_running" in capabilities
    expected_services = {plugin_id} if long_running else set()
    if set(service_points) != expected_services:
        audit.fail(
            "long_running capability must match exactly one same-ID dispatch.services entry point"
        )
    elif long_running:
        target = service_points.get(plugin_id)
        handler = _load_target(audit.root, source_root, target) if isinstance(target, str) else None
        if not callable(handler):
            audit.fail("dispatch.services entry point target is not a source callable")
        else:
            try:
                inspect.signature(handler).bind(object())
            except (TypeError, ValueError):
                audit.fail("dispatch.services entry point must accept one context argument")

    configurators = groups.get("dispatch.configurators", {})
    if not isinstance(configurators, dict) or set(configurators) not in (set(), {plugin_id}):
        audit.fail("dispatch.configurators must be empty or contain exactly the declared plugin id")
    elif configurators:
        target = configurators.get(plugin_id)
        handler = _load_target(audit.root, source_root, target) if isinstance(target, str) else None
        if not callable(handler):
            audit.fail("dispatch.configurators entry point target is not a source callable")
        else:
            try:
                inspect.signature(handler).bind(object())
            except (TypeError, ValueError):
                audit.fail("dispatch.configurators entry point must accept one context argument")


def _manifest_actions(manifest: dict[str, Any], component_id: str | None = None) -> set[str] | None:
    components = manifest.get("components")
    if not isinstance(components, list):
        return None
    for component in components:
        if not isinstance(component, dict) or (component_id and component.get("id") != component_id):
            continue
        actions = component.get("actions")
        if not isinstance(actions, list):
            return None
        values: set[str] = set()
        for action in actions:
            if not isinstance(action, dict) or not isinstance(action.get("name"), str):
                return None
            values.add(action["name"])
        return values
    return None


def _registration_probe(adapter: Path, source_root: Path) -> tuple[list[dict[str, Any]] | None, str | None]:
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
                json.dumps(value, allow_nan=False)
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
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": os.environ.get("PATH", os.defpath),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join(filter(None, [str(source_root), os.environ.get("PYTHONPATH", "")])),
    }
    try:
        completed = subprocess.run(
            [sys.executable, "-B", "-c", helper, str(adapter)],
            cwd=adapter.parents[3] if len(adapter.parents) > 3 else adapter.parent,
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


def _manifest_tool(manifest: dict[str, Any]) -> tuple[dict[str, Any], set[str]] | None:
    components = manifest.get("components")
    if not isinstance(components, list):
        return None
    for component in components:
        if not isinstance(component, dict) or not isinstance(component.get("hermes"), dict):
            continue
        hermes = component["hermes"]
        if not all(isinstance(hermes.get(key), str) for key in ("manifest", "adapter", "tool", "toolset")):
            return None
        actions = _manifest_actions(manifest, component.get("id"))
        return hermes, actions or set()
    return None


def _manifest_actions_from_schema(schema: Any) -> set[str] | None:
    if not isinstance(schema, dict):
        return None
    parameters = schema.get("parameters")
    if not isinstance(parameters, dict):
        return None
    if parameters.get("type") != "object" or parameters.get("additionalProperties") is not False or parameters.get("required") != ["action"]:
        return None
    properties = parameters.get("properties")
    if not isinstance(properties, dict) or "action" not in properties:
        return None
    action = properties["action"]
    values = action.get("enum") if isinstance(action, dict) else None
    if not isinstance(action, dict) or action.get("type") != "string" or not isinstance(values, list) or not values:
        return None
    if any(not isinstance(item, str) or not ACTION_ID.fullmatch(item) for item in values):
        return None
    return set(values)


def _check_tool(audit: Audit, source_root: Path, manifest: dict[str, Any]) -> None:
    tool_info = _manifest_tool(manifest)
    if tool_info is None:
        return
    hermes, declared_actions = tool_info
    plugin_manifest = audit.relative(hermes["manifest"], "Hermes plugin manifest")
    adapter = audit.relative(hermes["adapter"], "Hermes adapter")
    if plugin_manifest and plugin_manifest.is_file():
        try:
            payload = _load_yaml(plugin_manifest)
        except (OSError, yaml.YAMLError):
            payload = None
            audit.fail("Hermes plugin manifest is invalid YAML")
        if isinstance(payload, dict):
            audit.require(payload.get("provides_tools") == [hermes["tool"]], "Hermes provides_tools does not match the declared tool")
    if not adapter or not adapter.is_file():
        return
    try:
        ast.parse(adapter.read_text(encoding="utf-8"), filename=str(adapter))
    except (OSError, SyntaxError):
        audit.fail("Hermes adapter syntax is invalid")
        return
    registrations, error = _registration_probe(adapter, source_root)
    if error:
        audit.fail(f"Hermes {error}")
        return
    if not audit.require(isinstance(registrations, list) and len(registrations) == 1, "Hermes adapter must register exactly one tool"):
        return
    assert registrations is not None
    registration = registrations[0]
    audit.require(registration.get("name") == hermes["tool"], "registered tool name does not match")
    audit.require(registration.get("toolset") == hermes["toolset"], "registered toolset does not match")
    actual_actions = _manifest_actions_from_schema(registration.get("schema"))
    audit.require(actual_actions is not None, "tool schema must require action, close properties, and define an action enum")
    if actual_actions is not None and declared_actions:
        audit.require(actual_actions == declared_actions, "declared actions do not match the registered tool schema")
    audit.require(isinstance(registration.get("available"), bool), "tool availability check did not return a boolean")
    _check_envelope(audit, registration.get("invalid_response"), "invalid-input response")
    if actual_actions is not None and "health" in actual_actions:
        _check_envelope(audit, registration.get("health_response"), "health response", health=True)


def audit_owner(root: Path) -> Audit:
    audit = Audit(root)
    if not audit.require(root.is_dir(), f"owner root is not a directory: {root}"):
        return audit
    pyproject_path = root / "pyproject.toml"
    if not audit.require(pyproject_path.is_file(), "missing pyproject.toml"):
        return audit
    try:
        project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        audit.fail("pyproject.toml is invalid TOML")
        return audit
    if not audit.require(isinstance(project, dict), "pyproject.toml must contain a table"):
        return audit

    tool = project.get("tool")
    dispatch = tool.get("dispatch") if isinstance(tool, dict) else None
    if not audit.require(isinstance(dispatch, dict), "pyproject must declare [tool.dispatch]"):
        return audit
    plugin_id = dispatch.get("id")
    if not audit.require(isinstance(plugin_id, str) and bool(PLUGIN_ID.fullmatch(plugin_id)), "tool.dispatch.id is invalid"):
        return audit
    capabilities = dispatch.get("capabilities")
    if audit.require(isinstance(capabilities, list) and bool(capabilities), "tool.dispatch.capabilities must be a non-empty list"):
        audit.require(
            all(isinstance(value, str) and value in CAPABILITIES for value in capabilities)
            and len(capabilities) == len(set(capabilities)),
            "tool.dispatch.capabilities contains invalid or duplicate values",
        )
    audit.require(root.name == plugin_id, f"tool.dispatch.id {plugin_id} does not match the source directory {root.name}")

    manifest_path = root / "dispatch-plugin.yaml"
    manifest: dict[str, Any] | None = None
    if manifest_path.exists():
        try:
            loaded = _load_yaml(manifest_path)
        except (OSError, ValueError, yaml.YAMLError):
            loaded = None
            audit.fail("dispatch-plugin.yaml is invalid YAML")
        if isinstance(loaded, dict):
            manifest = loaded
            audit.require(manifest.get("id") == plugin_id, "dispatch-plugin.yaml id must match tool.dispatch.id")
        else:
            audit.fail("dispatch-plugin.yaml must contain a mapping")

    source_root = _source_root(root, project)
    _check_entry_point(audit, project, source_root, plugin_id)
    if isinstance(capabilities, list) and all(isinstance(value, str) for value in capabilities):
        _check_auxiliary_entry_points(audit, project, source_root, plugin_id, capabilities)

    scripts = root / "scripts"
    if audit.require(scripts.is_dir() and not scripts.is_symlink(), "missing scripts directory"):
        for name in ("test", "build", "verify", "health"):
            script = scripts / name
            if audit.require(script.is_file() and not script.is_symlink(), f"missing regular scripts/{name}"):
                mode = stat.S_IMODE(script.stat().st_mode)
                audit.require(bool(mode & stat.S_IXUSR), f"scripts/{name} is not owner-executable")
                audit.require(not bool(mode & 0o022), f"scripts/{name} is group/world writable")
        for script in scripts.iterdir():
            if script.is_file() and not script.is_symlink():
                audit.require(not bool(stat.S_IMODE(script.stat().st_mode) & 0o022), f"{script.relative_to(root)} is group/world writable")

    if manifest is not None:
        _check_tool(audit, _source_root(root, project), manifest)

    if not audit.failures:
        audit.passed("pyproject Dispatch metadata and entry point")
        audit.passed("optional manifest identity")
        audit.passed("source lifecycle scripts and permissions")
        audit.passed("exact response envelope and tool schema")
    return audit


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("owner", type=Path)
    args = parser.parse_args()
    audit = audit_owner(args.owner.resolve())
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
