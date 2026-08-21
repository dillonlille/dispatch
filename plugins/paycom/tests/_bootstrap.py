"""Shared sys.path bootstrap so every test file works under bare pytest,
./scripts/test, and editable installs alike."""
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src"
CORE = ROOT.parent.parent / "dispatch-core"
for entry in (str(CORE), str(SOURCE)):
    if entry not in sys.path:
        sys.path.insert(0, entry)
