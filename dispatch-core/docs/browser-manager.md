# Browser Manager

Browser Manager provides isolated Playwright Chromium sessions, realm policy, durable leases, crash recovery, and profile locking.

## Runtime ownership

The installer owns browser dependency installation:

- the Python `playwright` package is installed in `~/.dispatch/venv`;
- Chromium is installed by Playwright in `~/.dispatch/cache/browser`;
- required Linux libraries are installed through Playwright's supported dependency command.

Browser Manager does not install or activate browser files. At runtime it asks the installed Playwright package for the Chromium executable, verifies that Chromium is inside the private Dispatch browser cache, verifies the Playwright control executable, and uses those paths for process supervision.

There is no root-owned browser generation, selector, receipt, or parallel activation hierarchy.

## Persistence

- Lease ledger: `~/.dispatch/data/db/browser-manager/browser-manager.sqlite3`
- Per-realm profiles: `~/.dispatch/state/browser-manager/profiles/<realm>`
- Browser cache: `~/.dispatch/cache/browser`
- Temporary process bookkeeping: `~/.dispatch/run/browser-manager`

Realm profiles are private and persist across ordinary updates and uninstall. Browser binaries and runtime bookkeeping are disposable.

## Health

`dispatch browser-doctor` performs bounded package, cache, executable, ownership, and permissions checks. It does not launch a browser. Real browser launch errors are reported when a lease is acquired.

## Verification

```bash
cd dispatch-core
python3 -m pytest -q -p no:cacheprovider tests/browser_manager
```
