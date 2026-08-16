# Public source, data, and privacy boundary

This repository is a reviewed source tree. It is intentionally not an installation payload assembled from a package plan or a release manifest. Stable and development installations select an ordinary Git ref and keep mutable user state under `~/.dispatch`.

## Allowed source

- canonical Core, installer, and plugin source;
- public schemas, tests, documentation, and safe templates;
- placeholder-only configuration examples;
- explicitly declared synthetic fixtures whose provenance and digest appear in `synthetic-data.json`;
- CI and manual source-release workflows.

## Never commit

- credentials, private keys, cookies, sessions, browser profiles, or usable tokens;
- active `.env` files, account configuration, destination allowlists, or private hostnames;
- databases, reports, logs, queues, caches, virtual environments, downloaded dependencies, or business documents;
- generated launchers, service state, selectors, receipts, staging directories, or local installation roots;
- wheels, build directories, release bundles, checksum output, or source archives created as local byproducts;
- real employee, customer, payroll, route, safety, handbook, or policy data.

`.gitignore` is a staging defense, not publication approval. Review `git status --short` and `git diff --cached` before every push. The verifier walks the candidate filesystem and rejects unsafe names, symlink escapes, oversized files, binary/business documents, secret-shaped values, personal paths, private identities, and undeclared fixture/data files. It deliberately does not maintain a generated digest inventory of every source path.

## Synthetic data

A fixture under an example, demo, sample, or data-like path must be declared in `synthetic-data.json` with:

- `synthetic: true`;
- `provenance: "created-for-public-tests"`;
- the SHA-256 digest of the exact file bytes.

Synthetic data is for tests and explicit demos only. It is never an implicit configuration source and never becomes private operational data merely because a user installs the repository.

## Behavioral boundary

Read-only commands do not authenticate, browse, collect, deliver, install, or silently create configuration. Integration setup is explicit and private. Absolute paths generated for a local installation belong under the user's private roots, not in canonical source.
