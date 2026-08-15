from __future__ import annotations

import json
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[1]


def test_installation_release_manifest_matches_schema_and_is_fail_closed() -> None:
    manifest = json.loads((ROOT / "packaging" / "installation-release-manifest.json").read_text(encoding="utf-8"))
    schema = json.loads((ROOT / "docs" / "schemas" / "dispatch-installation-release-v1.schema.json").read_text(encoding="utf-8"))

    jsonschema.Draft202012Validator(schema).validate(manifest)
    assert manifest["ready"] is False
    assert manifest["core"]["artifact"] == {"url": None, "size": None, "sha256": None}
    assert manifest["browser_runtime"]["ready"] is False
    assert manifest["post_install"] == {
        "setup_implemented": False,
        "setup_command": "dispatch setup",
        "choices": ["start_setup", "skip_for_now"],
    }
    assert manifest["uninstall"] == {
        "user_scope_implemented": True,
        "administrative_command": "dispatch-installer uninstall",
        "future_user_command": "dispatch uninstall",
        "default_mode": "keep-data",
        "purge_requires_confirmation": True,
        "privileged_browser_removal_implemented": False,
    }
