# Dispatch uninstallation contract

## Status

The installer foundation implements a receipt-bound, offline user-scope uninstaller. It is not yet the complete production uninstaller because the privileged browser-runtime removal helper and service-supervisor shutdown integration do not exist. When root-owned browser authority or non-quiescent Browser Manager state is present, user-scope mutation fails closed before deleting anything.

`packaging/installation-release-manifest.json` records that user-scope removal is implemented, purge requires confirmation, and privileged browser removal remains unimplemented. The overall release therefore remains fail-closed with `ready: false`.

Hermes and shared operating-system dependencies are always preserved.

## Commands

Inspect the exact plan without mutation:

```text
dispatch-installer uninstall --plan
```

Remove installed code and disposable operational material while preserving configuration and durable data:

```text
dispatch-installer uninstall --yes
```

Explicitly remove configuration and durable data as well:

```text
dispatch-installer uninstall --purge --yes
```

The future `dispatch uninstall` command may wrap this administrative entry point after the launcher exists. The administrative command requires either `--plan` or `--yes`; there is no implicit destructive default.

## Default preservation boundary

The default `keep-data` operation preserves:

- `${DISPATCH_HOME}/config`;
- `${DISPATCH_HOME}/data`;
- unknown top-level material;
- unknown entries under the immutable-release root;
- secret-free layout and uninstall receipts needed to explain the retained installation state;
- shared operating-system packages;
- every Hermes binary, profile, configuration file, and runtime;
- root-owned browser authority, although its presence blocks the current operation pending the privileged helper.

It removes only validated user-owned Dispatch roots and verified Core releases:

- active Core selector and verified immutable Core releases;
- `${DISPATCH_HOME}/bin`;
- `${DISPATCH_HOME}/cache`;
- `${DISPATCH_HOME}/staging`;
- transient runtime material after quiescence is proven;
- ordinary user-owned operational state.

An unknown release entry is preserved by default and blocks purge. A symlink, hard-linked regular file, unsupported file type, unexpected owner, mount-boundary crossing, invalid selector, invalid release, or invalid receipt blocks mutation.

## Purge boundary

`--purge --yes` additionally removes the validated `config` and `data` roots. It is the explicit destructive mode and must not be inferred from setup state, a missing launcher, or a previous standard uninstall.

If unrelated top-level material exists in `${DISPATCH_HOME}`, it is preserved and the result is `purged-with-preserved-files`. The uninstaller never treats the entire home directory or an arbitrary environment-provided path as disposable.

## Transaction and recovery model

All installation and uninstallation mutations take a lifecycle lock on the physical user home directory and the existing installation transaction lock. This prevents a conforming installer, activation, or uninstaller process from racing the final deletion window even after the in-tree lock path is removed.

Each operation re-plans under the lock before mutation. A keep-data operation records an in-tree transaction journal and publishes a final secret-free uninstall receipt. Before the first release mutation, the transaction records every exact release ID, device/inode identity, tree-manifest hash, and release-receipt hash authorized for deletion. The uninstaller then atomically renames each verified immutable release to its same-parent quarantine; only a quarantine matching that exact transaction record may be resumed, while unrelated pattern-shaped or replacement quarantines block and remain untouched. Immediately before recursive mutation, the remover reopens the quarantine with `O_NOFOLLOW`, compares the resulting descriptor's device/inode to the transaction record, and operates through that pinned descriptor; a same-name replacement after pathname preflight is rejected and preserved. An interrupted, partially removed authorized quarantine can be validated as an owned residual tree and safely resumed without pretending the damaged release is still intact. A purge also publishes a restrictive external journal in the physical home before deleting internal authority. The journal contains the exact validated canonical layout receipt, its unique installation ID and SHA-256, and the original `DISPATCH_HOME` device/inode identity. It permits a repeated `--purge` invocation to resume after an interruption while rejecting arbitrary digest-only journals, stale journals from a previous installation generation, and replacement trees at the same path. The recorded `DISPATCH_HOME` device/inode is revalidated after journal loading, before installation-lock state can be created, after the final plan under that lock, and again at apply entry before destructive mutation; a root substituted in any covered window is preserved and the purge fails closed. Transaction or external-journal cleanup is treated as the final filesystem mutation; the uninstaller performs its privileged-authority and postcondition check afterward and reports incomplete rather than success if authority appeared during final cleanup.

Tree validation and descriptor-relative removal compare Linux mount IDs from `/proc/self/fdinfo` at every directory and non-directory member. This rejects nested bind mounts even when they share the same filesystem device number, and the check is repeated during removal to fail closed on a mount introduced after planning.

A second completed invocation is idempotent: it reports `already-uninstalled` for retained-data mode or `already-absent` after a complete purge.

## Quiescence and privileged boundary

The current foundation blocks when:

- the user runtime root is non-empty;
- Browser Manager state is non-empty and shutdown/reconciliation has not been proven;
- `/etc/dispatch/browser-runtime-active.json` exists;
- `/opt/dispatch/browser-runtimes/` contains generations.

A later privileged helper must independently verify root-owned generation receipts, process quiescence, selector identity, tree integrity, and exclusive Dispatch ownership before atomically deactivating a selector or removing a generation. It must never remove Hermes, Playwright shared caches, unrelated browser installations, arbitrary system paths, or operating-system packages merely because Dispatch once required them.

## Network and secrets

Uninstallation performs no downloads. Receipts and journals contain paths, modes, hashes, policy state, and operation status only. They must never contain credentials, cookies, provider configuration, setup answers, tokens, or browser session contents.
