# Dispatch Core

Dispatch Core is the root-level, feature-oriented control plane for Dispatch. It is not a plugin and does not live under `plugins/`.

Each Core feature owns a directory, documentation, implementation, and focused tests. The current source contains implemented `paths`, `health`, `command-interface`, `plugin-policy`, `lifecycle`, and `browser-manager` features. Authentication has encrypted credential storage and a bounded Amazon/Paycom login workflow; authorized live-account acceptance remains pending. Collection Manager has bounded registration, a transactional SQLite task queue, retries, cancellation, reconciliation, schedules, receipts, and spawned worker-process supervision with hard deadlines, heartbeats, process-tree cleanup, and startup orphan recovery. Persistent OS service installation remains deferred to the installer.

## Layout

```text
dispatch-core/
├── paths/
├── health/
├── command-interface/
├── plugin-policy/
├── lifecycle/
├── collection-manager/
├── authentication/
└── browser-manager/
```

Domain integrations remain under [`../plugins/`](../plugins/). Core features may provide bounded infrastructure to reviewed plugins. Authentication owns only fixed provider login fields; domain collection selectors and business rules remain outside Core.

## Commands

```bash
./scripts/test
./scripts/build
./scripts/verify
./scripts/health
```

`build` writes deterministic immutable output beneath validated `DISPATCH_BUILD_OUTPUT` when set, otherwise beneath the resolved per-user cache root. It never writes releases into the source checkout.

The Core runtime wheel exposes:

```bash
dispatch-core verify
dispatch-core health
dispatch-core browser-doctor
dispatch-core paths --owner example-plugin
dispatch-core auth status
dispatch-core auth enroll amazon-operations
dispatch-core auth remove amazon-operations --yes
dispatch-core collection status
dispatch-core collection worker-once
dispatch-core collection reconcile
dispatch-core collection cancel TASK_ID
dispatch-core collection resume TASK_ID
```

Installed execution requires an explicit `DISPATCH_CODE_ROOT` naming the installed code or release authority. Every command preserves the exact seven-field response envelope. Read-only commands create no private roots; explicit authentication enrollment and removal may create or update the private encrypted credential store.

Browser Manager creates private state only when explicitly instantiated by a Core service or test. It uses persistent isolated profiles with temporary Chromium processes and typed internal leases rather than generic browser commands. Collection Manager likewise creates its private database only when explicitly opened; health and `collection status` inspection do not create an empty store. A zero-collector worker exits successfully without claiming queued work it cannot execute.

See [`../docs/path-configuration.md`](../docs/path-configuration.md).
