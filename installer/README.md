# Dispatch clone installer

The installer is a standard-library runtime package carried by the Dispatch
checkout. The canonical `install.sh` bootstrap selects a channel, resolves a
stable GitHub Release when requested, clones `dillonlille/dispatch`, and runs
this package from the staged clone through `PYTHONPATH`.

## Channels

- **Latest Stable** resolves the newest published, non-draft, non-prerelease
  GitHub Release and checks out its tag detached.
- **Dev Branch** clones and tracks the `dev` branch.
- `--version TAG` selects an explicit stable tag. The dev channel always tracks
  `dev` and rejects `--version`.

The active checkout is `~/.dispatch/dispatch`. The bootstrap creates a
per-user virtual environment at `~/.dispatch/venv` and installs this package
editable from the active checkout. No wheel, release manifest, browser
artifact, or Hermes inspection is involved.

## Commands

```text
dispatch install --yes
dispatch update
dispatch repair --yes
dispatch channel stable
dispatch doctor
dispatch verify
dispatch browser status
dispatch browser reconcile
dispatch browser providers
dispatch setup --plugin handbook --yes
dispatch setup --plugin companion-bridge --yes
dispatch plugin configure companion-bridge
dispatch auth enroll amazon-operations
dispatch plugin-service status companion-bridge
dispatch plugin-service enable companion-bridge
dispatch plugin-service disable companion-bridge
dispatch uninstall --plan
dispatch uninstall --yes
dispatch uninstall --purge --yes
```

The final layout is:

```text
~/.dispatch/{dispatch,venv,config,secrets,data,state,cache,logs,run}
~/.dispatch/installation.json
~/.local/bin/dispatch
~/.config/systemd/user/dispatch.service
```

Default uninstall removes code, the virtual environment, cache, runtime,
launcher, service, and installation record while preserving `config`, `secrets`, `data`,
`state`, and `logs`. `--purge` removes the complete `~/.dispatch` tree.
Setup installs selected built-in plugins direct-source from `dispatch/plugins`,
installs their approved dependency closures, and writes `config/plugins.json`.
Long-running selections receive disabled receipt-owned service projections;
private configuration and explicit enablement remain separate steps.
Installation and repair install the pinned Core
requirements, then Browser Manager resolves the Chromium revision matched to
staged Playwright. A safe existing generation is reused; otherwise Chromium is
installed under `~/.dispatch/cache/browser-manager/playwright`. Browser Manager
scans shared libraries before invoking Playwright's system-dependency operation,
so a prepared host never receives an unnecessary sudo prompt. Missing libraries
may require normal administrator authorization; denial fails before activation.
A real sandboxed smoke launch must pass, and checkout, venv, and browser activate
or roll back together. Hermes is never inspected or modified.

Plugin services run through `dispatch plugin serve <id>` from the same verified
environment as Core. Service units and receipts contain only product paths and
plugin IDs, never plugin credentials. Deselect, update, repair, uninstall, and
rollback account for each owned service projection.
