# Contributing

This is a pre-release source candidate. Keep changes small, owner-scoped, and verifiable.

Before proposing a change:

1. read `docs/dispatch-plugin-standard-v1.md`;
2. run `./scripts/verify-source-export`;
3. run the affected owner's canonical test and verify commands;
4. use synthetic fixtures with declarations in `synthetic-data.json`;
5. keep production and test dependencies separate;
6. do not add credentials, live data, active configuration, generated releases, or deployment records.

A contribution that requires private operational evidence should use a private test package rather than adding that evidence to this repository.
