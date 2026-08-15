---
name: dispatch-handbook
description: Use when answering from a configured local handbook index.
---

# Dispatch Handbook

Use the `dispatch_handbook` tool only for explicit local-handbook questions.

- `lookup` accepts one question and returns bounded evidence.
- `contents` lists verified sections.
- `overview` summarizes section coverage without inventing policy rules.
- `health` reports configuration, query, data, and freshness readiness.

Never treat the synthetic demo as real policy. Never collect documents, authenticate, browse, or send messages during a query. If the tool reports `not_configured`, explain that a local index must be explicitly configured rather than attempting collection.
