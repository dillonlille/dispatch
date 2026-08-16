#!/usr/bin/env python3
"""Check that the Handbook source is directly buildable without artifacts."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src"


def envelope(ok: bool, status: str, data: dict, error: dict | None = None) -> dict:
    return {
        "ok": ok,
        "action": "build",
        "status": status,
        "data": data,
        "freshness": None,
        "delivery": None,
        "error": error,
    }


def main() -> int:
    try:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        plugin_id = project["tool"]["dispatch"]["id"]
        files = sorted(SOURCE.rglob("*.py"))
        if not files:
            raise RuntimeError("source_tree_empty")
        for path in files:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except (KeyError, OSError, SyntaxError, tomllib.TOMLDecodeError, TypeError, UnicodeError, ValueError, RuntimeError) as exc:
        print(json.dumps(envelope(False, "error", {}, {"code": "source_check_failed", "message": str(exc)[:512]}), sort_keys=True))
        return 1
    print(json.dumps(envelope(True, "ready", {"id": plugin_id, "source_files": len(files), "editable": True}), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
