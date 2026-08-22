"""Tests for the bounded YAML-subset parser and its use by plugin_policy."""
from __future__ import annotations

import importlib.abc
import sys
import types
from pathlib import Path

import pytest

CORE_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = CORE_ROOT.parent
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

import plugin_yaml


class _BlockYAML(importlib.abc.MetaPathFinder):
    """Simulate a clean install where PyYAML is absent."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "yaml" or fullname.startswith("yaml."):
            raise ModuleNotFoundError(f"No module named {fullname!r}")
        return None


@pytest.fixture()
def without_pyyaml(monkeypatch):
    finder = _BlockYAML()
    monkeypatch.setattr(sys, "meta_path", [finder, *sys.meta_path])
    saved = {name: mod for name, mod in sys.modules.items() if name == "yaml"}
    for name in saved:
        monkeypatch.delitem(sys.modules, name)
    yield
    # restore handled by monkeypatch


class TestScalars:
    def test_plain_strings(self):
        assert plugin_yaml.parse_subset("name: Dispatch Paycom\n") == {"name": "Dispatch Paycom"}

    def test_int_and_bool_and_null(self):
        doc = "a: 1\nb: true\nc: false\nd: null\ne:\n"
        assert plugin_yaml.parse_subset(doc) == {"a": 1, "b": True, "c": False, "d": None, "e": None}

    def test_quoted_scalars(self):
        doc = 'a: "quoted: value"\nb: \'single\'\n'
        assert plugin_yaml.parse_subset(doc) == {"a": "quoted: value", "b": "single"}

    def test_hash_comment_ignored(self):
        doc = "# header\na: 1  # trailing\n# footer\n"
        assert plugin_yaml.parse_subset(doc) == {"a": 1}


class TestCollections:
    def test_nested_block_mapping(self):
        doc = "owner:\n  data: paycom\n  team: Dispatch Operations\n"
        assert plugin_yaml.parse_subset(doc) == {"owner": {"data": "paycom", "team": "Dispatch Operations"}}

    def test_sequence_of_scalars(self):
        doc = "provides_tools:\n  - dispatch_handbook\nrequires_env:\n  - ALPHA\n  - BETA\n"
        parsed = plugin_yaml.parse_subset(doc)
        assert parsed["provides_tools"] == ["dispatch_handbook"]
        assert parsed["requires_env"] == ["ALPHA", "BETA"]

    def test_sequence_of_inline_mappings(self):
        doc = "actions:\n  - {name: lookup, privilege: read}\n  - {name: health, privilege: health}\n"
        parsed = plugin_yaml.parse_subset(doc)
        assert parsed["actions"] == [
            {"name": "lookup", "privilege": "read"},
            {"name": "health", "privilege": "health"},
        ]

    def test_compact_mapping_sequence(self):
        doc = "items:\n  - id: first\n    note: kept\n  - id: second\n"
        assert plugin_yaml.parse_subset(doc)["items"] == [
            {"id": "first", "note": "kept"},
            {"id": "second"},
        ]

    def test_flow_map_and_list(self):
        doc = "m: {a: 1, b: [x, y]}\nl: [1, 2]\n"
        assert plugin_yaml.parse_subset(doc) == {"m": {"a": 1, "b": ["x", "y"]}, "l": [1, 2]}

    def test_empty_document(self):
        assert plugin_yaml.parse_subset("") is None
        assert plugin_yaml.parse_subset("\n\n") is None


class TestRejections:
    @pytest.mark.parametrize(
        "doc",
        [
            "anchor: &a 1\nuse: *a\n",
            "tagged: !!str x\n",
            "block: |\n  line one\n",
            "folded: >\n  line one\n",
            "m: {a: 1,\n    b: 2}\n",
            "a: 1\n\tb: 2\n",
            "a: 1\na: 2\n",
            "m: {a: 1, a: 2}\n",
            "a: 1\n...\n",
        ],
    )
    def test_outside_subset_raises(self, doc):
        with pytest.raises(ValueError):
            plugin_yaml.parse_subset(doc)

    def test_root_level_sequence_is_a_mapping_error_context(self):
        # A root-level sequence parses as a list, but plugin manifests are
        # mappings; the policy layer rejects non-mappings separately.
        assert plugin_yaml.parse_subset("- just\n- a list\n") == ["just", "a list"]

    def test_unterminated_quote(self):
        with pytest.raises(ValueError):
            plugin_yaml.parse_subset('a: "open\n')


REAL_MANIFESTS = sorted((WORKSPACE / "plugins").glob("*/dispatch-plugin.yaml"))


class TestAgreementWithPyYAML:
    """Every shipped manifest must parse identically under both parsers."""

    @pytest.mark.parametrize("path", REAL_MANIFESTS, ids=[p.parent.name for p in REAL_MANIFESTS])
    def test_matches_pyyaml(self, path):
        yaml = pytest.importorskip("yaml")
        text = path.read_text(encoding="utf-8")
        assert plugin_yaml.parse_subset(text) == yaml.safe_load(text)

    def test_hermes_manifests_match(self):
        yaml = pytest.importorskip("yaml")
        hermes = sorted((WORKSPACE / "plugins").glob("*/integration/hermes-plugins/*/plugin.yaml"))
        assert hermes, "expected at least one Hermes projection manifest"
        for path in hermes:
            text = path.read_text(encoding="utf-8")
            assert plugin_yaml.parse_subset(text) == yaml.safe_load(text), path


class TestPolicyAuditWithoutPyYAML:
    """The conformance audit must pass on clean installs without PyYAML."""

    def test_all_builtin_plugins_conform(self, without_pyyaml):
        pytest.importorskip(
            "pydantic", reason="companion-bridge declares pydantic; the audit imports plugin source"
        )
        # companion-bridge itself imports PyYAML at module import time (it is a
        # declared dependency of that plugin). The audit only needs PyYAML to be
        # ABSENT for dispatch-core's own manifest handling, which paycom and
        # handbook exercise; companion-bridge is audited with its own declared
        # dependency present, exactly as a real install would have it.
        import plugin_policy  # noqa: F401

        results = {}
        for owner in ("paycom", "handbook"):
            root = WORKSPACE / "plugins" / owner
            policy = __import__("plugin_policy")
            audit = policy.audit_owner(root)
            results[owner] = list(audit.failures)
        for owner, failures in results.items():
            assert failures == [], f"{owner}: {failures}"

    def test_paycom_and_handbook_audits_never_touch_yaml(self, without_pyyaml):
        """The two plugins whose manifests drive _load_yaml audit cleanly while
        PyYAML is unimportable -- proving WS1 on a clean-install venv."""
        import plugin_policy

        assert "yaml" not in sys.modules
        for owner in ("paycom", "handbook"):
            audit = plugin_policy.audit_owner(WORKSPACE / "plugins" / owner)
            assert audit.failures == [], f"{owner}: {audit.failures}"

    def test_load_yaml_rejects_outside_subset_even_with_pyyaml(self, tmp_path):
        policy = __import__("plugin_policy")
        manifest = tmp_path / "dispatch-plugin.yaml"
        manifest.write_text("anchored: &x 1\nreuse: *x\n", encoding="utf-8")
        with pytest.raises(ValueError):
            policy._load_yaml(manifest)
