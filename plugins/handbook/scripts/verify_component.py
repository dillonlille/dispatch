#!/usr/bin/env python3
"""Run the source-owned Dispatch conformance audit for the Handbook."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parents[1]
POLICY = WORKSPACE / "dispatch-core" / "plugin_policy.py"


def load_policy():
    spec = importlib.util.spec_from_file_location("dispatch_source_plugin_policy", POLICY)
    if spec is None or spec.loader is None:
        raise RuntimeError("plugin_policy_import_failed")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def envelope(ok: bool, status: str, data: dict, error: dict | None = None) -> dict:
    return {
        "ok": ok,
        "action": "verify",
        "status": status,
        "data": data,
        "freshness": None,
        "delivery": None,
        "error": error,
    }


def main() -> int:
    try:
        policy = load_policy()
        audit = policy.audit_owner(ROOT)
        if audit.failures:
            raise RuntimeError("; ".join(audit.failures))
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(json.dumps(envelope(False, "error", {}, {"code": "source_conformance_failed", "message": str(exc)[:512]}), sort_keys=True))
        return 1
    print(json.dumps(envelope(True, "ready", {"owner": "handbook", "checks": len(audit.passes), "source_only": True}), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
