from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

VERIFIER = Path(__file__).resolve().parents[1] / "scripts" / "verify-source-export"


def invoke_verifier(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(VERIFIER), str(root), "--json"],
        check=False,
        capture_output=True,
        text=True,
    )


def run_verifier(root: Path) -> subprocess.CompletedProcess[str]:
    return invoke_verifier(root)


class VerifySourceExportTests(unittest.TestCase):
    def make_root(self) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "README.md").write_text("# Synthetic public source\n", encoding="utf-8")
        return temporary, root

    def test_clean_tree_passes(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        (root / "src").mkdir()
        (root / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
        result = run_verifier(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unlisted_source_file_is_checked_without_scope_manifest(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        (root / "unreviewed.py").write_text("VALUE = 1\n", encoding="utf-8")
        result = run_verifier(root)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["synthetic_declarations_checked"], 0)

    def test_database_and_environment_files_fail(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        (root / "records.sqlite").write_bytes(b"SQLite format 3")
        (root / ".env").write_text("TOKEN=not-public\n", encoding="utf-8")
        result = run_verifier(root)
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(any("forbidden file category" in item for item in payload["errors"]))
        self.assertTrue(any("environment file" in item for item in payload["errors"]))

    def test_personal_path_and_secret_shape_fail(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        token = "xox" + "b-" + "A" * 24
        personal_path = "/" + "home" + "/" + "realoperator" + "/private"
        (root / "config.example.yaml").write_text(
            f"path: {personal_path}\n" f"bot_token: {token}\n",
            encoding="utf-8",
        )
        result = run_verifier(root)
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(any("personal home" in item for item in payload["errors"]))
        self.assertTrue(any("Slack token" in item for item in payload["errors"]))

    def test_broken_absolute_and_escaping_symlinks_fail(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        (root / "absolute").symlink_to("/tmp")
        (root / "broken").symlink_to("missing")
        (root / "escape").symlink_to("../outside")
        result = run_verifier(root)
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(any("absolute symlink" in item for item in payload["errors"]))
        self.assertTrue(any("broken symlink" in item for item in payload["errors"]))
        self.assertTrue(any("escaping symlink" in item for item in payload["errors"]))

    def test_fixture_requires_digest_bound_synthetic_declaration(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        fixture = root / "examples" / "people.csv"
        fixture.parent.mkdir(parents=True)
        fixture.write_text("name,email\nAvery Example,avery@example.invalid\n", encoding="utf-8")
        missing = run_verifier(root)
        self.assertNotEqual(missing.returncode, 0)

        digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
        (root / "synthetic-data.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "files": {
                        "examples/people.csv": {
                            "synthetic": True,
                            "provenance": "created-for-public-tests",
                            "sha256": digest,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        accepted = run_verifier(root)
        self.assertEqual(accepted.returncode, 0, accepted.stdout + accepted.stderr)

    def test_non_placeholder_secret_in_example_fails(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        (root / ".env.example").write_text(
            "API_KEY=ordinary-looking-but-not-placeholder\n", encoding="utf-8"
        )
        result = run_verifier(root)
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(any("non-placeholder" in item for item in payload["errors"]))

    def test_non_placeholder_identity_and_private_hostname_fail(self) -> None:
        temporary, root = self.make_root()
        self.addCleanup(temporary.cleanup)
        workspace_key = "workspace" + "_id"
        hostname_key = "host" + "name"
        private_hostname = "dispatch.private" + ".internal"
        (root / "config.example.yaml").write_text(
            f"{workspace_key}: private-workspace-42\n{hostname_key}: {private_hostname}\n",
            encoding="utf-8",
        )
        result = run_verifier(root)
        self.assertNotEqual(result.returncode, 0)
        payload = json.loads(result.stdout)
        self.assertTrue(any("identity-like setting" in item for item in payload["errors"]))
        self.assertTrue(any("private hostname" in item for item in payload["errors"]))


if __name__ == "__main__":
    unittest.main()
