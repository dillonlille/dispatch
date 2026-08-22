"""Tests for the bounded YAML-subset parser and its use by plugin_policy."""
from __future__ import annotations

import importlib.abc
import subprocess
import sys
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

    def test_root_level_sequence_parses_as_list(self):
        # Root-level sequences parse as lists; the policy layer rejects
        # non-mapping manifests separately.
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


AUDIT_PROBE = r"""
import sys
sys.path.insert(0, {core_root!r})
if {block_yaml!r}:
    # Simulate a clean install: make PyYAML unimportable before plugin_policy loads.
    class _Block:
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "yaml" or fullname.startswith("yaml."):
                raise ModuleNotFoundError("No module named 'yaml'")
            return None
    sys.meta_path.insert(0, _Block())
    sys.modules.pop("yaml", None)
import plugin_policy
failures = {{}}
for owner in {owners!r}:
    audit = plugin_policy.audit_owner(plugin_policy.Path({workspace!r}) / "plugins" / owner)
    failures[owner] = list(audit.failures)
assert all(not f for f in failures.values()), failures
print("clean-install audits OK")
"""


class TestPolicyAuditWithoutPyYAML:
    """The conformance audit must pass on clean installs without PyYAML.

    Audits run in a subprocess: ``plugin_policy._load_target`` purges and
    re-imports plugin modules from ``sys.modules``, so auditing in-process
    would leave stale ``dispatch_paycom``/``dispatch_companion_bridge``
    module copies behind and break later tests that monkeypatch them.
    """

    @pytest.mark.parametrize(
        ("owners", "requires", "block_yaml"),
        [
            (["paycom", "handbook"], [], True),
            # companion-bridge imports PyYAML + pydantic itself; auditing it
            # requires those declared dependencies in the environment.
            (["companion-bridge"], ["pydantic"], False),
        ],
    )
    def test_audit_passes(self, owners, requires, block_yaml):
        for module in requires:
            pytest.importorskip(module)
        probe = AUDIT_PROBE.format(
            core_root=str(CORE_ROOT), workspace=str(WORKSPACE), owners=owners, block_yaml=block_yaml
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        assert "clean-install audits OK" in completed.stdout

    def test_paycom_and_handbook_audits_never_touch_yaml(self):
        """Paycom + handbook audits pass while PyYAML is unimportable."""
        owners = ["paycom", "handbook"]
        probe = AUDIT_PROBE.format(
            core_root=str(CORE_ROOT), workspace=str(WORKSPACE), owners=owners, block_yaml=True
        )
        completed = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]

    def test_load_yaml_rejects_outside_subset_even_with_pyyaml(self, tmp_path):
        policy = __import__("plugin_policy")
        manifest = tmp_path / "dispatch-plugin.yaml"
        manifest.write_text("anchored: &x 1\nreuse: *x\n", encoding="utf-8")
        with pytest.raises(ValueError):
            policy._load_yaml(manifest)
