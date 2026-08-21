from __future__ import annotations

import importlib.util
from pathlib import Path
import stat
import textwrap

import yaml

SCRIPT = Path(__file__).resolve().parents[2] / "plugin_policy.py"
SPEC = importlib.util.spec_from_file_location("dispatch_plugin_policy", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def fixture(tmp_path: Path) -> Path:
    root = tmp_path / "example"
    (root / "src/example_plugin").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "integration/hermes-plugins/example_plugin").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "src/example_plugin/__init__.py").write_text("\n", encoding="utf-8")
    (root / "src/example_plugin/service.py").write_text(
        textwrap.dedent(
            """
            def envelope(action, ok=True):
                return {
                    "ok": ok,
                    "action": action,
                    "status": "ready" if ok else "error",
                    "data": {"registration": "ready", "runtime_integrity": "ready", "query": "ready", "data": "ready", "freshness": "ready", "collector": "not_applicable", "authentication": "not_applicable", "delivery": "not_applicable", "overall": "ready"} if action == "health" else {},
                    "freshness": None,
                    "delivery": None,
                    "error": None if ok else {"code": "invalid_input", "message": "invalid"},
                }
            def handle(request):
                action = request.get("action") if isinstance(request, dict) else "invalid"
                return envelope(action if isinstance(action, str) else "invalid", action == "health")
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [build-system]
            requires = ["setuptools==83.0.0"]
            build-backend = "setuptools.build_meta"

            [project]
            name = "dispatch-example"
            version = "0.1.0"
            requires-python = ">=3.11"

            [project.entry-points."dispatch.plugins"]
            example = "example_plugin.service:handle"

            [tool.dispatch]
            id = "example"
            capabilities = ["read_local_data"]

            [tool.setuptools]
            package-dir = {"" = "src"}
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (root / "dispatch-plugin.yaml").write_text(
        textwrap.dedent(
            """
            id: example
            components:
              - id: example-tool
                hermes:
                  manifest: integration/hermes-plugins/example_plugin/plugin.yaml
                  adapter: integration/hermes-plugins/example_plugin/__init__.py
                  tool: dispatch_example
                  toolset: dispatch_example
                actions:
                  - {name: health, privilege: health}
                  - {name: summary, privilege: read}
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (root / "integration/hermes-plugins/example_plugin/plugin.yaml").write_text(
        "provides_tools: [dispatch_example]\n",
        encoding="utf-8",
    )
    (root / "integration/hermes-plugins/example_plugin/__init__.py").write_text(
        textwrap.dedent(
            """
            import json
            SCHEMA = {
                "name": "dispatch_example",
                "description": "Read example data.",
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"action": {"type": "string", "enum": ["health", "summary"]}},
                    "required": ["action"],
                },
            }
            def handler(args):
                action = args.get("action", "invalid") if isinstance(args, dict) else "invalid"
                ok = action == "health"
                data = {key: "ready" for key in ("registration", "runtime_integrity", "query", "data", "freshness", "collector", "authentication", "delivery", "overall")} if ok else {}
                return json.dumps({"ok": ok, "action": action, "status": "ready" if ok else "error", "data": data, "freshness": None, "delivery": None, "error": None if ok else {"code": "invalid_input", "message": "invalid"}})
            def register(ctx):
                ctx.register_tool(name="dispatch_example", toolset="dispatch_example", schema=SCHEMA, handler=handler, check_fn=lambda: True)
            """
        ).lstrip(),
        encoding="utf-8",
    )
    for name in ("test", "build", "verify", "health"):
        script = root / "scripts" / name
        script.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
    return root


def test_source_metadata_and_contract_conform(tmp_path: Path) -> None:
    result = MODULE.audit_owner(fixture(tmp_path))
    assert result.failures == []
    assert any("source lifecycle scripts" in value for value in result.passes)


def test_manifest_id_must_match_pyproject_metadata(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    manifest = yaml.safe_load((root / "dispatch-plugin.yaml").read_text(encoding="utf-8"))
    manifest["id"] = "other"
    (root / "dispatch-plugin.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")

    result = MODULE.audit_owner(root)

    assert any("dispatch-plugin.yaml id must match" in failure for failure in result.failures)


def test_group_writable_script_fails(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    (root / "scripts/verify").chmod(stat.S_IRWXU | stat.S_IRWXG)

    result = MODULE.audit_owner(root)

    assert any("scripts/verify is group/world writable" in failure for failure in result.failures)


def test_build_backend_must_be_exactly_pinned(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    project = root / "pyproject.toml"
    project.write_text(
        project.read_text(encoding="utf-8").replace(
            'requires = ["setuptools==83.0.0"]',
            'requires = ["setuptools>=68"]',
        ),
        encoding="utf-8",
    )

    result = MODULE.audit_owner(root)

    assert any("must use pinned setuptools==83.0.0" in value for value in result.failures)


def test_entry_point_metadata_is_required(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    (root / "pyproject.toml").write_text(text.replace('example = "example_plugin.service:handle"', 'other = "example_plugin.service:handle"'), encoding="utf-8")

    result = MODULE.audit_owner(root)

    assert any("dispatch.plugins must contain exactly" in failure for failure in result.failures)


def test_exact_tool_schema_is_required(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    adapter = root / "integration/hermes-plugins/example_plugin/__init__.py"
    text = adapter.read_text(encoding="utf-8").replace('"additionalProperties": False', '"additionalProperties": True')
    adapter.write_text(text, encoding="utf-8")

    result = MODULE.audit_owner(root)

    assert any("tool schema must require action" in failure for failure in result.failures)


def test_flat_response_envelope_fails(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    service = root / "src/example_plugin/service.py"
    text = service.read_text(encoding="utf-8").replace('return envelope(action if isinstance(action, str) else "invalid", action == "health")', 'return {"ok": True}')
    service.write_text(text, encoding="utf-8")

    result = MODULE.audit_owner(root)

    assert any("exact seven-field response envelope" in failure for failure in result.failures)


def test_long_running_capability_requires_same_id_service_entry_point(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    project = root / "pyproject.toml"
    text = project.read_text(encoding="utf-8").replace(
        'capabilities = ["read_local_data"]',
        'capabilities = ["read_local_data", "long_running"]',
    )
    project.write_text(text, encoding="utf-8")

    result = MODULE.audit_owner(root)

    assert any("long_running capability must match" in failure for failure in result.failures)


def test_collect_capability_requires_same_id_collector_provider(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    project = root / "pyproject.toml"
    text = project.read_text(encoding="utf-8").replace(
        'capabilities = ["read_local_data"]',
        'capabilities = ["read_local_data", "collect"]',
    )
    project.write_text(text, encoding="utf-8")

    result = MODULE.audit_owner(root)

    assert any("collect capability must match" in failure for failure in result.failures)


def test_authentication_metadata_must_match_capability(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    project = root / "pyproject.toml"
    text = project.read_text(encoding="utf-8")
    text += (
        "\n[tool.dispatch.authentication]\n"
        'required_profiles = [{ provider = "amazon-operations" }]\n'
    )
    project.write_text(text, encoding="utf-8")

    result = MODULE.audit_owner(root)

    assert any("authentication capability must declare" in failure for failure in result.failures)


def test_malformed_capabilities_fail_without_crashing_auth_policy(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    project = root / "pyproject.toml"
    text = project.read_text(encoding="utf-8").replace(
        'capabilities = ["read_local_data"]',
        'capabilities = "read_local_data"',
    )
    project.write_text(text, encoding="utf-8")

    result = MODULE.audit_owner(root)

    assert any("capabilities must be a non-empty list" in failure for failure in result.failures)


def test_collector_provider_is_loadable_and_accepts_no_arguments(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    service = root / "src/example_plugin/service.py"
    service.write_text(
        service.read_text(encoding="utf-8") + "\ndef registrations():\n    return ()\n",
        encoding="utf-8",
    )
    project = root / "pyproject.toml"
    text = project.read_text(encoding="utf-8").replace(
        'capabilities = ["read_local_data"]',
        'capabilities = ["read_local_data", "collect"]',
    )
    text += (
        '\n[project.entry-points."dispatch.collectors"]\n'
        'example = "example_plugin.service:registrations"\n'
    )
    project.write_text(text, encoding="utf-8")

    result = MODULE.audit_owner(root)

    assert result.failures == []

    service.write_text(
        service.read_text(encoding="utf-8").replace("def registrations():", "def registrations(context):"),
        encoding="utf-8",
    )
    result = MODULE.audit_owner(root)
    assert any("dispatch.collectors entry point must accept no arguments" in failure for failure in result.failures)


def test_service_and_configurator_entry_points_must_be_loadable(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    service = root / "src/example_plugin/service.py"
    service.write_text(
        service.read_text(encoding="utf-8")
        + "\ndef serve(context):\n    return None\n"
        + "\ndef configure(context):\n    return envelope('configure')\n",
        encoding="utf-8",
    )
    project = root / "pyproject.toml"
    text = project.read_text(encoding="utf-8").replace(
        'capabilities = ["read_local_data"]',
        'capabilities = ["read_local_data", "long_running"]',
    )
    text += (
        '\n[project.entry-points."dispatch.services"]\n'
        'example = "example_plugin.service:serve"\n'
        '\n[project.entry-points."dispatch.configurators"]\n'
        'example = "example_plugin.service:configure"\n'
    )
    project.write_text(text, encoding="utf-8")

    result = MODULE.audit_owner(root)

    assert result.failures == []


def test_auxiliary_entry_point_must_accept_context(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    service = root / "src/example_plugin/service.py"
    service.write_text(
        service.read_text(encoding="utf-8") + "\ndef serve():\n    return None\n",
        encoding="utf-8",
    )
    project = root / "pyproject.toml"
    text = project.read_text(encoding="utf-8").replace(
        'capabilities = ["read_local_data"]',
        'capabilities = ["read_local_data", "long_running"]',
    )
    text += (
        '\n[project.entry-points."dispatch.services"]\n'
        'example = "example_plugin.service:serve"\n'
    )
    project.write_text(text, encoding="utf-8")

    result = MODULE.audit_owner(root)

    assert any("must accept one context argument" in failure for failure in result.failures)


def test_entry_point_rejects_symlinked_source_parent(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (external / "__init__.py").write_text("", encoding="utf-8")
    (external / "service.py").write_text(
        "def handle(request):\n    return {}\n",
        encoding="utf-8",
    )
    (root / "src" / "linked").symlink_to(external, target_is_directory=True)
    project = root / "pyproject.toml"
    project.write_text(
        project.read_text(encoding="utf-8").replace(
            'example = "example_plugin.service:handle"',
            'example = "linked.service:handle"',
        ),
        encoding="utf-8",
    )

    result = MODULE.audit_owner(root)

    assert any("entry point target is not a source callable" in failure for failure in result.failures)


def test_policy_bounds_plugin_base_exception(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    service = root / "src/example_plugin/service.py"
    service.write_text(
        service.read_text(encoding="utf-8").replace(
            "def handle(request):\n    action = request.get(\"action\") if isinstance(request, dict) else \"invalid\"",
            "def handle(request):\n    raise KeyboardInterrupt",
        ),
        encoding="utf-8",
    )

    result = MODULE.audit_owner(root)

    assert any("request probe failed: KeyboardInterrupt" in failure for failure in result.failures)


def test_manifest_is_optional_for_source_plugin(tmp_path: Path) -> None:
    root = fixture(tmp_path)
    (root / "dispatch-plugin.yaml").unlink()
    result = MODULE.audit_owner(root)
    assert result.failures == []
