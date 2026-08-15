from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "installer" / "deploy" / "cloudflare" / "install.sh.in"
PUBLIC = ROOT / "installer" / "deploy" / "cloudflare" / "public" / "install.sh"


def test_bootstrap_template_is_fail_closed_and_shell_valid(tmp_path: Path) -> None:
    content = TEMPLATE.read_text(encoding="utf-8")
    rendered = (
        content.replace("@PRODUCT_VERSION@", "0.0.1")
        .replace("@MANIFEST_URL@", "https://dispatch.dillonlille.com/releases/0.0.1/installation-release-manifest.json")
        .replace("@MANIFEST_SHA256@", "0" * 64)
        .replace("@INSTALLER_URL@", "https://dispatch.dillonlille.com/releases/0.0.1/dispatch_installer-0.1.0-py3-none-any.whl")
        .replace("@INSTALLER_SIZE@", "1")
        .replace("@INSTALLER_SHA256@", "1" * 64)
    )
    path = tmp_path / "install.sh"
    path.write_text(rendered, encoding="utf-8")

    completed = subprocess.run(["sh", "-n", str(path)], check=False, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    assert "--max-redirs 0" in rendered
    assert "1. Start Setup" in rendered
    assert "2. Skip for Now" in rendered
    assert '"$installer_environment/bin/python" -m dispatch_installer install' in rendered
    assert "/bin/dispatch-installer" not in rendered
    assert "| sh" not in rendered and "| bash" not in rendered


def test_public_bootstrap_remains_blocked_until_release_activation() -> None:
    content = PUBLIC.read_text(encoding="utf-8")
    assert "installation is not available yet" in content
    assert "exit 1" in content


def test_renderer_refuses_draft_release(tmp_path: Path) -> None:
    output = tmp_path / "install.sh"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "render-bootstrap"),
            "--manifest",
            str(ROOT / "packaging" / "installation-release-manifest.json"),
            "--installer-wheel",
            str(tmp_path / "missing.whl"),
            "--template",
            str(TEMPLATE),
            "--output",
            str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert not output.exists()
