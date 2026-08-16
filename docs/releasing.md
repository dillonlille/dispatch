# Releasing Dispatch source

Dispatch releases are reviewed Git source tags. A release does not build a wheel catalog, generate an installation manifest, select a runtime tree, or publish installer receipts.

## Branches and authority

- `dev` is the integration channel.
- `main` is the stable source channel.
- A version tag identifies the exact source released to users.
- Merging into `main` does not publish a release or change an installed checkout.

Release publication requires explicit approval in the active conversation. A passing pull request, a GitHub review, or a successful CI run is not release approval.

## Prepare the source

1. Make and test changes on `dev`.
2. Run source safety, all source/Core/installer/Handbook tests, ShellCheck, and `python dispatch-core --help`.
3. Open the reviewed pull request from `dev` to `main`.
4. After the exact commit is approved and merged, resolve the resulting `main` commit.
5. Create the proposed version tag on that exact commit only after receiving separate release approval.

The tag is the release identity. Do not rewrite an existing tag or move a stable checkout by replacing files without first reviewing the target commit.

## Manual release workflow

Run **Publish Dispatch release** with an existing tag in the GitHub Actions UI. The workflow:

1. checks out the requested tag with full history;
2. verifies that the checked-out `HEAD` is exactly both the tag target and current `main`, and that the worktree is clean;
3. installs the development requirements and editable installer/Handbook components;
4. reruns the source safety scan and all source/Core/installer/Handbook tests;
5. Shell-checks the canonical root `install.sh` and smoke-runs `python dispatch-core --help`;
6. creates a published GitHub Release with GitHub-generated notes.

The workflow is manual-only. It has no push, tag, schedule, or automatic-release trigger. It uploads no wheel, archive, manifest, checksum list, or installer file. The GitHub-generated source archives are provider output, not Dispatch-managed installation assets.

## Promotion and rollback

After publication, stable bootstrap promotion is a separate approved deployment operation. Run **Publish Dispatch bootstrap** with the exact approved 40-character `main` commit. The workflow refuses any commit other than current `origin/main`, requires the latest published stable release tag to point to that exact commit, reruns source and shell verification, stages the canonical root `install.sh` only inside CI, and deploys it with defensive headers. A failed release is corrected by a new source commit and new tag; never overwrite an existing tag or silently substitute a different checkout.

For a local or test stable installation, rollback means running `dispatch update --channel stable --version <previous-published-tag>` after reviewing the change. The lifecycle keeps private `config`, `secrets`, `data`, `state`, and `logs` outside the source checkout and restores the previous checkout/environment automatically if activation fails.
