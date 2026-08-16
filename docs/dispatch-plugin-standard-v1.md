# Dispatch Plugin Standard v1

Status: **source standard for built-in and cloned plugins**.

This document defines how Dispatch plugins are authored, installed, tested, and operated from a maintained source clone. It intentionally does not define wheel publication, immutable runtime copies, generated generations, activation pointers, or release receipts.

## 1. Design principles

1. **One editable source.** A built-in plugin lives in its cloned source tree under `plugins/<owner>`; that tree is authoritative for code, tests, metadata, and documentation.
2. **Shared-environment discovery.** The plugin is installed editable into the same shared virtual environment as Core. Core discovers `dispatch.plugins` entry points from that environment and filters only by `DISPATCH_ACTIVE_PLUGINS`.
3. **Own dependencies.** Plugin dependencies belong in the plugin's `pyproject.toml`. Discovery does not require a `dispatch-core` distribution dependency.
4. **Explicit capabilities.** Query, collection, authentication, service, delivery, and control-plane privileges are declared independently in `[tool.dispatch]` and kept separate in code.
5. **Owner-scoped data.** Source and tests are separate from owner-managed data. Use `plugins/<owner>/data` by default or document an operator-owned private root for databases and artifacts.
6. **Fail closed and report honestly.** Missing data, stale data, unavailable producers, invalid configuration, and unavailable authentication are distinct states.
7. **Standardize contracts, not business logic.** Core owns only generic entry-point discovery and bounded invocation. Domain behavior remains in the plugin.

## 2. Component classes

A plugin may contain one or more components. Each component has one effective class:

- `hermes-tool`: model-facing bounded tool;
- `collector`: authenticated or networked producer;
- `service`: long-running process or bridge;
- `auth-provider`: privileged authentication boundary;
- `library`: imported domain implementation with no independent agent exposure;
- `control-plane`: scheduling, queueing, reconciliation, or shared policy authority;
- `retired`: compatibility code that must not become a new dependency.

A service or script is not a Hermes tool unless it has an adapter, schema, registration, and declared tool exposure.

## 3. Canonical source layout

Only create directories the plugin uses:

```text
plugins/<owner>/
├── README.md
├── pyproject.toml
├── dispatch-plugin.yaml            # optional source manifest
├── SKILL.md                        # when model-facing
├── src/                            # canonical editable implementation
├── tests/                          # canonical runnable tests
├── references/                     # optional domain contracts
├── integration/hermes-plugins/     # optional Hermes projection
│   └── <package>/{__init__.py,plugin.yaml}
└── scripts/{test,build,verify,health}
```

`src/` and the package metadata are authoritative. Do not add `runtime/`, `current` pointers, generation directories, activation records, or receipt stores as part of this source contract. Keep source, tests, local configuration, logs, and browser profiles separate from owner data.

## 4. Plugin metadata and discovery

### 4.1 pyproject metadata

Every Python plugin declares:

```toml
[project.entry-points."dispatch.plugins"]
example = "dispatch_example.service:handle"

[tool.dispatch]
id = "example"
capabilities = ["read_local_data"]
```

`tool.dispatch.id` is a lowercase plugin ID. `capabilities` is a non-empty list of effective labels from:

- `read_local_data`;
- `mutate_data`;
- `collect`;
- `network`;
- `authentication`;
- `direct_delivery`;
- `long_running`.

The `dispatch.plugins` group contains exactly one entry named by the ID. The target is a callable accepting one bounded JSON object and returning the exact response envelope. The plugin retains control of its normal `[project.dependencies]`.

A root `dispatch-plugin.yaml` is optional. If retained, its `id` must exactly match `[tool.dispatch].id`; it is descriptive source metadata, not an activation authority.

### 4.2 Editable setup

From the Dispatch environment:

```bash
python -m pip install -e plugins/<owner>
export DISPATCH_ACTIVE_PLUGINS=<owner>
```

Core loads only the installed `dispatch.plugins` entry points whose names are selected. It must require exactly one matching entry point for every selected ID. It must not scan repositories, use package-specific registry code, or consult `DISPATCH_PLUGIN_PATHS`; that variable is obsolete.

## 5. Capability boundaries

### Query plane

A default model-facing query:

- reads verified local data;
- does not authenticate, browse, collect, mutate, or silently refresh;
- does not own platform credentials;
- returns bounded structured output.

A read request never implies collection.

### Collector, delivery, service, and auth planes

Collectors declare network/browser/authentication needs and own validation. Scheduling and retries belong to an approved coordinator. Slack/Discord credentials belong to a reviewed delivery boundary. Long-running services and auth providers document privilege and private data boundaries explicitly.

## 6. Hermes contract

A Hermes projection normally registers one namespaced tool with generic actions such as `health`, `summary`, `report`, or `driver`. A `collect` action needs a separate product and security approval; an owner CLI action does not authorize model initiation.

The schema:

- requires `action`;
- uses a closed action enum;
- rejects additional properties;
- declares exact fields per action;
- bounds all strings, rows, byte counts, ranges, and timeouts;
- rejects forbidden input before side effects;
- registers exactly one tool with matching name, toolset, and optional `plugin.yaml.provides_tools`.

Availability checks return a boolean. An intentionally unconfigured optional plugin may be unavailable while its health action returns a valid degraded envelope.

## 7. Response and readiness envelopes

Every Core entry point and Hermes handler returns exactly these top-level fields:

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

`data` is an object. `freshness` and `delivery` are null or objects. Successful responses have `error: null`; failures have exactly `{ "code": "...", "message": "..." }` with bounded strings.

Health distinguishes:

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

Do not claim generic readiness when required data is absent.

## 8. Data, state, and permissions

Default owner paths are:

```text
plugins/<owner>/data/
plugins/<owner>/state/
```

A plugin may document `db/<component>` or an operator-owned private root when its data contract needs a separate database or artifact location. In every case:

- source-controlled code and private data are separate;
- private directories are normally `0700` and private files `0600`;
- executable scripts are owner-executable and not group/world writable;
- no secrets, cookies, tokens, connection strings, browser profiles, or private records appear in source, metadata, tests, skills, or documentation;
- traversal outside declared roots is rejected.

## 9. Required commands and source tests

Every conforming owner provides executable commands that work from the owner root:

```text
./scripts/test
./scripts/build
./scripts/verify
./scripts/health
```

`test` discovers a nonzero count. `build` is a direct source syntax/import check. `verify` runs source-owned conformance. `health` is read-only. None publishes or validates a generated plugin artifact.

Minimum focused coverage:

1. pyproject identity, capability, dependency, and entry-point metadata;
2. optional manifest ID consistency;
3. lifecycle script permissions;
4. direct entry-point health and invalid-input envelopes;
5. Hermes registration, tool name/toolset, schema closure, actions, and availability;
6. query/collection separation and missing-data boundaries;
7. non-mutating health.

## 10. Conformance and adoption

Run:

```bash
python3 dispatch-core/plugin_policy.py plugins/<owner>
```

The audit checks the maintained source directly. It does not require a generated directory, an activation selector, a package-specific Core edit, or a published artifact. A plugin is conforming only when the audit and the real source commands pass.

Migration order:

1. establish the canonical clone and owner data boundary;
2. add `[tool.dispatch]` metadata and one matching entry point;
3. separate query, collector, delivery, service, and auth capabilities;
4. make envelopes and tool schemas exact and bounded;
5. run source tests, build, verify, health, and editable shared-environment smoke tests;
6. remove obsolete wrappers and artifact-generation machinery from the clone.
