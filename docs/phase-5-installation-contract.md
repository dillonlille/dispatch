# Phase 5 installation contract

## Purpose

Phase 5 turns reviewed Dispatch Core source into a deterministic online installation foundation. It does not implement setup, Authentication, Collection Manager, provider workflows, scheduling, delivery, or domain plugins.

## Intended public flow

Public release publication happens only after the owner decides the project is ready and all release gates pass. Private product-repository development and remote CI may happen earlier, but they create no copy-and-paste production installer. At final release time, a reviewed, versioned, digest-pinned terminal command will retrieve the bootstrap and approved component artifacts without cloning or downloading any source repository.

After a complete future installation, the `dispatch` CLI will offer **Start setup process** or **Skip for now**. Setup is not implemented here. A user who skips can later run:

```text
dispatch setup
```

## Selected ownership model

The selected model is hybrid:

- user-owned Dispatch code and mutable roots under `${DISPATCH_HOME}`, default `${HOME}/.dispatch`;
- transient sockets and locks under `${XDG_RUNTIME_DIR}/dispatch` where available;
- installer/root-owned browser and sandbox authority under `/etc/dispatch` and `/opt/dispatch/browser-runtimes`;
- reusable provider credentials in the future Authentication backend, never ordinary installation files.

Hermes is assumed to be preinstalled. The installer does not install, configure, inspect, create profiles for, or mutate Hermes.

## Phase 5 foundation implemented

The `dispatch-installer==0.1.0` component provides:

1. absolute layout resolution without mutation;
2. idempotent `0700` user-directory preparation;
3. a `0600`, secret-free layout receipt;
4. digest-pinned planning-manifest parsing with strict shapes and false-ready rejection;
5. strict future-GitHub HTTPS download primitives with host allowlisting, redirect revalidation, bounded streaming, exact size/SHA-256 checks, and private atomic staging;
6. Dispatch Core wheel identity, exact approved package-member hashes and dependency metadata, generated metadata closure, `RECORD`, expanded-size, hard-link, and path validation;
7. content-addressed immutable Core release staging;
8. full installed-tree and source-wheel rebinding verification;
9. atomic active-release selector publication;
10. preservation of the previous selector when staging fails and reconciliation of safe interrupted publication;
11. explicit uncertain-publication reporting if a new atomic value is visible but directory durability confirmation fails;
12. non-mutating layout, doctor, plan, and verify commands;
13. receipt-driven, non-mutating uninstall planning;
14. explicitly confirmed keep-data removal and purge, with lifecycle locking, secret-free journals, idempotence, and interruption recovery;
15. release-manifest declarations for the implemented user-scope uninstaller and the still-incomplete privileged browser remover.
16. a closed, digest-bound browser-generation manifest contract plus fixed-authority, generation-bound dependency, sandbox, and launch-probe receipt contracts for Ubuntu 24.04 x86-64;
17. exact synthetic-path browser tree staging with owner, link, type, mode, size, digest, member-set, expanded-size, nonblocking special-file rejection, and bottom-up directory durability enforcement;
18. immutable generation receipts and complete tree manifests consumed by Browser Manager, including exact sealed member modes and generation-bound copies of the three validated local evidence receipts;
19. one atomic active browser selector with candidate verification plus explicit-target rollback through the same one-selector primitive; rollback-target persistence remains the future install/upgrade transaction's responsibility.

The selected user home, `DISPATCH_HOME` parent, and transient runtime parent must already exist, belong to the invoking user, and not be group- or world-writable. Core artifacts must be absolute, unaliased, singly linked, user-owned regular files without group/world write permission. Persistent and transient primary roots must not overlap.

## Activation rule

A staged Core release is not sufficient for complete product activation. The public installation orchestration may publish a ready product only after all of these pass:

- the digest-pinned release manifest and installer bootstrap are complete and approved;
- the Core artifact and pinned Python dependency closure are downloaded, staged, and reverified;
- the installer-owned browser generation and sandbox receipts are published and pass Browser Manager authority validation;
- the final `dispatch` launcher is installed;
- post-install doctor and verify pass without setup, authentication, browser navigation, collection, scheduling, or delivery.

## Setup boundary

The planning manifest records only the future setup UX contract:

- `setup_implemented: false`;
- setup command: `dispatch setup`;
- post-install choices: `start_setup` or `skip_for_now`.

The installer must not collect credentials, account identifiers, provider configuration, MFA, CAPTCHA answers, schedules, or delivery settings.

## Current fail-closed boundary

`packaging/installation-release-manifest.json` intentionally remains `ready: false`, and schema version 1 rejects every `ready: true` declaration. The browser-generation transaction foundation currently accepts only an already assembled exact manifest-bound source tree and fresh, closed-shape evidence receipts from the fixed generation authority; it copies and binds those receipts into generations and is exercised only beneath isolated synthetic authority paths. Caller-selected evidence paths and self-supplied expected evidence digests are rejected by the API. Approved online URLs, artifact sizes/hashes, archive extraction, signature authority, the real Playwright/Chromium generation payload, trusted Ubuntu dependency/AppArmor/launch-probe producers, a selected-generation Python bootstrap, a production privileged helper, browser quiescence/service integration, privileged removal, launcher, and final bootstrap are outstanding. Therefore the installer exposes no production `install` command, and doctor reports explicit `production_release` and `browser_launch_composition` blockers even when Core and browser authority verify independently.

The implemented user-scope uninstaller preserves configuration and durable data by default and requires `--purge --yes` to remove them. It preserves Hermes and shared operating-system packages in every mode. It fails closed before mutation when root-owned browser authority, non-quiescent runtime state, invalid receipts, invalid releases, symlinks, hard links, ownership changes, or mount-boundary crossings are present. The exact contract is documented in [`uninstallation.md`](uninstallation.md).

This is not replaced with development cache authority, arbitrary paths, test wrappers, shared Playwright caches, or sandbox-disabling flags.

## Required acceptance before production

- clean Ubuntu 24.04 x86-64 installation with Python 3.11;
- repeated installation proves idempotence;
- interrupted staging preserves or safely reconciles activation;
- artifact, hard-link, installed-tree, selector, redirect, and manifest tampering fail closed;
- privileged browser generation provisioning and production sandbox probe pass;
- no secrets appear in source, manifests, receipts, logs, profiles, or model output;
- Python 3.12 and 3.13 matrix checks;
- final post-install setup choice is added only when setup itself exists;
- repair, upgrade, rollback, privileged browser removal, and complete service-integrated uninstall acceptance in their later lifecycle phase;
- rights and licensing review before publication.
