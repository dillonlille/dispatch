from __future__ import annotations

import os
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

# A few installer tests exercise Core integration points (provider_catalog)
# directly. Resolve them from the sibling dispatch-core checkout so the suite
# runs standalone (`cd installer && pytest`) without editable installs; when CI
# provides the modules through editable installs the guard is a no-op.
CORE_ROOT = Path(__file__).resolve().parents[2] / "dispatch-core"
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))

# Match the CI runner environment: developer umasks like 002 leave pytest's
# tmp_path directories group-writable, which the installer correctly rejects
# as unsafe. Pinning here keeps local runs deterministic instead of
# fail-closed-noisy.
os.umask(0o022)
