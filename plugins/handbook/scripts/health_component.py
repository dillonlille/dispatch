#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"
if str(SOURCE) not in sys.path:
    sys.path.insert(0, str(SOURCE))

from dispatch_handbook.service import health


def main() -> int:
    print(json.dumps(health(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
