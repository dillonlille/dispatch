from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import yaml

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = ROOT / "docs" / "schemas" / "dispatch-plugin-v1.schema.json"
MANIFESTS = (
    ROOT / "SKILL" / "dispatch-plugin-development" / "templates" / "dispatch-plugin.yaml",
    ROOT / "plugins" / "handbook" / "dispatch-plugin.yaml",
    ROOT / "plugins" / "paycom" / "dispatch-plugin.yaml",
)


def test_source_plugin_manifests_conform_to_optional_source_schema() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    for manifest_path in MANIFESTS:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        errors = sorted(validator.iter_errors(manifest), key=lambda error: list(error.path))
        assert errors == [], [error.message for error in errors]


def test_source_schema_contains_no_runtime_activation_authority() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    rendered = json.dumps(schema, sort_keys=True)
    for obsolete in (
        "activation_record",
        "current_pointer",
        "launcher_manifest",
        "keep_rollback",
        "preserve_pinned",
    ):
        assert obsolete not in rendered

    path_properties = schema["properties"]["paths"]["properties"]
    assert set(path_properties) == {
        "source",
        "tests",
        "data",
        "references",
        "hermes_integration",
    }
