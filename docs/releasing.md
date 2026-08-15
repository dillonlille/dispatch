# Preparing and publishing a Dispatch version

Dispatch separates source integration, version preparation, merging, and production publication.

- `dev` contains active development and release preparation.
- `main` contains production-ready source.
- A published immutable tag and the stable bootstrap determine what users install.
- Merging into `main` does not publish or install a version.

## Authority boundaries

Dillon gives approval directly in the active conversation. GitHub review state is not release authority.

These are separate approvals:

1. approval of proposed product and component versions;
2. approval to merge the exact version-preparation PR head;
3. approval to tag, publish, and promote the exact accepted `main` commit and version.

A later source change invalidates approval for the earlier head.

## 1. Preview a version plan

Run from a clean `dev` branch with the full published tag history available:

```bash
./scripts/prepare-release \
  --product-version X.Y.Z \
  --installer-version X.Y.Z \
  --core-version X.Y.Z
```

The default is preview-only. It compares the working source with the product version named by the tracked production bootstrap, resolves that immutable tag, and reports which component sources changed.

Rules enforced by the command:

- the product version must be newer than the published product;
- a changed component must receive a newer component version;
- an unchanged component must retain its published component version;
- an existing product tag cannot be reused;
- the production bootstrap is not changed.

Use `--json` for one machine-readable result.

## 2. Apply approved version metadata

After the proposed versions are approved:

```bash
./scripts/prepare-release \
  --product-version X.Y.Z \
  --installer-version X.Y.Z \
  --core-version X.Y.Z \
  --apply
```

Application is allowed only on `dev` with a clean worktree. The command updates canonical product, installer, and Core identities; refreshes package-plan hashes and sizes; rebuilds the draft manifest's Core file declarations; clears all draft artifact identities; keeps `ready` false; and refreshes the exact public-source scope.

It does not commit, push, tag, build, publish, deploy, or modify the production bootstrap.

## 3. Verify static release readiness

```bash
./scripts/verify-release-readiness --require-clean
```

Run this after committing the prepared metadata. It verifies:

- product and component version consistency;
- changed-component version policy against the published tag;
- package-plan source hashes and sizes;
- Core runtime-plan source hashes;
- draft manifest/package-plan consistency;
- empty artifact identities and `ready: false` before finalization;
- absence of mutable production-approval state in source metadata;
- worktree cleanliness when requested.

Static readiness is not lifecycle acceptance and is not permission to publish.

## 4. Build and accept the exact candidate

Build deterministically from the exact pushed `dev` commit. Keep outputs outside the repository. Finalize candidate artifacts with `scripts/finalize-installation-release --acceptance-candidate`, publish them only under an immutable commit-qualified development path, and run all real install/setup/service/uninstall acceptance on `dispatch-testing`.

Do not add candidate wheels, manifests, checksums, acceptance evidence, release evidence, logs, databases, or downloaded runtime material to Git.

## 5. Merge approval

Open a `dev` to `main` PR and report:

- exact PR head;
- proposed product and component versions;
- local and hosted tests;
- exact candidate artifact identities;
- `dispatch-testing` acceptance;
- risks and deferred work.

Ask Dillon in the active conversation whether to merge that exact PR head. Merge approval does not authorize publication.

## 6. Post-merge acceptance

Resolve the resulting `main` commit and verify that its tree contains the approved source. Rebuild deterministically from that exact commit and repeat final acceptance on `dispatch-testing`. Production finalization requires acceptance evidence whose source commit and product version exactly match the release source.

`production_install_ready` is intentionally not stored in source metadata. Exact acceptance evidence is the production-readiness authority; a mutable boolean could become stale after any source change.

## 7. Release and promotion approval

Before publishing, report:

- exact accepted `main` commit;
- exact product and component versions;
- final manifest, installer, and Core digests;
- post-merge CI and acceptance results;
- current production version and proposed production changes.

Ask Dillon in the active conversation whether to tag, publish, and promote that exact version.

Only after approval:

1. create the immutable product tag on the accepted `main` commit;
2. run the tag-qualified release workflow;
3. publish immutable versioned manifest and component artifacts;
4. verify all hosted sizes and SHA-256 digests;
5. create the GitHub Release with only its reviewed `install.sh` asset;
6. update the stable bootstrap only after the immutable release exists;
7. verify the literal stable installation on `dispatch-testing`.

Never overwrite a published version path or silently substitute a different commit, version, or artifact.
