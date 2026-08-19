# Dispatch uninstallation

Dispatch uninstallation removes application code and disposable runtime material while preserving durable private data by default. It never removes Hermes, unrelated files, another user's installation, or shared operating-system packages.

## Commands

Inspect the exact plan without mutation:

```text
dispatch uninstall --plan
```

Remove Dispatch while retaining durable data:

```text
dispatch uninstall --yes
```

Explicitly remove the complete Dispatch root, including durable data:

```text
dispatch uninstall --purge --yes
```

The command requires `--plan` or `--yes`; there is no implicit destructive default.

## Ordinary uninstall

Ordinary uninstall removes only validated Dispatch-owned paths:

- `${DISPATCH_HOME}/dispatch`;
- `${DISPATCH_HOME}/venv`;
- `${DISPATCH_HOME}/cache`;
- `${DISPATCH_HOME}/run`;
- `${DISPATCH_HOME}/installation.json`;
- temporary installation directories;
- `${HOME}/.local/bin/dispatch` when it matches the Dispatch-owned launcher;
- the Dispatch systemd user service and its generated service record.
- every receipt-owned selected-plugin service projection;

It preserves:

- `${DISPATCH_HOME}/config`;
- `${DISPATCH_HOME}/secrets`;
- `${DISPATCH_HOME}/data`;
- `${DISPATCH_HOME}/state` except the generated service record;
- `${DISPATCH_HOME}/logs`;
- unrelated top-level files and directories;
- shared operating-system and browser dependencies;
- all Hermes binaries, profiles, configuration, state, and runtime files.

`cache` and `run` are disposable. Durable configuration, credentials, data, operational state, and logs survive updates, repairs, channel switches, and ordinary uninstall.

Before code, environment, or Browser Manager removal, Dispatch stops every owned
plugin service. A modified, unrelated, or unreceipted unit blocks removal rather
than being guessed as Dispatch-owned. Deselecting a long-running plugin follows
the same rule while preserving that plugin's private durable roots.

## Purge and safety

`--purge --yes` is the only mode that removes the complete `${DISPATCH_HOME}` tree. It is explicit and cannot be inferred from setup state or a missing launcher.

Symlinks, non-directory managed roots, unexpected command content, unsafe service units, and other ownership/type conflicts stop mutation rather than causing broad deletion. The uninstaller never treats `HOME` as disposable.

The source checkout is ordinary Git state. There is no active-release selector, generated release tree, installation receipt, or artifact catalog to retain or interpret.
