# Releasing Dispatch source

Dispatch has one long-lived source branch: `main`. Task branches merge into `main`; immutable published GitHub Release tags are the stable production authority. A release does not build a wheel catalog, generate an installation manifest, select a runtime tree, or publish installer receipts.

## Branch and channel authority

- `main` is the latest reviewed, integrated, potentially unreleased source.
- Short-lived feature, fix, docs, test, and refactor branches start from current `main` and target pull requests back to `main`.
- The public `dev` installation channel tracks current `main` attached and refreshes through a clean staged-clone replacement; `dev` is a channel name, not a long-lived Git branch.
- The stable channel resolves only published, non-draft, non-prerelease GitHub Release tags and checks them out detached.
- A version tag identifies the exact source released to stable users.
- Merging a task PR into `main` does not publish a release, create a tag, or promote production.

Release publication requires explicit approval for the exact proposed `main` commit and version in the active conversation. A passing pull request, merge, GitHub review, CI run, or development deployment is not release approval.

## Integrate task work

1. Fetch current `main` and create a short-lived task branch from it.
2. Keep the task scoped and add focused tests.
3. Run source safety, affected tests, the complete source/Core/installer/plugin suite, ShellCheck, and `python dispatch-core --help` as applicable.
4. Push the branch and open a pull request directly into `main`.
5. For installer, authentication, browser, destructive, release, or security-sensitive work, freeze the exact PR head and obtain an independent read-only P0–P2 audit.
6. Require hosted CI on the exact current PR head.
7. Merge an accepted task PR into `main` using the repository's linear-history method and delete the task branch.
8. Resolve the resulting exact `main` commit and inspect hosted CI for those bytes.
9. Run exact `dispatch-testing` acceptance when the change affects installation, setup, repair, update, services, browser runtime, or uninstall.

A defect found after merge is corrected through a new task branch and PR from current `main`; never rewrite shared `main` history.

## Prepare an exact release candidate

1. Confirm current `main` is clean, protected, and green.
2. Select the exact `main` commit proposed for release.
3. Record the SHA-256 of `<commit>:install.sh`.
4. Fetch those exact commit-qualified bootstrap bytes on `dispatch-testing`, verify the digest, and run them through the public `dev` channel, which must resolve to that same `main` commit.
5. Run required setup, doctor, health, verify, repair, update, ordinary uninstall, reinstall, and changed-feature acceptance.
6. Confirm durable roots and Hermes remain untouched except for the explicitly tested Dispatch lifecycle.
7. Present the exact commit, version, tests, audit, host acceptance, bootstrap digest, risks, omissions, and production changes for approval.
8. If any byte changes, invalidate the candidate and repeat the exact checks.

There is no `dev → main` release pull request and no permanent release branch. Use a temporary release or hotfix branch only for an exceptional, explicitly reviewed stabilization need; merge any correction back through a normal PR to `main` before tagging.

The former remote `dev` branch can be deleted only after the first stable Release containing the main-tracking installer is published, the current-main bootstrap is promoted publicly, and literal public stable/development acceptance passes. Until then it is a frozen compatibility ref for already-published bootstraps, not an integration target.

## Tag and publish

Only after explicit approval for the exact commit and version:

1. create the approved annotated version tag at the accepted `main` commit;
2. run **Publish Dispatch release** with that existing tag;
3. verify the workflow checks out the tag with full history;
4. require the checked-out `HEAD` to equal both the tag target and current `main`;
5. rerun source safety, all source/Core/installer/plugin tests, ShellCheck, and the Core smoke command;
6. create the published GitHub Release with generated notes.

The manual workflow has no push, tag, schedule, or automatic-release trigger. It uploads no wheel, archive, manifest, checksum list, or installer file. GitHub-generated source archives are provider output, not Dispatch-managed installation assets. Never move, rewrite, or reuse a published stable tag.

## Stable acceptance

After publication:

1. fetch the exact released `install.sh` and verify its digest;
2. install through `--channel stable` on `dispatch-testing` and require resolution to the approved immutable tag;
3. rerun stable doctor, health, verify, service, launcher, setup, and lifecycle checks;
4. exercise the literal public stable installer and require resolution to the same approved immutable tag. If the public bootstrap cannot resolve that release, promote a compatible exact-current-main bootstrap first; stable acceptance is incomplete until the literal public check passes.

A failed release is corrected by a new `main` PR and a new version tag; never overwrite an immutable release.

## Bootstrap promotion

Bootstrap promotion is independent from stable Release publication. It changes only the mutable Cloudflare entry point; the promoted script continues to resolve stable from the latest published immutable Release and development from current `main` at invocation time.

For a bootstrap promotion:

1. select an exact reviewed current-`main` commit and record `<commit>:install.sh` SHA-256;
2. require source CI, any proportional exact audit, and development-channel acceptance for that commit;
3. from the private operator workspace, run `./promote --check <commit>` and require current-main, source, provider-account, dry-run, and cleanup gates to pass;
4. present the exact commit, bootstrap digest, current public digest, check evidence, and intended production change for explicit approval;
5. after approval, revalidate the same current-main commit and run `./promote <commit>`;
6. verify public HTTPS status, defensive headers, byte identity, Cloudflare version attribution, and literal public stable/development behavior on `dispatch-testing`.

Source and stable Release changes require no bootstrap redeployment only when canonical `install.sh` bytes are unchanged. Any bootstrap-byte change requires a new exact-current-main check, approval, promotion, and literal public acceptance. Cloudflare configuration, credentials, and promotion tooling intentionally live outside this repository.

For a reviewed rollback, install an earlier published stable tag explicitly. Private `config`, `secrets`, `data`, `state`, and `logs` remain outside the checkout, while incompatible schema changes must follow their owning component's migration and rollback contract.
