# Dispatch Local Handbook

This public component is a portable, local-only Handbook query example. Its maintained source is the cloned tree under `plugins/handbook`; it contains no private source document, business record, production database, model cache, or active launcher.

## Source and setup

The Handbook is installed from the clone into the same shared virtual environment as Dispatch Core. The editable install keeps source changes immediately visible to the runtime:

```bash
python -m pip install -e plugins/handbook
export DISPATCH_ACTIVE_PLUGINS=handbook
```

Core discovers the installed `dispatch.plugins` entry point named `handbook`. It does not scan plugin directories and does not use `DISPATCH_PLUGIN_PATHS`.

## Capability boundary

- `query` reads an explicitly configured local SQLite FTS index;
- `demo-init` is an explicit operator action that creates an index from declared synthetic JSON;
- no query operation collects data, authenticates, opens a browser, starts a service, or sends messages;
- the Hermes adapter remains unavailable until `DISPATCH_HANDBOOK_DATA_ROOT` declares its resolved private owner root and `DISPATCH_HANDBOOK_INDEX` names a verified index below it;
- the synthetic fixture is not selected automatically.

The owner data convention is `plugins/handbook/data` for local owner-managed data. An operator-provided index may instead live below an explicitly declared private `DISPATCH_HANDBOOK_DATA_ROOT`; it must remain outside source and below that root. The query path is read-only.

## Source checks

Run the maintained source commands from this directory:

```bash
./scripts/test
./scripts/build
./scripts/verify
./scripts/health
```

`build` compiles the source tree in memory. `verify` runs the source-owned metadata, entry-point, envelope, tool-schema, and script-permission audit. None of these commands creates or validates generated plugin artifacts.

## Source-tree demo

Run the demo directly from maintained source:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m dispatch_handbook.cli demo-init \
  --fixture examples/synthetic-handbook.json \
  --target "${TMPDIR:-/tmp}/dispatch-synthetic-handbook.sqlite3"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m dispatch_handbook.cli query \
  --index "${TMPDIR:-/tmp}/dispatch-synthetic-handbook.sqlite3" \
  --action lookup \
  --question 'Where does a paper star with a curled corner go?'
```

The Aster Lantern fixture is wholly invented for public automated tests. Real document ingestion, production indexes, model downloads, and private configuration are outside this source repository and require separate onboarding contracts.

Installed/model-facing configuration must use absolute paths without traversal. The configured index must remain below `DISPATCH_HANDBOOK_DATA_ROOT`; read-only actions never create either path. The explicit demo target is separate operator-selected temporary output.

The optional Hermes adapter is a narrow projection over the same `handle` function. It uses the exact seven-field Dispatch response envelope and a closed action schema.
