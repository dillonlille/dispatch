# Portable path configuration

Dispatch Core owns one non-mutating resolver in `dispatch-core/src/dispatch_core/paths/__init__.py`. It resolves canonical code separately from private configuration, data, state, cache, runtime, and build output. Constructing the resolver never creates directories.

## User installation root

The Phase 5 per-user default is:

```text
${DISPATCH_HOME}
```

`DISPATCH_HOME` must be absolute and defaults to:

```text
${HOME}/.dispatch
```

The installer owns creation and permissions. Core only resolves paths.

## Root precedence

| Root | Explicit override | Default |
|---|---|---|
| Canonical code | `DISPATCH_CODE_ROOT` | current candidate checkout when running maintained source; exact content-addressed release when installed |
| Private configuration | `DISPATCH_CONFIG_ROOT` | `${DISPATCH_HOME}/config` |
| Private data | `DISPATCH_DATA_ROOT` | `${DISPATCH_HOME}/data` |
| Mutable state | `DISPATCH_STATE_ROOT` | `${DISPATCH_HOME}/state` |
| Disposable cache | `DISPATCH_CACHE_ROOT` | `${DISPATCH_HOME}/cache` |
| Runtime state | `DISPATCH_RUNTIME_ROOT` | `${XDG_RUNTIME_DIR}/dispatch`, otherwise `${DISPATCH_HOME}/runtime` |
| One build invocation | `DISPATCH_BUILD_OUTPUT` | `<cache-root>/build/<owner>` |

Individual `DISPATCH_*_ROOT` settings override only their corresponding root. The installer normally projects all exact values instead of relying on a user's interactive shell environment.

Every configured value must be absolute. Relative paths, explicit `..` traversal, symlink-root aliases, overlapping primary roots, and private/build roots inside the canonical code release are rejected.

Outside a maintained source checkout, `DISPATCH_CODE_ROOT` must be supplied explicitly by the installer or launcher projection. It names the exact release directory, never a mutable `current` symlink.

## Installed layout

```text
~/.dispatch/
├── releases/<release-id>/
├── bin/
├── config/
├── data/
├── state/
├── cache/
└── staging/
```

The installer maintains the active release selector at:

```text
~/.dispatch/state/install/active-release.json
```

The release selector binds both the immutable tree manifest and the secret-free release receipt by SHA-256.

Default uninstall removes verified releases and operational roots while preserving `config` and `data`. Explicit purge removes those two roots as well. Secret-free retention and interruption receipts remain under `state/install` for keep-data uninstall; a purge uses a temporary digest-bound external journal so it can resume after the in-tree receipt is removed. See [`uninstallation.md`](uninstallation.md).

Browser runtime authority deliberately remains outside the user-owned tree:

```text
/etc/dispatch/browser-runtime-active.json
/opt/dispatch/browser-runtimes/<generation>/
```

Hermes is outside the installation path contract. It is assumed to be preinstalled, and the installer does not inspect or mutate its files, profiles, or environment.

## Owner roots

`DispatchPaths.owner_environment(owner)` derives bounded owner roots and the exact environment a launcher or service projection must receive. For `handbook`, this includes:

```text
DISPATCH_HANDBOOK_CONFIG_ROOT
DISPATCH_HANDBOOK_DATA_ROOT
DISPATCH_HANDBOOK_STATE_ROOT
```

Generic `DISPATCH_OWNER_*_ROOT` values are emitted at the same time. Owner IDs must be lowercase Dispatch slugs; they cannot contain separators or traversal.

The deferred Handbook adapter requires both `DISPATCH_HANDBOOK_DATA_ROOT` and `DISPATCH_HANDBOOK_INDEX`. The configured index must resolve to a physical file below the declared owner data root. A query never creates the root or index.

The explicit source-tree synthetic demo may use an operator-selected absolute temporary target. It is a development action, not installed configuration or a model-facing import capability.

## Projection contract

The installer renders installed launchers and future user-service units with absolute values from the selected installation layout and `owner_environment()`. Source templates must not contain a username, home directory, current release identity, or active private path.

Missing optional roots or integration-specific values are reported as not configured or unavailable by choice. Read-only query, verify, doctor, and health operations never authenticate, collect, browse, install, repair, or silently create configuration.
