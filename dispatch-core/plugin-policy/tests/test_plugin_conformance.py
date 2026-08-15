from __future__ import annotations

import importlib.util
import hashlib
from pathlib import Path
import shutil
import stat
import textwrap
import json
import zipfile

import yaml

SCRIPT = Path(__file__).resolve().parents[1] / "plugin_conformance.py"
SPEC = importlib.util.spec_from_file_location("dispatch_plugin_conformance", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
TEMPLATE = Path(__file__).resolve().parents[3] / "SKILL/dispatch-plugin-development/templates/dispatch-plugin.yaml"


def fixture(tmp_path: Path) -> Path:
    root = tmp_path / "example-plugin"
    (root / "src/dispatch_example_plugin").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "integration/hermes-plugins/dispatch_example_plugin").mkdir(parents=True)
    (root / "runtime/releases/example").mkdir(parents=True)
    (root / "config/release-manifests").mkdir(parents=True)
    (root / "scripts").mkdir()
    shutil.copyfile(TEMPLATE, root / "dispatch-plugin.yaml")
    (root / "README.md").write_text("# Example Plugin\n", encoding="utf-8")
    (root / "src/dispatch_example_plugin/__init__.py").write_text("", encoding="utf-8")
    (root / "tests/test_example.py").write_text("def test_example(): assert True\n", encoding="utf-8")
    (root / "runtime/current").write_text("releases/example\n", encoding="utf-8")
    (root / "config/release-manifests/dispatch-example-plugin.yaml").write_text("release: example\n", encoding="utf-8")
    (root / "integration/hermes-plugins/dispatch_example_plugin/launcher-manifest.json").write_text("{}\n", encoding="utf-8")
    (root / "integration/hermes-plugins/dispatch_example_plugin/plugin.yaml").write_text(
        "name: dispatch_example_plugin\nversion: 1.0.0\nprovides_tools: [dispatch_example_plugin]\n",
        encoding="utf-8",
    )
    (root / "integration/hermes-plugins/dispatch_example_plugin/__init__.py").write_text(
        textwrap.dedent(
            """
            SCHEMA = {
                "name": "dispatch_example_plugin",
                "description": "Read example data.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"action": {"type": "string", "enum": ["health", "summary"]}},
                    "required": ["action"],
                },
            }
            import json
            def handler(args, **kwargs):
                action = args.get("action", "unknown") if isinstance(args, dict) else "unknown"
                if action == "health":
                    data = {key: "ready" for key in (
                        "registration", "runtime_integrity", "query", "data", "freshness",
                        "collector", "authentication", "delivery", "overall",
                    )}
                    return json.dumps({"ok": True, "action": action, "status": "ready", "data": data,
                                       "freshness": None, "delivery": None, "error": None})
                if action == "summary":
                    return json.dumps({"ok": True, "action": action, "status": "ready", "data": {},
                                       "freshness": None, "delivery": None, "error": None})
                return json.dumps({"ok": False, "action": action, "status": "error", "data": {},
                                   "freshness": None, "delivery": None,
                                   "error": {"code": "invalid_input", "message": "Invalid request."}})
            def register(ctx):
                ctx.register_tool(
                    name="dispatch_example_plugin",
                    toolset="dispatch_example_plugin",
                    schema=SCHEMA,
                    handler=handler,
                    check_fn=lambda: True,
                )
            """
        ).lstrip(),
        encoding="utf-8",
    )
    for name in ("test", "build", "verify", "health"):
        script = root / "scripts" / name
        script.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
    return root


def test_minimal_agent_template_conforms(tmp_path: Path) -> None:
    result = MODULE.audit_owner(fixture(tmp_path))
    assert result.failures == []


