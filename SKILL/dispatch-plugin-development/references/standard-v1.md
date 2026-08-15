# Dispatch Plugin Standard v1

Status: **authoring standard for new and migrated components**.

This document defines how Dispatch components are classified, authored, tested, released, activated, and operated. Existing plugins may be migrated incrementally; declaring `schema_version: 1` means the owner accepts this contract and must pass the conformance audit before being called conforming.

## 1. Design principles

1. **One canonical source.** Editable implementation, tests, build logic, and documentation live outside generated runtime releases.
2. **Generated releases are immutable.** Never patch `runtime/current` or a content-addressed release in place.
3. **One activation authority.** The activation record binds active and rollback releases plus every installed launcher/projection. Secondary manifests must be generated from it or mechanically checked against it.
4. **Separate capabilities.** Query, collection, authentication, service, delivery, and control-plane privileges are declared independently.
5. **Owner-scoped data.** Databases use `db/<owner>/`; generated and downloaded artifacts use `artifacts/<owner>/`; operational state stays under `plugins/<owner>/`.
6. **Fail closed and report honestly.** Missing data, unavailable producers, stale data, failed domain audits, and unavailable authentication are distinct states.
7. **Standardize contracts, not business logic.** Shared infrastructure may verify releases, execute launchers, and deliver messages; domain validation remains with the owning plugin.
8. **Source-control-independent release identity.** Git tracks the source monorepo, but installed release identity is based on deterministic content and manifests rather than a checkout or commit alone.

## 2. Component classes

A plugin owner can contain one or more components. Every component declares one class:

- `hermes-tool`: model-facing bounded tool.
- `collector`: authenticated or networked producer that stages, validates, and atomically publishes data.
- `service`: long-running process or bridge.
- `auth-provider`: privileged authentication boundary.
- `library`: importer, formatter, or shared domain implementation with no independent agent exposure.
- `control-plane`: scheduling, queueing, reconciliation, or shared policy authority.
- `retired`: retained compatibility component that must not be activated as a new dependency.

A service or script directory is not a Hermes tool unless it has a Hermes manifest, adapter, registration, and declared tool exposure.

## 3. Canonical owner layout

Only create directories that the owner uses.

```text
plugins/<owner>/
├── README.md                       # required developer/operator contract
├── dispatch-plugin.yaml            # required authoritative owner manifest
├── SKILL.md                        # required when a model-facing tool exists
├── src/                            # canonical editable implementation
├── tests/                          # canonical runnable tests
├── references/                     # model/operator domain contracts
├── config/                         # non-secret config schemas/default examples
├── integration/
│   ├── hermes-plugins/<package>/
│   │   ├── __init__.py
│   │   ├── plugin.yaml
│   │   └── launcher-manifest.json
│   ├── launchers/
│   └── systemd/
├── scripts/
│   ├── test
│   ├── build
│   ├── verify
│   └── health
├── runtime/
│   ├── releases/<content-digest>/  # generated and immutable
│   └── current                     # optional derived active pointer
├── state/                          # mutable operational state
├── staging/                        # unpublished candidates and journals
├── locks/
└── receipts/
```

Rules:

- `src/` or an explicitly declared equivalent is authoritative; `runtime/` is never the only recoverable source.
- Source, tests, configuration, editable virtual environments, logs, and browser profiles must not be mixed into a production runtime root.
- Shell wrappers use an extensionless name or `.sh`; a Bash program must not be named `.js`.
- Legacy wrappers live under an explicitly documented `scripts/legacy/` and delegate to canonical code.
- Empty placeholder directories are discouraged.

## 4. Authoritative manifest

Every conforming owner has `dispatch-plugin.yaml`, validated by:

```text
docs/schemas/dispatch-plugin-v1.schema.json
```

The manifest declares:

- owner identity and version;
- source, test, data, artifact, and operational-state roots; owners with multiple independently released components may use named `databases` and `artifact_stores` maps instead of singular roots;
- standard lifecycle commands;
- every component class;
- component capabilities;
- Hermes exposure and actions, when applicable;
- runtime release and activation records.

Hermes `plugin.yaml`, launcher manifests, release manifests, installed profile projections, and service units are secondary artifacts. They must agree with the root manifest and activation record.

Runtime paths in the source manifest are logical projection declarations. Generated releases, selectors, launcher manifests, and activation records do not need to exist in a clean source checkout; conformance validates their contents when installed artifacts are present.

## 5. Capability planes

### Query plane

Default model-facing query components:

- read verified local data;
- do not authenticate, browse, collect, mutate, or silently refresh;
- do not own platform credentials;
- return bounded structured output.

A read request never implies collection.

### Collector plane

Collectors:

- declare browser, network, and authentication prerequisites;
- write to staging before publication;
- validate period, schema, provenance, and domain invariants;
- publish atomically;
- return a bounded structured receipt;
- integrate with Collector Coordinator when scheduled or queued.

The domain owner owns collection and validation logic. Collector Coordinator owns scheduling, retries, queues, and reconciliation.

### Delivery plane

Slack/Discord credentials and network posting belong in a shared delivery capability, not repeated domain adapters. Domain tools return structured content or use a reviewed delivery interface and return only a bounded receipt.

### Service and auth planes

Long-running services and auth providers declare external ports, browser profiles, service units, secrets, and privilege boundaries explicitly. Their source/release/state lifecycle follows the same rules even when they expose no Hermes tool.

## 6. Hermes contract

A model-facing component normally exposes one namespaced tool and generic actions:

- `health`
- `summary`
- `report`
- `driver`
- `collect` only when a documented interactive product requirement and separate security review explicitly approve model initiation.

