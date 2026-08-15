#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILDER = ROOT / "scripts" / "build_release.py"


def load_builder():
    spec = importlib.util.spec_from_file_location("public_handbook_builder", BUILDER)
    if spec is None or spec.loader is None:
        raise RuntimeError("builder_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    try:
        builder = load_builder()
        with tempfile.TemporaryDirectory(prefix="handbook-a-") as left, tempfile.TemporaryDirectory(prefix="handbook-b-") as right:
            first = builder.build(Path(left))
            second = builder.build(Path(right))
            if first["release_id"] != second["release_id"]:
                raise RuntimeError("nondeterministic_release")
        result = builder.build(builder.default_output())
    except (OSError, RuntimeError, ValueError) as exc:
        payload = {
            "ok": False,
            "action": "build",
            "status": "error",
            "data": {},
            "freshness": None,
            "delivery": None,
            "error": {"code": "build_failed", "message": str(exc)[:512]},
        }
        print(json.dumps(payload, sort_keys=True))
        return 1
    print(json.dumps({
        "ok": True,
        "action": "build",
        "status": "ready",
        "data": {"deterministic": True, **result},
        "freshness": None,
        "delivery": None,
        "error": None,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
