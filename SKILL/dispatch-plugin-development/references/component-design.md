# Component design

## Choose the class first

| Class | Use when | Must not be confused with |
|---|---|---|
| `hermes-tool` | The model invokes a bounded local tool | A service or script with no Hermes registration |
| `collector` | The component acquires and validates data | A model read action |
| `service` | A long-running bridge or broker is needed | A request/response tool |
| `auth-provider` | Privileged authentication is isolated | A domain collector |
| `library` | Code is imported by another component | A separately exposed plugin |
| `control-plane` | It schedules, queues, retries, or reconciles work | Domain collection implementation |
| `retired` | Compatibility is retained but new use is forbidden | An active fallback |

## Preferred capability split

For data-backed plugins:

```text
query (hermes-tool)
  -> verified local database/artifacts only

collector
  -> auth/browser/network
  -> validation and publication

coordinator
  -> schedule/queue/retry/reconciliation

delivery service
  -> Slack/Discord credentials and posting
```

A query must not collect because data is absent or stale. It reports `not_configured`, `not_loaded`, or `stale` and leaves collection to an explicitly approved boundary.

Long-running service plugins use the generic `dispatch.services` entry-point
contract and an installer-owned service projection. Do not disguise an endless
service loop as a bounded `dispatch.plugins` request. Secrets are onboarded only
through an explicit trusted configurator or Core authentication boundary, and
service selection never means automatic enablement.

## Metadata and data

Declare the plugin identity and effective capability labels in `pyproject.toml`:

```toml
[tool.dispatch]
id = "example"
capabilities = ["read_local_data"]
```

Keep the cloned source and tests under `plugins/<owner>`. Keep owner-managed data under `plugins/<owner>/data` or a documented operator-owned private root. Do not store secrets or private records in source-controlled files.

## Hermes actions

Use generic action names within a namespaced tool. Assign a privilege to every action:

- `read`
- `health`
- `mutation`
- `administration`
- `direct-delivery`

Mutation and direct delivery require corresponding capability declarations. Keep action count small enough for exact per-action input and output validation. Require `action`, close the schema with `additionalProperties: false`, and bound every string, row count, byte count, range, and timeout.
