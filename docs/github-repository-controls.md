# GitHub repository controls

This repository has one long-lived source branch: `main`. It is the latest reviewed and integrated source, while immutable published GitHub Release tags are the stable production authority. GitHub visibility does not make an arbitrary branch, commit, tag, or pull request an approved stable installation.

## Branch model

- Protect `main` as the sole long-lived branch.
- Create short-lived task branches from current `main` and target pull requests directly to `main`.
- Require the exact `Dispatch CI` check before merge.
- Require conversation resolution and prohibit force pushes and deletion of `main`.
- Keep linear history through squash or rebase merges; task branches are deleted after merge.
- Do not maintain a permanent `dev` or release branch.
- The installer channel named `dev` tracks current `main`; stable installs use only immutable published Release tags.

During the one-time migration, the former remote `dev` branch may remain frozen
as a compatibility ref for already-published installers that still fetch it.
No new PR may target it. Delete that compatibility branch only after a stable
Release containing the main-tracking installer has been published, the Vercel
custom-domain cutover is complete, and literal public stable and development
acceptance has passed.

Agents may merge bounded task PRs into `main` after their required local gates, hosted CI, proportional exact audit, and mergeability checks pass. Merging into `main` does not authorize a tag or GitHub Release.

Dillon's explicit approval remains required for:

1. the exact `main` commit and proposed production version for a stable release;
2. creation of the production tag and GitHub Release.

Any byte change invalidates exact release approval and acceptance evidence.

## Repository settings

1. Keep the default workflow token permission at **read repository contents**.
2. Grant release write permission only to the manual release workflow.
3. Protect `main` and require `Source, Core, installer, and built-in plugin tests` with strict up-to-date status.
4. Keep pull requests mandatory while permitting authorized maintainers/agents to merge after the documented gates; repository policy supplies the review/audit boundary rather than an unrelated mandatory GitHub reviewer.
5. Require conversation resolution and linear history.
6. Disable force pushes and deletion of `main`.
7. Keep the public Vercel build contract and GitHub-hosted publication workflow in source. Store only the project-scoped deploy hook as a GitHub Actions secret; project and public URL identifiers are non-secret repository variables.

Source CI needs no repository, environment, browser, account, or production secrets. It installs test dependencies and editable source components from the checkout.

## Workflow boundaries

`.github/workflows/ci.yml`:

- runs on pull requests targeting `main`, pushes to `main`, and explicit manual dispatch;
- checks source safety before tools can create generated metadata;
- installs the pinned Core development requirements and editable installer/plugins;
- runs root, Core, installer, and built-in plugin tests;
- Shell-checks the canonical root `install.sh`;
- smoke-runs `python dispatch-core --help`.

`.github/workflows/release.yml` is manual-only. The operator supplies an existing approved tag. The job checks out that exact tag, verifies that `HEAD` equals both the tag target and current `main`, reruns source safety and tests, and creates a published GitHub Release with generated notes. It has no automatic trigger and uploads no wheels, manifests, checksums, installer files, or other Dispatch assets.

`.github/workflows/publish-bootstrap-vercel.yml` runs on published Releases and explicit dispatch. It verifies Release/tag/current-`main` identity, source and shell safety, exact staging, deploy-hook job creation, deployed byte identity, defensive headers, and a final current-`main` gate. Ordinary Vercel Git auto-deployments are disabled. The workflow does not receive a general Vercel API token and cannot claim real installed-system acceptance.

## Distribution authority

- Development channel: current reviewed `main`, attached and refreshed through verified staged-clone replacement.
- Stable channel: latest or explicitly selected immutable published Release tag, detached.
- Production bootstrap hosting: release-triggered Vercel publication of canonical `install.sh`, which resolves stable Releases and development `main` dynamically at install time.

Vercel-hosted bootstrap files are deployment surfaces, not a package registry or source authority. They must resolve the documented stable/development channel contract, publish the exact source-commit marker, and use defensive headers. `dispatch.dillonlille.com` is DNS-only at Cloudflare and serves the Vercel project directly.
