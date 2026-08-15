#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
SOURCE = ROOT / "src"


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"{name}_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        conformance = load(
            WORKSPACE / "dispatch-core" / "plugin-policy" / "plugin_conformance.py",
            "public_plugin_conformance",
        )
        audit = conformance.audit_owner(ROOT)
        if audit.failures:
            raise RuntimeError("; ".join(audit.failures))
        builder = load(ROOT / "scripts" / "build_release.py", "public_handbook_builder")
        builder.entries()
        release = None
        release_value = argv[0] if argv else os.environ.get("DISPATCH_VERIFY_RELEASE")
        if release_value:
            release = builder.verify(Path(release_value).resolve(), builder.entries())
        data = {"owner": "handbook", "conformance_checks": len(audit.passes)}
        if release is not None:
            data["release_id"] = release["release_id"]
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({
            "ok": False,
            "action": "verify",
            "status": "error",
            "data": {},
            "freshness": None,
            "delivery": None,
            "error": {"code": "verification_failed", "message": str(exc)[:512]},
        }, sort_keys=True))
        return 1
    print(json.dumps({
        "ok": True,
        "action": "verify",
        "status": "ready",
        "data": data,
        "freshness": None,
        "delivery": None,
        "error": None,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
