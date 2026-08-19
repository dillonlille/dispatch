# Portable path configuration

Dispatch Core resolves code, private configuration, durable data, mutable state, disposable cache, runtime state, and temporary installation work as separate roots. Resolving paths is non-mutating; the installer performs directory creation.

## User installation root

`DISPATCH_HOME` must be an absolute, user-owned, non-symlink directory. It defaults to:

```text
${HOME}/.dispatch
```

The default layout is:

```text
${DISPATCH_HOME}/
├── dispatch/                  selected Git checkout
├── venv/                      per-installation Python environment
├── config/                    private configuration
├── secrets/                   private secret storage
├── data/                      durable user data
├── state/                     mutable operational state
├── cache/                     disposable downloads and test cache
├── logs/                      private operational logs
└── run/                       transient sockets and locks
```

There is no active-release selector or content-addressed release tree in the source installation contract. The selected source ref is recorded as ordinary installation state and is changed only by an explicit channel update.

## Root precedence

| Root | Explicit override | Default |
|---|---|---|
| Canonical code | `DISPATCH_CODE_ROOT` | `${DISPATCH_HOME}/dispatch` outside a checkout; current checkout during development |
| Private configuration | `DISPATCH_CONFIG_ROOT` | `${DISPATCH_HOME}/config` |
| Private secrets | `DISPATCH_SECRETS_ROOT` | `${DISPATCH_HOME}/secrets` |
| Private data | `DISPATCH_DATA_ROOT` | `${DISPATCH_HOME}/data` |
| Mutable state | `DISPATCH_STATE_ROOT` | `${DISPATCH_HOME}/state` |
| Disposable cache | `DISPATCH_CACHE_ROOT` | `${DISPATCH_HOME}/cache` |
| Private logs | `DISPATCH_LOGS_ROOT` | `${DISPATCH_HOME}/logs` |
| Runtime state | `DISPATCH_RUNTIME_ROOT` | `${DISPATCH_HOME}/run` |

Every configured path must be absolute. Reject explicit `..` traversal, symlink aliases, overlapping primary roots, and private roots inside the source checkout. A maintained source checkout may use its own root for code, but private and generated roots must remain outside it.

### Local trust boundary

Dispatch uses the Unix account as its local security boundary. The installation owner UID is trusted: a malicious process already running as that same UID can inspect private credentials, interfere with processes, and rename user-owned directories, so Dispatch does not claim isolation from a hostile same-UID process. Filesystem hardening instead fails closed on unsafe pre-existing paths, symlinks, ownership or mode conflicts, process interruption, and accidental concurrent lifecycle operations. Isolation from untrusted local code requires a separate OS account, container, or equivalent operating-system boundary.

The `dispatch/` checkout and `venv/` environment are installer-owned lifecycle paths. Core does not expose separate overrides for them in plugin owner-path output; the installer records both in `installation.json` and projects the checkout as `DISPATCH_CODE_ROOT`.

## Stable and development channels

A stable installation uses a published, non-draft, non-prerelease GitHub Release
tag and remains detached at that tag. A development installation uses current
`main` and remains attached to it. Explicit channel switching preserves the
durable roots. Development updates refuse local changes and merge `origin/main`
with `--ff-only`; `dev` remains the public channel label only.

Use `dispatch update` to advance the selected channel and `dispatch channel stable|dev` to switch channels. Do not manually reset the installed checkout around the lifecycle command.

## Ownership and projections

Core's path API supports explicit absolute root overrides for development, embedded process use, and intentionally external private storage. Installed launchers and user services project the exact validated roots selected at installation time: defaults remain beneath `DISPATCH_HOME`, while explicit `DISPATCH_*_ROOT` values may be separate non-overlapping private directories and are preserved for lifecycle and uninstall operations. Paths must not embed a username, a branch-specific private path, or a secret.

Hermes is outside the Dispatch path contract. Dispatch never inspects, configures, creates profiles for, or removes Hermes files.
