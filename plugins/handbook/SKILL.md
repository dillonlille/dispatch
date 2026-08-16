---
name: dispatch-handbook
description: Use when answering from a configured local handbook index.
---

# Dispatch Handbook

Use the `dispatch_handbook` tool only for explicit local-handbook questions. The tool is backed by the cloned built-in source in `plugins/handbook`, installed editable in the shared Dispatch virtual environment.

- `lookup` accepts one question and returns bounded evidence.
- `contents` lists verified sections.
- `overview` summarizes section coverage without inventing policy rules.
- `health` reports configuration, query, data, and freshness readiness.

Never treat the synthetic demo as real policy. Never collect documents, authenticate, browse, or send messages during a query. If the tool reports `not_configured`, explain that an operator must explicitly configure a local index below `DISPATCH_HANDBOOK_DATA_ROOT` rather than attempting collection.

Owner data belongs under the operator's private Handbook data root, not in the cloned source tree. Source edits are checked with `./scripts/test`, `./scripts/build`, `./scripts/verify`, and `./scripts/health`; no generated plugin artifact is authoritative.
