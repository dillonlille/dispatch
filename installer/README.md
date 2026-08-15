# Dispatch installer

This standard-library-only component owns the Phase 5 installation boundary. It is separate from Dispatch Core and Browser Manager.

## Intended user flow

After final publication, a user will copy a versioned, digest-pinned install command into a terminal. The bootstrap will retrieve the approved installer and product release manifest, verify them, install only the mandatory Core/dependency artifacts, and create the `dispatch` launcher. Built-in plugin artifacts remain optional declarations for `dispatch setup`; external plugins remain separate. The installer never clones source repositories, enumerates release assets, or infers packages from source layout.

GitHub publication and the copy-and-paste command are deliberately deferred until the complete release passes licensing, security, CI, clean-machine, and acceptance gates.

## Implemented foundation

- resolves the per-user installation under `${DISPATCH_HOME}`, defaulting to `${HOME}/.dispatch`;
- keeps browser authority fixed at `/etc/dispatch/browser-runtime-active.json` and `/opt/dispatch/browser-runtimes/`;
- prepares private user directories and a secret-free layout receipt idempotently;
- validates the fail-closed release-planning manifest;
- provides a strict GitHub HTTPS downloader with host allowlisting, redirect revalidation, exact size/SHA-256 verification, bounded streaming, private staging, and atomic publication, plus a Core-specific policy that accepts only an immutable versioned Core wheel URL;
- verifies a Dispatch Core wheel's identity, exact approved package-member hashes and dependency metadata, generated metadata closure, console entry point, top-level package, and complete `RECORD` before extraction;
- rejects aliased, hard-linked, or group/world-writable artifacts;
- binds release reuse to verified wheel bytes and reconciles safe interrupted staging/publication;
- stages Core into an immutable content-addressed release and activates it with an atomic selector;
- verifies the complete active Core release tree and detects tampering;
- provides non-mutating `doctor` and `verify` inspection.
- provides receipt-bound uninstall planning, keep-data removal, explicit purge, lifecycle locking, and interruption recovery;
- refuses user-scope removal when browser/runtime quiescence is unproven or privileged browser authority is present.

## Commands

```text
dispatch-installer layout
dispatch-installer prepare --yes
dispatch-installer doctor
dispatch-installer verify
dispatch-installer plan --manifest <path> --manifest-sha256 <digest>
dispatch-installer uninstall --plan
dispatch-installer uninstall --yes
dispatch-installer uninstall --purge --yes
```

`prepare` creates directories only. Standard uninstall preserves `config` and `data`; `--purge` is the separately confirmed destructive mode. Unknown top-level files and unknown release entries are not silently deleted. Shared system packages and Hermes are always preserved. See [`../docs/uninstallation.md`](../docs/uninstallation.md).

None of these commands configures accounts, credentials, authentication, browser sessions, collection, scheduling, delivery, or setup answers.

## Hermes boundary

Hermes is a user-supplied prerequisite. The installer does not install, configure, inspect, create profiles for, or otherwise mutate Hermes. No Hermes path or profile declaration appears in the installation manifest or installer layout.

## Setup boundary

Setup is not implemented in Phase 5. The manifest records the future UX contract only: after a complete installation, the `dispatch` CLI will offer **Start setup process** or **Skip for now**. A skipped setup can later be started with:

```text
dispatch setup
```

## Current production block

`packaging/installation-release-manifest.json` deliberately has `ready: false`, and planning schema version 1 rejects every `ready: true` declaration. Private GitHub source preparation does not create a public bootstrap URL or production `install` command. Approved online artifact URLs, a complete direct/transitive dependency closure, exact sizes/hashes, signature authority, authenticated private-asset transport or public distribution authority, browser generation payload, Ubuntu dependency receipt, sandbox/AppArmor receipt, privileged browser install/uninstall helper, launcher, service shutdown integration, and clean Ubuntu 24.04 acceptance remain required.

The staging API already requires exact path-to-SHA-256 and dependency-metadata policies and rejects all extra Core or `.dist-info` members. The future production manifest version must carry or digest-bind those policies from `packaging/runtime-package-plan.json`; planning schema v1 intentionally cannot authorize them.

## Ownership

```text
~/.dispatch/                         user-owned Dispatch installation and mutable roots
${XDG_RUNTIME_DIR}/dispatch/          transient user sockets and locks
/etc/dispatch/                        root-owned browser selector
/opt/dispatch/browser-runtimes/       root-owned immutable browser generations
```

Receipts contain versions, hashes, sizes, paths, and policy state only. Business credentials, provider secrets, cookies, and browser sessions are never installation material.
