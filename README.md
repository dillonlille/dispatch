# Dispatch

Dispatch is being rebuilt as a clean-installable, per-user Linux product. This is the public product-source repository, not yet a published release. It contains Dispatch Core, the installer, and independently packaged Dispatch-owned built-in plugin source. External plugins live in separate repositories. The fail-closed installer foundation is not yet a complete production installer.

The repository uses GitHub-hosted source verification. Product installation, browser, and authorized-account acceptance remain reserved for a separate clean test system.

## Repository structure

Dispatch Core is a first-class root-level product component, not a plugin:

```text
dispatch-core/
  paths/
  health/
  command-interface/
  plugin-policy/
  lifecycle/
  collection-manager/
  authentication/
  browser-manager/
installer/
plugins/
  handbook/
```

Every Core feature owns its directory. `browser-manager` implements private profiles, durable leases, process-safe locking, tracked temporary Chromium lifecycle, timeout/crash handling, and reconciliation. `authentication` implements encrypted credential storage and a bounded Amazon/Paycom login workflow. `collection-manager` implements bounded registration, a transactional durable queue, retries, cancellation, reconciliation, fixed-interval schedules, durable receipts, and synchronous Browser/Authentication coordination. Domain integrations remain under `plugins/`.

## Repository and installation boundary

The Git checkout is a development source boundary, not an installation payload. A developer clone contains Core, installer, and built-in plugin source. External plugin source remains in separate repositories. The product installer must instead consume one exact digest-bound Dispatch release manifest. Initial installation consumes only its mandatory installer, Core, and dependency declarations; `dispatch setup` may later install built-in plugins explicitly declared by the same product release. Installation must never clone this repository, download its source archive, enumerate release assets, scan `plugins/`, or infer packages from neighboring directories.

Core builds only from `dispatch-core/`; the installer builds only from `installer/`. Each built-in plugin builds independently from `plugins/<owner>/`; external plugins build from their own repositories. Built-in plugin artifacts are available through the product release catalog but are installed only through setup or another explicit plugin operation. Core has no mandatory third-party runtime dependency and must start, report health, and execute its control-plane behavior with zero installed plugins. Authentication and browser dependencies are capability extras installed only when setup requires them. `packaging/runtime-package-plan.json` defines the exact Core package closure; `policy/public-source-scope.json` defines the larger reviewed repository source closure and is never installer authority.

The GitHub workflows under `.github/workflows/` build only explicit project roots and verify the Core wheel against the exact runtime plan. The manual artifact workflow produces short-lived review artifacts only; it does not publish a production release or mark the installation manifest ready.

## Implemented source scope

- root-level feature-oriented Dispatch Core;
- portable path resolution, bounded health and command interfaces, plugin policy, and deterministic lifecycle controls;
- Dispatch Plugin Standard v1, schema, and public plugin-development guidance;
- a standard-library installer foundation using `${HOME}/.dispatch` for per-user Dispatch code and mutable roots;
- digest-pinned installation-manifest, approved-GitHub download, transactional Core release, doctor, verification, and receipt-bound user-scope uninstall primitives;
- a closed, digest-bound browser-generation manifest plus fixed-authority evidence receipts, synthetic-path immutable staging, full-tree verification, one atomic active selector, and explicit-target rollback primitives owned by the installer;
- a pinned Core and browser runtime package plan for the future production install orchestration;
- independently packaged Handbook plugin source with one standard Core discovery entry point and one explicitly declared fictional demo fixture;
- fail-closed source-scope, provenance, path, fixture, configuration, and secret checks;
- focused source, lifecycle, Authentication, Browser Manager, and Collection Manager tests.

No domain collectors, Companion Bridge, Slack delivery, enabled schedules, production documents, or private Handbook content are included. Collection Manager can durably queue, schedule, and supervise registered collectors, but there are currently zero registered collectors and zero configured schedules. Authorized live-account acceptance and persistent OS service installation remain pending. Handbook is discoverable after explicit setup, but production index onboarding remains outside the immediate readiness contract.

## Current readiness

The current scope supports the `${HOME}/.dispatch` per-user layout, feature-oriented Core source tests, deterministic Core source builds, encrypted Authentication storage, bounded Amazon/Paycom login orchestration, Browser Manager lifecycle infrastructure, durable Collection Manager orchestration with bounded worker-process supervision, plugin conformance checks, exact Core-wheel closure verification, source privacy verification, and the non-deploying browser-generation transaction foundation described above. Hermes is assumed to be preinstalled and is not inspected, configured, or removed. Setup is deferred to a future `dispatch setup` flow. The source does **not** yet provide a published GitHub bootstrap, complete release artifacts, authenticated private-release retrieval, authorized live-account Authentication acceptance, the production privileged browser helper and real sandbox/dependency probes, persistent OS service registration, complete product upgrade/rollback orchestration, or clean-machine acceptance.

## Source, data, and privacy boundaries

The source candidate contains canonical source and declared synthetic test material only. It must never contain credentials, active configuration, private documents, production databases, business artifacts, browser sessions, logs, receipts, installed selectors, or generated deployment authority.

See [`docs/public-source-boundary.md`](docs/public-source-boundary.md) and [`docs/path-configuration.md`](docs/path-configuration.md).
The current installer boundary is defined in [`docs/phase-5-installation-contract.md`](docs/phase-5-installation-contract.md).
Repository controls are defined in [`docs/private-github-setup.md`](docs/private-github-setup.md).
The user-scope uninstall boundary is defined in [`docs/uninstallation.md`](docs/uninstallation.md).

## Configuration and integration prerequisites

The current Core proof needs no credentials or external accounts. Built-in plugins are never installed by the mandatory Core-only installation phase; explicit setup activates only manifest-approved wheels. No integration receives a public configuration template until its credential, browser, lifecycle, and privacy boundaries are reviewed.

## Development checks

From the candidate root:

```bash
./scripts/verify-source-export
python3 -B -m unittest -v tests/test_verify_source_export.py
python3 -m pytest installer/tests
dispatch-core/scripts/test
dispatch-core/scripts/verify
dispatch-core/scripts/health
plugins/handbook/scripts/test
plugins/handbook/scripts/verify
plugins/handbook/scripts/health
```

The Handbook checks exercise its independent source contract and generic `dispatch.plugins` discovery boundary; they do not add Handbook to mandatory Core installation.

GitHub-hosted CI additionally builds the Core and installer from their explicit project roots, runs `scripts/verify-core-wheel` against the Core artifact, and clean-installs each wheel on an ephemeral runner. It deliberately does not run live browser, account, installed-host health, or production service acceptance.

Test dependencies remain separate from future production dependencies.

## License

Copyright 2026 Dillon Lille.

Dispatch is licensed under the [Apache License 2.0](LICENSE).
