---
name: dispatch-plugin-development
description: "Create or migrate Dispatch plugins to the canonical v1 lifecycle and conformance standard."
version: 1.0.0
metadata:
  hermes:
    tags: [dispatch, plugins, development, architecture, testing, release]
---

# Dispatch Plugin Development

Use this skill whenever creating, migrating, packaging, testing, installing, or reviewing a Dispatch plugin or component in `${DISPATCH_CODE_ROOT}`.

The canonical standard is:

```text
${DISPATCH_CODE_ROOT}/docs/dispatch-plugin-standard-v1.md
```

The root manifest schema is:

```text
${DISPATCH_CODE_ROOT}/docs/schemas/dispatch-plugin-v1.schema.json
```

The conformance command is:

```bash
python3 ${DISPATCH_CODE_ROOT}/dispatch-core/plugin-policy/plugin_conformance.py \
  ${DISPATCH_CODE_ROOT}/plugins/<owner>
```

## Non-negotiable rules

1. Classify every component before choosing its layout: `hermes-tool`, `collector`, `service`, `auth-provider`, `library`, `control-plane`, or `retired`.
2. Keep one canonical editable source tree. Never treat `runtime/releases/*` as the only source and never edit an active immutable release.
3. Add `README.md` and `dispatch-plugin.yaml` at the owner root.
4. Put each database under `db/<component>/`, each artifact store under `artifacts/<component>/`, and mutable operational state under `plugins/<owner>/`.
   For a single-component owner, the component ID normally equals the owner ID. When one owner has multiple independently released components, declare named `databases` or `artifact_stores` maps keyed by component ID. Do not hide a store, collapse live data, or force every store beneath the umbrella owner ID merely to fit singular fields.
5. Separate model-facing reads from collection, authentication, browser use, mutation, and platform delivery. Reads never trigger collection implicitly.
6. Keep Hermes adapters narrow. Shared infrastructure verifies releases, executes launchers, and delivers messages; domain behavior remains owner-local.
7. Require strict bounded schemas: required `action`, closed enum, `additionalProperties: false`, exact per-action fields, and bounded strings/rows/ranges/timeouts.
8. Provide executable `scripts/test`, `scripts/build`, `scripts/verify`, and `scripts/health` commands. Tests must discover a nonzero count without undocumented import paths.
9. Build deterministic immutable releases, use one activation authority, retain rollback, and verify all installed selectors converge.
10. Never place secrets, credentials, cookies, browser profiles, connection strings, or sensitive business rows in manifests, tests, documentation, skills, or receipts.
11. Keep Dispatch-owned built-in plugin source under `plugins/<owner>/`. External plugin source belongs in separate repositories. Build, version, verify, and release every plugin independently from Core.
12. Do not claim conformance until the conformance command passes and the real build/test/verify/health commands have been exercised.
13. Keep Core discovery minimal: an installable wheel publishes one `dispatch.plugins` entry point whose name matches the plugin ID and whose callable accepts one bounded request object and returns the standard envelope. Never add package-specific registry code to Core.

## Required workflow

### 1. Discover and classify

Read the owner README and root manifest if present. Inventory canonical source, generated releases, activation selectors, installed projections, operational state, external dependencies, and historical evidence. Do not infer authority from directory names.

### 2. Design capability boundaries

Declare capabilities explicitly. Prefer:

- a local read-only Hermes query component;
- a separate collector for browser/network/authenticated production;
- Collector Coordinator for scheduling, retries, and reconciliation;
- a shared delivery interface for Slack/Discord posting;
- a separate service or auth-provider component when privileges require it.

Only expose collection to the model when a documented interactive product requirement and separate security review explicitly approve model initiation. Scheduled, queued, background, browser-authenticated, or long-running collection defaults to Collector Coordinator only; an owner CLI `collect` command does not authorize a Hermes `collect` action.

### 3. Create the canonical files

At minimum:

```text
plugins/<owner>/README.md
plugins/<owner>/dispatch-plugin.yaml
plugins/<owner>/src/
plugins/<owner>/tests/
plugins/<owner>/scripts/{test,build,verify,health}
```

Add Hermes integration, runtime, service, config, references, state, locks, staging, and receipts only when the declared component uses them. Do not create meaningless empty directories.

Use `templates/dispatch-plugin.yaml` as a starting point and delete components that do not apply.

### 4. Implement and test the source contract

Keep changes small. Test registration, every action, malformed input, missing-data boundaries, response envelopes, release tamper rejection, query/collector separation, activation convergence, and non-mutating health. Avoid sprawling repetitive tests.

### 5. Build and activate safely

Run source tests first. Build into staging, verify the exact member set and stable hashes, publish an immutable content-addressed release, update the single activation authority, derive secondary projections, then smoke-test through the model-facing boundary. Preserve the prior active release as rollback.

If an owner has independently released query, collector, service, or auth components, use one activation record with an `interfaces` map. Bind each interface to its active runtime, exact rollback path and digest, and every launcher/manager/service projection. Do not force historical rollback directory names to look like current digest-prefix identities. If activation must replace files in a sealed owner directory, open only that directory for the replacement window and reseal it in `finally`; publish the root activation record last so interrupted activation fails closed.

Never patch an immutable release in place or switch a live service to mutable source merely to make verification pass.

### 6. Prove conformance

Run, in order:

```bash
./scripts/test
./scripts/build
./scripts/verify
./scripts/health
python3 ${DISPATCH_CODE_ROOT}/dispatch-core/plugin-policy/plugin_conformance.py .
```

Also test a fresh Hermes registration and representative safe action when a Hermes component exists. Report query readiness separately from producer/freshness readiness.

## Completion checklist

Before saying a plugin is complete:

- root manifest and README are accurate;
- canonical source is editable and outside runtime releases;
- component classes and privileges are explicit;
- owner data/artifact/state paths are correct;
- standard commands ran successfully;
- tests discovered a nonzero count;
- all advertised actions crossed the real adapter boundary;
- release manifest excludes volatile runtime byproducts;
- active and rollback identities are explicit;
- launcher, manager record, profile projection, service unit, and activation record agree;
- health reports registration, runtime, query, data, freshness, collector, auth, and delivery honestly;
- no secret or sensitive payload was exposed;
- conformance audit passes.

## References

- `references/component-design.md` — choose component classes and capability boundaries.
- `references/release-and-verification.md` — deterministic build, activation, rollback, and proof gates.
- `templates/dispatch-plugin.yaml` — root manifest starting template.
- Canonical full standard: `${DISPATCH_CODE_ROOT}/docs/dispatch-plugin-standard-v1.md`.
