# Release and verification workflow

## Build direction

```text
src + package metadata + build script
        -> staged candidate
        -> deterministic member inventory
        -> immutable runtime/releases/<digest>
```

Do not include logs, locks, caches, bytecode, SQLite sidecars, browser state, editable virtualenv metadata, or temporary files in release identity.

## Activation

One activation record declares:

- active release;
- rollback release;
- launcher and launcher-manifest identities;
- query/collector/service interfaces;
- installed profile projections;
- service units when applicable.

For owners with multiple independently released components, use one authority with an `interfaces` object. Every interface records its active runtime and SHA-256, exact rollback path/release/SHA-256, launcher manifest, and manager or service projections. Verify old rollback directories by explicit identity rather than assuming their names are digest prefixes.

A single-file interface records the launcher/artifact path and its digest. A directory-bundle interface records the release directory, release identity, and digest of its sealed member manifest; rollback uses the same shape. Directory identity is the directory name plus a verified member manifest—not a hash of the directory node itself. Launchers remain separate hash-bound selectors into the bundle.

When projection directories are sealed read-only, activation may temporarily add owner write permission only around atomic leaf replacement. Restore the restrictive directory mode in `finally` and write the root activation authority after all hash-bound leaves. A failed or interrupted activation must leave either the old converged projection or secondary files that fail closed until the authority is repaired.

`runtime/current`, manager executables, adjacent manager records, Hermes launcher manifests, installed profile links/copies, and effective service units must agree with it. Matching hashes are insufficient when the referenced JSON has the wrong semantic schema.

## Required proof gates

1. Source tests pass and discover a nonzero count.
2. Build is deterministic for the same inputs.
3. Release member set, modes, sizes, and hashes verify.
4. Release contains no volatile members.
5. Standard health command is read-only and succeeds honestly.
6. Hermes registration and availability succeed when applicable.
7. Every advertised action is exercised through the adapter.
8. Installed projections match canonical integration bytes.
9. All activation selectors converge.
10. Rollback remains present and verified.

## Readiness reporting

Report separately:

- registration;
- runtime integrity;
- query;
- data/domain audit;
- freshness;
- collector;
- authentication;
- delivery;
- service health.

A safe fail-closed collector can still be unavailable. A healthy query plane can coexist with a degraded producer. State both facts.
