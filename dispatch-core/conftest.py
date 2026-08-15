from __future__ import annotations

import sys
from pathlib import Path

CORE = Path(__file__).resolve().parent
SOURCE_ROOTS = (
    CORE / "src",
    CORE / "paths" / "src",
    CORE / "health" / "src",
    CORE / "command-interface" / "src",
    CORE / "collection-manager" / "src",
    CORE / "authentication" / "src",
    CORE / "browser-manager" / "src",
)
for source in reversed(SOURCE_ROOTS):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
