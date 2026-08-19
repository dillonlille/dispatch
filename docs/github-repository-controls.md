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
Release containing the main-tracking installer has been published and its public
bootstrap promoted and accepted.

Agents may merge bounded task PRs into `main` after their required local gates, hosted CI, proportional exact audit, and mergeability checks pass. Merging into `main` does not authorize a tag, GitHub Release, public bootstrap promotion, or production deployment.

Dillon's explicit approval remains required for:

1. the exact `main` commit and proposed production version;
2. creation of the production tag and GitHub Release;
3. production bootstrap promotion for that exact version.

Any byte change invalidates exact release approval and acceptance evidence.

## Repository settings

1. Keep the default workflow token permission at **read repository contents**.
2. Grant release write permission only to the manual release workflow.
3. Protect `main` and require `Source, Core, installer, and built-in plugin tests` with strict up-to-date status.
4. Keep pull requests mandatory while permitting authorized maintainers/agents to merge after the documented gates; repository policy supplies the review/audit boundary rather than an unrelated mandatory GitHub reviewer.
5. Require conversation resolution and linear history.
6. Disable force pushes and deletion of `main`.
7. Keep Cloudflare deployment configuration, credentials, and promotion tooling outside this source repository.

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

There is intentionally no Cloudflare deployment workflow or provider configuration in this repository. Bootstrap promotion is an explicitly approved operator action performed from a private workspace using the exact latest immutable Release. GitHub Actions therefore requires no Cloudflare account identifier or API token.

## Distribution authority

- Development channel: current reviewed `main`, attached and fast-forward-only.
- Stable channel: latest or explicitly selected immutable published Release tag, detached.
- Production promotion: exact approved stable Release bytes through private operator tooling.

Cloudflare-hosted bootstrap files are deployment surfaces, not a package registry or source authority. They must resolve the documented stable/development channel contract and use defensive headers.
