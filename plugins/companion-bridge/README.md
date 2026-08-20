# DSP Companion Bridge

A source-owned Dispatch plugin that receives allowlisted Slack Socket Mode events, maps Slack threads to Companion conversation IDs, and forwards Companion answer deltas through Slack native streaming.

## Security boundary

Each Companion request obtains a request-scoped Core `BrowserManager` lease and `AuthenticationManager` session in the fixed `amazon-operations` realm. The managed page is authenticated, the exact Companion context endpoint is probed, and cookies, user-agent, and CSRF metadata are snapshotted in memory. The lease is released in `finally` immediately after that proof. Only then does the bridge create a direct `httpx` SSE request. Session material is never logged, persisted, returned by health, or sent to Slack.

The plugin does not own browser installation, browser profiles, credential storage, authentication recovery helpers, or browser transport control. Core owns those boundaries. Private configuration, Slack credentials, and the thread mapping database are derived from `DispatchPaths` and are separate from this source tree.

## Slack behavior

- Access is deny-by-default: approved users and channels are required; teams can also be restricted.
- Top-level mentions start a thread mapping; replies continue it.
- Repeated event IDs are deduplicated.
- Reset commands advance a generation token so an in-flight response cannot restore a stale mapping.
- Global and per-user concurrency limits use a bounded wait; requests fail with a safe busy notice instead of growing an unbounded queue.
- Answer text is buffered before `chat.appendStream`; transport IDs can be rewritten from a private, read-only driver-name database when configured.
- Logs and user-visible failures pass through secret redaction. Prompts and answers are not logged.

## Setup

Product setup installs the plugin from a validated private source copy. For source development, install it editable:

```bash
python -m pip install -e plugins/companion-bridge
export DISPATCH_ACTIVE_PLUGINS=companion-bridge
```

Run the secure configurator through the Dispatch configurator entry point. It
prompts for Slack tokens through hidden input and writes them only below the
Dispatch secrets root. Configure at least one approved channel and user plus an
admin alert channel, then explicitly enable the exactly generated service:

```bash
dispatch plugin configure companion-bridge
dispatch auth enroll amazon-operations
dispatch plugin-service enable companion-bridge
```

The foreground service entry point is `companion_bridge.foreground_service:run`;
it starts Slack Socket Mode on the foreground process and does not detach a
worker. Source health reports prerequisites as `configured`, not operationally
`ready`; installer service status is the separate proof that the foreground
process remained active. A successful Companion request is the bounded proof
of Amazon session/context readiness. `dispatch plugin-service disable
companion-bridge` stops it without removing durable private configuration or
conversation state.

## Source checks

```bash
./scripts/test
./scripts/build
./scripts/verify
./scripts/health
```

All tests use synthetic page/context/auth/browser objects and a fixture HTTP transport. They do not launch a browser, authenticate to Amazon, connect to Slack, or make live network calls.
