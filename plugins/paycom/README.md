# Dispatch Paycom

Source-owned Paycom query and collection plugin for Dispatch. The maintained code lives in this checkout under `plugins/paycom`; private data and browser/authentication state remain outside Git.

This migration does **not** edit, disable, remove, install over, or otherwise change the currently deployed legacy Paycom plugin. The new source remains separately selected through Dispatch setup until an explicit, tested cutover.

## Capabilities

The Core plugin entry point exposes three closed, read-only actions:

- `health`
- `meal_comparison`
- `audit`

Every response uses the exact seven-field Dispatch envelope. Reads never authenticate, browse, collect, mutate data, or deliver a report. This stable Core boundary can later be wrapped by a separately reviewed Hermes plugin through `dispatch plugin invoke paycom`; Dispatch itself does not install or modify Hermes.

The same distribution publishes one `dispatch.collectors` provider with two non-model-facing collectors:

- `paycom-roster`
- `paycom-timecards`

Core owns durable queueing, worker isolation, retries, execution deadlines, the `paycom-client` Browser Manager lease, and the selected named authentication profile. Paycom owns only site-specific navigation, strict extraction, artifact staging, domain validation, transactional SQLite publication, and publication receipts. Selecting the plugin does not schedule collection or run a collector.

## Private roots

All paths derive from Core `DispatchPaths`:

```text
${DISPATCH_DATA_ROOT}/paycom/
├── roster.sqlite3
├── timecards.sqlite3
└── identity.sqlite3

${DISPATCH_DATA_ROOT}/meal-break-gaps/
└── meal-break-gaps.sqlite3
```

Transient collector staging remains below the Paycom owner root and is removed after verified publication. Credentials live only in Core's encrypted `paycom-client` authentication store; cookies and browser profiles remain Browser Manager state. No credential, cookie, browser profile, employee row, production response, generated release, activation record, or receipt belongs in this repository.

Owner directories are private (`0700`) and database files are private regular files (`0600`). Query readers use immutable SQLite handles, reject a live non-empty WAL, and verify the database family did not change while it was open.

## Operator examples

Install/select from the cloned Dispatch environment:

```bash
dispatch setup --plugin paycom --yes
dispatch auth add payroll --provider paycom
dispatch plugin health paycom
dispatch plugin invoke paycom --request '{"action":"meal_comparison","relative_scope":"today"}'
```

Queueing is explicit and does not execute inline:

```bash
dispatch collection submit paycom-roster \
  --parameters '{"target":"2026-08-19","replace":false}'

dispatch collection submit paycom-timecards \
  --parameters '{"period_end":"2026-08-22","replace":false}'
```

The profile selected during `dispatch setup` is used automatically. `--account`
remains a compatibility option for legacy callers. Profile resolution is
Core-owned and happens before a browser lease or collector work;
credentials never enter collector context or JSON output. Query and health
actions remain read-only and do not authenticate.

No recurring Paycom schedule is installed by this source migration. The current operational scheduler remains authoritative until a separately reviewed timezone-aware schedule migration and cutover.

## Source checks

Run from this directory:

```bash
./scripts/test
./scripts/build
./scripts/verify
./scripts/health
```

These checks are fixture-only and non-mutating. They do not open Paycom, authenticate, launch a browser, collect production data, modify Hermes, or run an installation lifecycle.

## Migration and acceptance status

The source package, bounded query boundary, generic collector registration, and synthetic collector/storage tests are present. Live Paycom extraction, Core authentication parity, legacy-database import, timezone-aware recurring schedules, and end-to-end installed acceptance remain separate gates. Until those gates pass on `dispatch-testing`, the currently deployed Paycom plugin remains untouched and authoritative.
