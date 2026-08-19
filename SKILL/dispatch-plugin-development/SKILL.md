---
name: dispatch-plugin-development
description: "Create, migrate, test, and review source-owned Dispatch plugins."
version: 2.0.0
metadata:
  hermes:
    tags: [dispatch, plugins, development, architecture, testing]
---

# Dispatch Plugin Development

Use this skill when creating, migrating, testing, installing, or reviewing a Dispatch plugin under `${DISPATCH_CODE_ROOT}`.

The canonical public contract is:

```text
${DISPATCH_CODE_ROOT}/docs/dispatch-plugin-standard-v1.md
```

The source conformance audit is:

```bash
python3 ${DISPATCH_CODE_ROOT}/dispatch-core/plugin_policy.py \
  ${DISPATCH_CODE_ROOT}/plugins/<owner>
```

## Non-negotiable rules

1. Keep one canonical, editable source tree under `plugins/<owner>/` for a built-in plugin. External plugins remain in their own cloned repositories.
2. Declare the plugin ID and effective capabilities in `pyproject.toml`:

   ```toml
   [tool.dispatch]
   id = "example"
   capabilities = ["read_local_data"]
   ```

3. Publish exactly one `[project.entry-points."dispatch.plugins"]` entry whose name equals that ID. Its callable accepts one bounded JSON object and returns the exact seven-field response envelope.
   A plugin declaring `collect` also publishes exactly one matching
   `[project.entry-points."dispatch.collectors"]` zero-argument provider that
   returns a bounded tuple of Core `CollectorRegistration` objects.
   A plugin declaring `long_running` also publishes exactly one matching
   `[project.entry-points."dispatch.services"]` foreground service callable.
   Interactive private onboarding uses an optional matching
   `[project.entry-points."dispatch.configurators"]` callable, never a model action.
4. Install the cloned source editable into the shared Dispatch virtual environment:

   ```bash
   python -m pip install -e ${DISPATCH_CODE_ROOT}/plugins/<owner>
   export DISPATCH_ACTIVE_PLUGINS=<owner>
   ```

   Core discovers installed `dispatch.plugins` entry points from that environment and filters them only by `DISPATCH_ACTIVE_PLUGINS`. `DISPATCH_PLUGIN_PATHS` is obsolete.
5. Keep owner-managed data outside source, normally under `plugins/<owner>/data` or an explicitly documented private data root. Never put secrets, cookies, credentials, or private business rows in source, manifests, tests, skills, or receipts.
6. Keep query, collection, authentication, browser, mutation, service, and delivery capabilities separate. A read action never collects implicitly.
   Plugin selection installs source but does not by itself authorize service
   enablement; configuration and enablement remain explicit operator steps.
7. Use bounded schemas: required `action`, a closed action enum, `additionalProperties: false`, exact action fields, and bounded strings/rows/ranges/timeouts.
8. Provide executable `scripts/test`, `scripts/build`, `scripts/verify`, and `scripts/health` commands. They operate on source and local configuration; they do not build, publish, activate, or verify generated plugin artifacts.
9. Keep the Hermes adapter narrow. Its tool name, toolset, action schema, availability check, and response envelopes must agree with the source entry point and optional `dispatch-plugin.yaml`.
10. Do not claim conformance until the real source tests, build check, verification audit, and health check have run.

## Required source layout

```text
plugins/<owner>/
├── README.md
├── pyproject.toml
├── dispatch-plugin.yaml       # optional; if present, id must match pyproject
├── SKILL.md                   # when a model-facing tool exists
├── src/
├── tests/
├── references/                # only when domain contracts are needed
├── integration/hermes-plugins/<package>/  # only when a Hermes projection exists
└── scripts/{test,build,verify,health}
```

Do not create `runtime/`, `current` pointers, generation directories, release manifests, activation records, or receipt machinery for a source-owned plugin.

## Workflow

1. Clone or update the built-in source under `plugins/<owner>` and read its README.
2. Classify the component and declare only the capabilities it actually uses.
3. Add the `pyproject.toml` Dispatch metadata and matching entry point.
4. Keep owner data paths explicit and private; keep source and data separate.
5. Exercise the source boundary:

   ```bash
   ./scripts/test
   ./scripts/build
   ./scripts/verify
   ./scripts/health
   python3 ${DISPATCH_CODE_ROOT}/dispatch-core/plugin_policy.py .
   ```

6. Install editable into the shared environment and test selection through `DISPATCH_ACTIVE_PLUGINS`.

## Contract checks

- pyproject metadata has a valid `tool.dispatch.id` and non-empty capability list;
- the entry-point group has exactly one matching plugin ID and a loadable source callable;
- `collect` capability and `dispatch.collectors` metadata agree exactly;
- long-running capability and `dispatch.services` metadata agree exactly;
- any `dispatch.configurators` entry point is operator-only and never accepts secret JSON fields;
- an optional root manifest has the same ID as pyproject metadata;
- lifecycle scripts are regular owner-executable files and not group/world writable;
- entry-point and Hermes responses use exactly `ok`, `action`, `status`, `data`, `freshness`, `delivery`, and `error`;
- Hermes schemas require only bounded, closed inputs and register the declared tool exactly once;
- health distinguishes registration, runtime, query, data, freshness, collector, authentication, delivery, and overall state.

## References

- `references/component-design.md` — component classes and capability boundaries.
- `references/standard-v1.md` — source ownership, setup, data paths, and contracts.
- `references/release-and-verification.md` — source test, verification, and health workflow (no artifact release).
- `templates/dispatch-plugin.yaml` — optional root manifest starting point.
