from __future__ import annotations

import sys
from pathlib import Path

CORE = Path(__file__).resolve().parent
SOURCE_ROOT = CORE / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))