def test_action_schema_drift_fails(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    manifest = yaml.safe_load((root / "dispatch-plugin.yaml").read_text(encoding="utf-8"))
    manifest["components"][0]["actions"] = [{"name": "summary", "privilege": "read"}]
    (root / "dispatch-plugin.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    result = MODULE.audit_owner(root)
    assert any("declared actions do not match registered schema" in failure for failure in result.failures)


def test_flat_response_envelope_fails(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    adapter = root / "integration/hermes-plugins/dispatch_example_plugin/__init__.py"
    text = adapter.read_text(encoding="utf-8")
    old = '''return json.dumps({"ok": False, "action": action, "status": "error", "data": {},
                       "freshness": None, "delivery": None,
                       "error": {"code": "invalid_input", "message": "Invalid request."}})'''
    assert old in text
    text = text.replace(old, 'return json.dumps({"ok": False, "status": "invalid_input"})')
    adapter.write_text(text, encoding="utf-8")
    result = MODULE.audit_owner(root)
    assert any("exact seven-field response envelope" in failure for failure in result.failures)


def test_health_missing_readiness_plane_fails(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    adapter = root / "integration/hermes-plugins/dispatch_example_plugin/__init__.py"
    text = adapter.read_text(encoding="utf-8").replace('"registration", "runtime_integrity", "query", "data", "freshness",', '"runtime_integrity", "query", "data", "freshness",')
    adapter.write_text(text, encoding="utf-8")
    result = MODULE.audit_owner(root)
    assert any("missing readiness planes: registration" in failure for failure in result.failures)


def test_mutating_action_requires_declared_capability(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    manifest = yaml.safe_load((root / "dispatch-plugin.yaml").read_text(encoding="utf-8"))
    manifest["components"][0]["actions"] = [{"name": "status", "privilege": "mutation"}]
    (root / "dispatch-plugin.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    result = MODULE.audit_owner(root)
    assert any("mutating action lacks mutation/collection capability" in failure for failure in result.failures)


def test_group_writable_lifecycle_script_fails(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    script = root / "scripts/verify"
    script.chmod(stat.S_IRWXU | stat.S_IRWXG)
    result = MODULE.audit_owner(root)
    assert any("scripts/verify is group/world writable" in failure for failure in result.failures)


def test_active_release_with_volatile_member_fails(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    active_dir = root / "runtime/releases/deadbeef"
    active_dir.mkdir()
    active = active_dir / "example"
    with zipfile.ZipFile(active, "w") as archive:
        archive.writestr("package/__pycache__/module.pyc", b"cache")
    digest = MODULE._sha256(active)
    activation = {
        "runtime": {"path": str(active), "sha256": digest, "release": "deadbeef"},
    }
    activation_path = root / "config/release-manifests/dispatch-example-plugin.json"
    activation_path.write_text(json.dumps(activation), encoding="utf-8")
    manifest = yaml.safe_load((root / "dispatch-plugin.yaml").read_text(encoding="utf-8"))
    manifest["components"][0]["runtime"]["activation_record"] = "config/release-manifests/dispatch-example-plugin.json"
    (root / "dispatch-plugin.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    result = MODULE.audit_owner(root)
    assert any("active runtime includes volatile member" in failure for failure in result.failures)


def test_multi_interface_activation_checks_explicit_rollback(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    active_bytes = b"active"
    active_release = hashlib.sha256(active_bytes).hexdigest()[:24]
    active_dir = root / "runtime/releases" / active_release
    rollback_dir = root / "runtime/releases/legacy-name"
    active_dir.mkdir()
    rollback_dir.mkdir()
    active = active_dir / "example"
    rollback = rollback_dir / "example"
    active.write_bytes(active_bytes)
    rollback.write_bytes(b"rollback")
    active.chmod(0o555)
    rollback.chmod(0o555)
    activation = {
        "interfaces": {
            "query": {
                "runtime": {
                    "path": str(active),
                    "sha256": MODULE._sha256(active),
                    "release": active_release,
                },
                "rollback": {
                    "path": str(rollback),
                    "sha256": MODULE._sha256(rollback),
                    "release": "legacy-name",
                },
            }
        }
    }
    activation_path = root / "config/release-manifests/dispatch-example-plugin.json"
    activation_path.write_text(json.dumps(activation), encoding="utf-8")
    manifest = yaml.safe_load((root / "dispatch-plugin.yaml").read_text(encoding="utf-8"))
    manifest["components"][0]["runtime"]["activation_record"] = "config/release-manifests/dispatch-example-plugin.json"
    (root / "dispatch-plugin.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    result = MODULE.audit_owner(root)
    assert result.failures == []

    rollback.chmod(0o700)
    rollback.write_bytes(b"tampered")
    result = MODULE.audit_owner(root)
    assert any("rollback release is missing or inconsistent" in failure for failure in result.failures)


def test_directory_runtime_and_rollback_use_sealed_manifest_digests(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    active = root / "runtime/releases/active-directory"
    rollback = root / "runtime/releases/rollback-directory"
    for release in (active, rollback):
        release.mkdir()
        member = release / "payload.py"
        member.write_text("VALUE = 1\n", encoding="utf-8")
        manifest_payload = {
            "version": 1,
            "bundle_digest": release.name,
            "files": [{"path": "payload.py", "sha256": MODULE._sha256(member), "size": member.stat().st_size, "mode": "0644"}],
        }
        (release / "release-manifest.json").write_text(json.dumps(manifest_payload), encoding="utf-8")
        member.chmod(0o444)
        (release / "release-manifest.json").chmod(0o444)
        release.chmod(0o555)
    activation = {
        "interfaces": {
            "bundle": {
                "runtime": {"path": str(active), "sha256": MODULE._sha256(active / "release-manifest.json"), "release": active.name},
                "rollback": {"path": str(rollback), "sha256": MODULE._sha256(rollback / "release-manifest.json"), "release": rollback.name},
            }
        }
    }
    activation_path = root / "config/release-manifests/dispatch-example-plugin.json"
    activation_path.write_text(json.dumps(activation), encoding="utf-8")
    manifest = yaml.safe_load((root / "dispatch-plugin.yaml").read_text(encoding="utf-8"))
    manifest["components"][0]["runtime"]["activation_record"] = "config/release-manifests/dispatch-example-plugin.json"
    (root / "dispatch-plugin.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    result = MODULE.audit_owner(root)
    assert result.failures == []

    (rollback / "release-manifest.json").chmod(0o600)
    (rollback / "release-manifest.json").write_text("{}", encoding="utf-8")
    result = MODULE.audit_owner(root)
    assert any("rollback release is missing or inconsistent" in failure for failure in result.failures)


def test_directory_runtime_manifest_name_is_supported(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    active = root / "runtime/releases/active-runtime"
    rollback = root / "runtime/releases/rollback-runtime"
    for release in (active, rollback):
        release.mkdir()
        member = release / "payload.py"
        member.write_text("VALUE = 1\n", encoding="utf-8")
        manifest_payload = {
            "contract_version": 1,
            "release_id": release.name,
            "files": [{"path": "payload.py", "sha256": MODULE._sha256(member), "size": member.stat().st_size, "mode": "0444"}],
        }
        (release / "runtime-manifest.json").write_text(json.dumps(manifest_payload), encoding="utf-8")
        member.chmod(0o444)
        (release / "runtime-manifest.json").chmod(0o444)
        release.chmod(0o555)
    activation = {
        "interfaces": {
            "bundle": {
                "runtime": {"path": str(active), "sha256": MODULE._sha256(active / "runtime-manifest.json"), "release": active.name},
                "rollback": {"path": str(rollback), "sha256": MODULE._sha256(rollback / "runtime-manifest.json"), "release": rollback.name},
            }
        }
    }
    activation_path = root / "config/release-manifests/dispatch-example-plugin.json"
    activation_path.write_text(json.dumps(activation), encoding="utf-8")
    manifest = yaml.safe_load((root / "dispatch-plugin.yaml").read_text(encoding="utf-8"))
    manifest["components"][0]["runtime"]["activation_record"] = "config/release-manifests/dispatch-example-plugin.json"
    (root / "dispatch-plugin.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    result = MODULE.audit_owner(root)
    assert result.failures == []


def test_named_component_stores_bind_to_component_identity(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    manifest = yaml.safe_load((root / "dispatch-plugin.yaml").read_text(encoding="utf-8"))
    manifest["paths"].pop("database", None)
    manifest["paths"]["databases"] = {
        "example-roster": "db/example-roster",
        "example-timecards": "db/example-timecards",
    }
    (root / "dispatch-plugin.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    result = MODULE.audit_owner(root)
    assert result.failures == []

    manifest["paths"]["databases"]["example-timecards"] = "db/other-owner"
    (root / "dispatch-plugin.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    result = MODULE.audit_owner(root)
    assert any("paths.databases.example-timecards must be owned beneath db/example-timecards" in failure for failure in result.failures)


def test_directory_release_manifest_binds_nested_launcher(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    release = root / "runtime/releases/release-v1"
    (release / "bin").mkdir(parents=True)
    launcher = release / "bin/example"
    launcher.write_bytes(b"launcher")
    files = [{"path": "bin/example", "sha256": MODULE._sha256(launcher), "mode": "0555"}]
    runtime_manifest = release / "runtime-manifest.json"
    runtime_manifest.write_text(json.dumps({"version": 1, "files": files}), encoding="utf-8")
    launcher.chmod(0o555)
    runtime_manifest.chmod(0o444)
    release.chmod(0o555)
    activation = {
        "interfaces": {
            "service": {
                "runtime": {"path": str(launcher), "sha256": MODULE._sha256(launcher), "release": "release-v1"},
                "runtime_manifest": {"path": str(runtime_manifest), "sha256": MODULE._sha256(runtime_manifest)},
                "rollback": {"path": str(launcher), "sha256": MODULE._sha256(launcher), "release": "release-v1"},
            }
        }
    }
    activation_path = root / "config/release-manifests/dispatch-example-plugin.json"
    activation_path.write_text(json.dumps(activation), encoding="utf-8")
    manifest = yaml.safe_load((root / "dispatch-plugin.yaml").read_text(encoding="utf-8"))
    manifest["components"][0]["runtime"]["activation_record"] = "config/release-manifests/dispatch-example-plugin.json"
    (root / "dispatch-plugin.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    result = MODULE.audit_owner(root)
    assert result.failures == []

    launcher.chmod(0o700)
    launcher.write_bytes(b"tampered")
    result = MODULE.audit_owner(root)
    assert any("active runtime digest mismatch" in failure for failure in result.failures)


def test_owner_writable_selected_runtime_fails(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    release = root / "runtime/releases/deadbeef"
    release.mkdir()
    runtime = release / "example"
    runtime.write_bytes(b"runtime")
    activation = {"runtime": {"path": str(runtime), "sha256": MODULE._sha256(runtime), "release": "deadbeef"}}
    activation_path = root / "config/release-manifests/dispatch-example-plugin.json"
    activation_path.write_text(json.dumps(activation), encoding="utf-8")
    manifest = yaml.safe_load((root / "dispatch-plugin.yaml").read_text(encoding="utf-8"))
    manifest["components"][0]["runtime"]["activation_record"] = "config/release-manifests/dispatch-example-plugin.json"
    (root / "dispatch-plugin.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")

    result = MODULE.audit_owner(root)

    assert any("active runtime is owner-writable" in failure for failure in result.failures)
