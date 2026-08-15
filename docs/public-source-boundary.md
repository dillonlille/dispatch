# Public source, data, and privacy boundary

The private monorepo candidate is constructed from an exact reviewed scope. Unknown paths, stale declarations, and digest changes fail verification rather than becoming source implicitly. This source scope includes deferred plugins and therefore must never be reused as the default Core installation payload.

## Ownership planes

| Plane | Purpose | Allowed in this source candidate |
|---|---|---|
| Canonical public source | Editable code, schemas, manifests, tests, documentation, and safe templates | Yes, when explicitly scope-bound |
| Declared synthetic material | Wholly invented fixtures with digest-bound provenance | Yes, for tests and explicit demos only |
| Generated releases | Immutable content-addressed build output and release manifests | No; rebuild outside the checkout |
| Local deployment projections | Installed launchers, profile adapters, activation records, and service units | No; generate during installation |
| Private configuration and credentials | Secrets, account settings, document paths, destination allowlists, and authentication references | No; create under private per-user roots |
| Business data | Owner databases, imported documents, reports, and generated artifacts | No; keep under owner-scoped private data roots |
| Mutable runtime state | Staging, locks, logs, receipts, queues, selectors, browser profiles, and sessions | No; keep under owner-scoped private state roots |
| Disposable cache | Dependency downloads, model caches, wheels, bytecode, and test caches | No; regenerate outside the checkout |

## Included

- canonical source and owner contracts for root-level Dispatch Core and deferred plugin proofs;
- public schemas and plugin-development guidance;
- sanitized public-user documentation;
- placeholder-only configuration examples required by current components;
- exact package metadata and an online runtime dependency plan;
- focused source, lifecycle, and browser-management tests;
- the declared Aster Lantern synthetic fixture.

The synthetic fixture was authored for public tests, is digest-bound in `synthetic-data.json`, and is not a runtime-release member or automatic model-facing data source.

Generated runtime wheels, hash locks, bundle manifests, virtual environments, and installed console scripts remain outside the candidate. They are reproduced from explicit component roots. The Core wheel is separately checked against `packaging/runtime-package-plan.json`; repository archives and source-scope manifests never authorize installation.

## Excluded

- credentials, cookies, sessions, browser profiles, private keys, and usable tokens;
- active `.env` files, account configuration, channel/user/workspace identifiers, and vault references;
- databases, artifacts, reports, receipts, logs, caches, and private documents;
- generated releases, current/rollback selectors, installation records, launcher manifests, and mutable projections;
- real employee, customer, payroll, route, safety, handbook, or policy fixtures;
- retired integrations and historical operational cutover material;
- vendored dependencies, virtual environments, model files, and browser binaries.

## Current configuration examples

`.env.example` contains only placeholder paths for the optional Handbook owner data root and index. It is documentation, not active configuration and not read automatically by the source commands. The index must remain below its declared owner data root.

Companion Bridge, Slack, browser collectors, Paycom, services, and schedules are outside the current component scope. Their live configuration was not copied, and placeholder templates will be added only when the corresponding onboarding contract is implemented and reviewed.

## Integration prerequisites

- **Core:** no credentials, network service, database, or external account is required for the current source proof.
- **Handbook query:** requires an explicitly selected absolute path to a verified local SQLite index.
- **Synthetic demo:** requires only Python and an operator-selected disposable output path; creation is explicit through `demo-init`.
- **Deferred authenticated integrations:** require separate setup, private secret storage, bounded authentication checks, and explicit enablement. They must never be activated by generic install, query, verify, or health operations.

## Behavioral boundary

Read-only query operations never collect, authenticate, browse, create services, or deliver messages. Missing optional configuration is reported as not configured or unavailable by choice, not silently repaired. Synthetic demo initialization is a separate, explicit mutating action.

Generated absolute paths and activation records belong only in a user's private installation roots. They must not be committed as canonical source.
