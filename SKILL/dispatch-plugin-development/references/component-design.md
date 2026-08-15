# Component design

## Choose the class first

| Class | Use when | Must not be confused with |
|---|---|---|
| `hermes-tool` | The model invokes a bounded local tool | A service or script that has no Hermes registration |
| `collector` | The component acquires and publishes data | A model read action |
| `service` | A long-running bridge or broker is installed | A request/response tool |
| `auth-provider` | Privileged authentication is isolated | A domain collector |
| `library` | Code is imported by another component | A deployable plugin |
| `control-plane` | It schedules, queues, retries, or enforces shared policy | Domain collection implementation |
| `retired` | Compatibility is retained but new activation is forbidden | An active fallback |

## Preferred domain split

For data-backed plugins, use separate components:

```text
query (hermes-tool)
  -> verified local database/artifacts only

collector
  -> auth/browser/network
  -> staging
  -> domain validation
  -> atomic publication

coordinator
  -> schedule/queue/retry/reconciliation

delivery service
  -> Slack/Discord credentials and posting
```

A query must not collect because data is absent or stale. It should report `not_loaded` or `stale` and let the caller explicitly request collection through the approved control plane.

## Capability declaration

Declare all seven booleans for every component:

- `read_local_data`
- `mutate_data`
- `collect`
- `network`
- `authentication`
- `direct_delivery`
- `long_running`

The declaration describes effective behavior, not intent. A subprocess that posts to Slack means `network` and `direct_delivery` are true even when the adapter itself contains no HTTP client.

## Hermes actions

Use generic action names within a namespaced tool. Assign one privilege to every action:

- `read`
- `health`
- `mutation`
- `administration`
- `direct-delivery`

Mutation and direct delivery require corresponding capability declarations. Keep action count small enough for exact per-action input and output validation.
