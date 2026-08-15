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

The package remains a deferred plugin source candidate. It is not part of the immediate Core-only online installer, readiness contract, or clean-machine acceptance. A later reviewed plugin package may depend on `dispatch-core==1.0.0`; runtime installation must continue to exclude synthetic fixtures and tests. See [`../../docs/online-installation.md`](../../docs/online-installation.md).