Scheduled, queued, background, browser-authenticated, or long-running collection defaults to Collector Coordinator only. The presence of an owner CLI `collect` command does not authorize a Hermes `collect` action.

Requirements:

- `plugin.yaml.provides_tools` matches the tool registered by `register(ctx)`.
- Tool name, toolset, schema, skill, and profile exposure agree.
- Schema requires `action`, uses a closed enum, rejects additional properties, and bounds all strings, rows, bytes, ranges, and timeouts.
- Exact fields are enforced per action.
- Each action declares a privilege: `read`, `health`, `mutation`, `administration`, or `direct-delivery`.
- Forbidden input fails before a subprocess, browser, network request, or database mutation is started.
- Registration availability checks return a boolean. `false` is conforming for an intentionally unconfigured or disabled optional component and must prevent broken model-tool exposure; its health action still returns a valid degraded readiness envelope.

## 7. Standard response and readiness envelopes

Model-facing tools return stable top-level fields:

```json
{
  "ok": true,
  "action": "summary",
  "status": "ready",
  "data": {},
  "freshness": null,
  "delivery": null,
  "error": null
}
```

Failures use a bounded error object with a stable code. Domain-specific fields belong under `data`.

Health must distinguish readiness planes:

```json
{
  "registration": "ready",
  "runtime_integrity": "ready",
  "query": "ready",
  "data": "ready",
  "freshness": "stale",
  "collector": "degraded",
  "authentication": "not_checked",
  "delivery": "not_checked",
  "overall": "degraded"
}
```

Do not report generic `ok` when a required database is absent. SQLite integrity and plugin-specific domain audit results are separate evidence.

## 8. Build, release, activation, and rollback

The lifecycle is:

```text
canonical source -> test -> deterministic build -> immutable release
                 -> verify -> activate -> install/project -> smoke test
```

Preferred release layout:

```text
runtime/releases/<content-digest>/
runtime/current -> releases/<content-digest>
```

The activation record is authoritative and declares active and rollback identities plus launchers and installed projections. The audit must detect disagreement among:

- activation record;
- `runtime/current`;
- manager executable and adjacent record;
- Hermes launcher manifest;
- installed profile projection;
- effective service `ExecStart`;
- release/install receipts.

When one owner contains independently released components, one activation record may contain an `interfaces` map. Each interface binds its own active runtime, exact rollback path and digest, launcher manifest, and any manager or service projection. Historical rollback directories created before digest-prefix naming are valid only when their exact path, directory identity, and full digest are recorded and verified; never infer or rename them during activation.

Activation utilities may temporarily open an owner-controlled read-only projection directory only for atomic replacement of declared leaf files. They must reseal the directory in a `finally` block, publish the root activation authority last, and ensure any partial replacement fails closed through hash-bound secondary manifests.

Release identity excludes volatile files such as bytecode, `__pycache__`, logs, caches, locks, SQLite WAL/SHM files, browser state, and temporary files.

Default retention is current, one rollback, and any externally pinned release. Additional history requires an explicit retention policy.

## 9. Data, state, and permissions

Canonical roots:

```text
db/<component>/
artifacts/<component>/
plugins/<owner>/state/
plugins/<owner>/staging/
plugins/<owner>/locks/
plugins/<owner>/receipts/
```

For a single-component owner, `<component>` is normally the owner slug. An umbrella owner with independently contracted data components may declare named stores. Each manifest key is the component identity and owns its corresponding root, for example `paycom-roster: db/paycom-roster`. Do not force an already coherent component store beneath the umbrella owner slug solely for cosmetic uniformity.

Defaults:

- private data directories: `0700`;
- private data/config/receipt files: `0600`;
- executables: owner-writable, normally `0755` or `0555` when sealed;
- no group-writable executable code;
- no `0777` data or state directories;
- no symlink traversal outside declared roots;
- host-specific absolute paths only in generated deployment projections.

Secrets, cookies, tokens, MFA data, connection strings, and browser profiles never appear in manifests, skills, documentation, tests, or receipts.

## 10. Required commands and tests

Every conforming owner provides executable entrypoints:

```text
./scripts/test
./scripts/build
./scripts/verify
./scripts/health
```

Commands must work from the owner root. `test` must discover a nonzero count and require no undocumented import path. A release may use a bounded self-test instead of shipping the complete source suite, but metadata must not advertise tests that are absent.

Minimum focused coverage:

1. root manifest validation;
2. Hermes declaration/registration parity;
3. availability against the built release;
4. every model-facing action;
5. malformed and forbidden input;
6. response-envelope validation;
7. missing/not-loaded data boundary;
8. release and launcher tamper rejection;
9. query never triggers collection;
10. collector receipt validation when applicable;
11. activation convergence;
12. installed projection equality;
13. non-mutating health.

## 11. Documentation contracts

`README.md` is for developers/operators and must identify source, components, privileges, data ownership, commands, external prerequisites, release process, rollback, and generated files that must not be edited.

`SKILL.md` is for model routing and must identify approved actions, authoritative evidence, read/mutation boundaries, presentation rules, and abstention/failure behavior. A skill is not developer documentation.

Do not manually freeze active release IDs in prose. Generate them or refer to the activation record.

## 12. Conformance and adoption

Run:

```bash
python3 dispatch-core/plugin-policy/plugin_conformance.py plugins/<owner>
```

A plugin is **conforming** only when the audit exits successfully. Existing owners without a root manifest are **legacy/unadopted**, not implicitly conforming because they resemble the layout.

Migration order:

1. restore broken registration, activation, integrity, and canonical tests;
2. establish canonical source and manifest authority;
3. separate query, collector, delivery, service, and auth capabilities;
4. replace duplicated infrastructure with small shared helpers;
5. prune obsolete wrappers and releases only after references and rollback are verified.
