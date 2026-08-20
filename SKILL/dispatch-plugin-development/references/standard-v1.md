# Dispatch plugin source standard v1

This reference defines the source contract for built-in and cloned Dispatch plugins. It deliberately does not define wheel publication, generated runtime copies, activation selectors, immutable generations, or receipts.

## Source ownership

A built-in plugin is maintained as a clone under `plugins/<owner>/`. That clone is authoritative for implementation, tests, documentation, metadata, and source checks. Product setup validates the clone, copies it to private temporary build state, and installs it into the shared environment without mutating source. Developers may install it editable:

```bash
python -m pip install -e plugins/<owner>
export DISPATCH_ACTIVE_PLUGINS=<owner>
```

Core reads the installed environment's `dispatch.plugins` entry points and filters by the selected IDs. It does not scan source directories. `DISPATCH_PLUGIN_PATHS` is obsolete.

## Required metadata

`pyproject.toml` uses the installer-supported pinned build backend and declares plugin identity and effective capabilities:

```toml
[build-system]
requires = ["setuptools==83.0.0"]
build-backend = "setuptools.build_meta"

[project.entry-points."dispatch.plugins"]
example = "dispatch_example.service:handle"

[tool.dispatch]
id = "example"
capabilities = ["read_local_data"]
```

The entry-point group contains exactly one entry named `tool.dispatch.id`. The target is a callable accepting one bounded JSON object. The plugin keeps its own dependencies in `[project.dependencies]`; it does not need a `dispatch-core` distribution dependency merely to be discoverable.

A root `dispatch-plugin.yaml` is optional for source metadata. When present, its `id` must exactly match `tool.dispatch.id`. It may describe owner data, components, Hermes projections, and commands, but it is not a generated runtime authority.

## Source layout and owner data

```text
plugins/<owner>/
├── README.md
├── pyproject.toml
├── dispatch-plugin.yaml       # optional
├── SKILL.md                   # when model-facing
├── src/
├── tests/
├── integration/hermes-plugins/<package>/  # optional
└── scripts/{test,build,verify,health}
```

Source, tests, and local configuration stay in the clone. Owner-managed data is separate, normally under `plugins/<owner>/data`; a plugin may document an operator-owned private root for larger databases or artifacts. Secrets and private records never enter the clone. A read-only query must not create or refresh its data implicitly.

## Capabilities

Declare only effective behavior. Supported capability labels are:

- `read_local_data`
- `mutate_data`
- `collect`
- `network`
- `authentication`
- `direct_delivery`
- `long_running`

Prefer a read-only query component for local data, a separately privileged collector for network/authenticated production, and a separate delivery or service boundary where needed.

## Core and Hermes contracts

Core discovery is the Python entry point. The standard response is exactly:

```json
{
  "ok": true,
  "action": "health",
  "status": "ready",
  "data": {},
  "freshness": null,
  "delivery": null,
  "error": null
}
```

Responses must use exactly those seven top-level fields. Successful responses have `error: null`; failures have a bounded `{ "code": "...", "message": "..." }` error object. Health includes registration, runtime, query, data, freshness, collector, authentication, delivery, and overall readiness values.

A Hermes projection, when present, registers exactly one tool. Its schema requires `action`, uses a closed enum, rejects additional properties, and declares exact per-action fields. The adapter's tool name, toolset, `plugin.yaml.provides_tools`, action list, availability check, and exercised responses must agree.

## Source commands and conformance

Every source-owned plugin provides executable commands:

```bash
./scripts/test
./scripts/build
./scripts/verify
./scripts/health
python3 dispatch-core/plugin_policy.py plugins/<owner>
```

`build` is a source syntax/import check, `verify` runs the source conformance audit, and `health` is read-only. None of these commands creates or checks generated plugin artifacts. Conformance checks metadata, optional manifest identity, entry-point loading, exact envelopes and tool schemas, and script permissions.
