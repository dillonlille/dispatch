# Dispatch Local Handbook

This public component is a portable, local-only Handbook query example. It contains no private source document, business record, production database, model cache, or active launcher.

## Capability boundary

- `query` reads an already configured local SQLite FTS index;
- `demo-init` is an explicit operator action that creates an index from declared synthetic JSON;
- no query operation collects data, authenticates, opens a browser, starts a service, or sends messages;
- the Hermes adapter remains unavailable until `DISPATCH_HANDBOOK_DATA_ROOT` declares its resolved private owner root and `DISPATCH_HANDBOOK_INDEX` names a verified index below it;
- the synthetic fixture is not included in runtime releases and is never selected automatically.

## Source-tree demo

Run the demo directly from maintained source without installing dependencies or writing package metadata:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m dispatch_handbook.cli demo-init \
  --fixture examples/synthetic-handbook.json \
  --target "${TMPDIR:-/tmp}/dispatch-synthetic-handbook.sqlite3"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python3 -B -m dispatch_handbook.cli query \
  --index "${TMPDIR:-/tmp}/dispatch-synthetic-handbook.sqlite3" \
  --action lookup \
  --question 'Where does a paper star with a curled corner go?'
```

The Aster Lantern fixture is wholly invented for public automated tests. Real document ingestion, production indexes, model downloads, and private configuration are intentionally outside this source repository and will require separate onboarding contracts.

Installed/model-facing configuration must use absolute paths without traversal. The configured index must remain below `DISPATCH_HANDBOOK_DATA_ROOT`; read-only actions never create either path. The explicit demo target is separate operator-selected temporary output.

The wheel publishes the standard `dispatch.plugins` entry point named `handbook`. After installer-approved activation, Core discovers it without a generated registry or plugin-specific Core code:

```bash
dispatch plugin list
dispatch plugin health handbook
dispatch plugin invoke handbook --request '{"action":"lookup","question":"Where does the paper star go?"}'
```

Lookup readiness still requires an operator-provided index. Runtime installation excludes synthetic fixtures and tests, and setup does not modify Hermes. The optional Hermes adapter remains a narrow separate projection over the same `handle` function. See [`../../docs/online-installation.md`](../../docs/online-installation.md).
