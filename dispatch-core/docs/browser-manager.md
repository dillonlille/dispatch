# Browser Manager

Browser Manager provides version-matched Playwright Chromium provisioning, isolated sessions, provider contracts, realm policy, durable leases, crash recovery, and profile locking.

## Ownership boundary

All browser policy lives under `dispatch-core/browser_manager/`.

The install-time provisioner:

- reads the exact Playwright version and Chromium revision from the staged virtual environment;
- scans an existing managed cache and reuses only the exact safe revision;
- migrates the former `~/.dispatch/cache/browser` cache only when it matches and is safe;
- downloads missing Chromium into owner-only staging;
- scans dynamic host libraries and invokes Playwright's supported system-dependency command only when libraries are missing;
- performs a real `about:blank` smoke launch with `chromium_sandbox=True`;
- returns a closed version/result record to the installer.

The installer remains the transaction coordinator. It swaps the checkout, virtual environment, and staged browser generation together, publishes `state/browser-manager/installation.json`, activates the launcher/service, and restores the prior generation on failure.

The runtime authority remains read-only. It verifies the active Playwright package, Chromium revision/path, control executable, ownership, and modes before Browser Manager launches or reconciles any process. Every live lease holds a shared generation lock; install/update/repair must acquire the exclusive counterpart before swapping a browser generation.

## Version contract

The exact staged Playwright package determines the supported Chromium revision through its packaged `browsers.json`. Playwright and Chromium are accepted as one compatibility generation; an arbitrary system browser or Hermes cache is never selected automatically. The current managed provisioner is explicitly Linux-only, matching the existing executable and process-identity contract; unsupported platforms fail before browser mutation.

The secret-free installation record reports:

- Playwright version;
- Chromium revision and browser version;
- active managed cache;
- readiness status.

## Persistence

- Lease ledger: `~/.dispatch/data/db/browser-manager/browser-manager.sqlite3`
- Per-realm profiles: `~/.dispatch/state/browser-manager/profiles/<realm>`
- Browser installation record and durable generation lock: `~/.dispatch/state/browser-manager/{installation.json,generation.lock}`
- Managed Playwright cache: `~/.dispatch/cache/browser-manager/playwright`
- Future Browser Manager Node/download caches: `~/.dispatch/cache/browser-manager/`
- Temporary per-lease process bookkeeping and locks: `~/.dispatch/run/browser-manager`

Realm profiles are private and persist across ordinary updates and uninstall. Browser binaries and runtime bookkeeping are disposable.

## Concurrency

- Up to 8 browsers run concurrently by default (`DISPATCH_BROWSER_CAPACITY` overrides, 1–64).
- Collectors scraping the same site run in parallel when they use different account profiles; each lease holds its own profile exclusively, so no two leases ever share a browser process or profile state.
- Each realm permits up to `max_concurrent_leases` simultaneous leases (default 4); a collector re-using a busy profile waits or fails with `browser_profile_busy`.
- The Core service loop runs browser maintenance every tick: crashed browsers are reaped and expired leases closed. Long-running collectors should renew their lease (`ManagedLease.renew`) while active; renewal requires the browser process to still match its recorded identity.

## Provider foundation

The closed provider registry currently implements only `managed-playwright`. It reserves non-operational contracts for `persistent-cdp` and `external-cdp` so later collector migrations can add authenticated persistent providers without giving collectors ownership of executables, profiles, endpoints, or locks.

No collector is integrated by this foundation.

## Commands

- `dispatch browser status` reports bounded runtime/provider status and succeeds even when the runtime is not ready.
- `dispatch browser doctor` and `dispatch browser verify` require the active runtime to be ready.
- `dispatch browser reconcile` positively reconciles interrupted/quarantined durable leases before generation mutation.
- `dispatch browser providers` lists implemented and reserved provider contracts.
- `dispatch browser-doctor` remains as a compatibility command.

These commands never include credentials, cookies, CDP URLs, or profile paths.

## Verification

```bash
cd dispatch-core
python3 -m pytest -q -p no:cacheprovider tests/browser_manager tests/command_interface
```

Real install, repair, update, sandbox-launch, and rollback acceptance runs only
on `dispatch-testing` from exact accepted `main` bootstrap bytes. The public
`dev` channel installs those current `main` bytes.
