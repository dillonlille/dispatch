# GitHub repository controls

This repository is a source repository. GitHub visibility does not make an arbitrary branch an approved stable installation, and GitHub Releases contain source-release notes rather than Dispatch-managed package assets.

## Repository settings

1. Keep the default workflow token permission at **read repository contents**; grant release write permission only to the manual release workflow.
2. Protect `main` and require the `Dispatch CI` checks before merge.
3. Require pull-request review for Core, installer, workflows, path/security contracts, and plugin boundaries.
4. Do not permit force pushes or branch deletion on `main` or `dev`.
5. Require review of changes to `.github/workflows/`, Cloudflare deployment files, and `SECURITY.md`.

Source CI needs no repository, environment, browser, account, or production secrets. It installs test dependencies and editable source components from the checkout.

## Workflow boundaries

`.github/workflows/ci.yml`:

- checks source safety before tests;
- installs `dispatch-core/requirements-dev.txt` and editable installer/Handbook components;
- runs root, Core, installer, and Handbook tests;
- Shell-checks the canonical root `install.sh`;
- smoke-runs `python dispatch-core --help`.

`.github/workflows/release.yml` is manual-only. The operator supplies an existing tag. The job checks out that exact tag, verifies that `HEAD` equals both the tag target and current `main`, reruns source safety and tests, and creates a published GitHub Release with generated notes. It has no automatic trigger and uploads no wheels, manifests, checksums, installer files, or other Dispatch assets.

`.github/workflows/deploy-bootstrap.yml` is also manual-only and uses the protected `production` environment. The operator supplies an exact approved 40-character commit. The job requires that commit to equal current `origin/main`, verifies source safety and the canonical bootstrap, copies root `install.sh` into the Cloudflare asset directory only in the runner workspace, and deploys it. Repository production secrets are available only to this deployment job.

## Distribution and Cloudflare authority

The stable channel is a reviewed Git ref. The development channel is `dev`. Cloudflare-hosted bootstrap files are deployment surfaces and require separate review; they must select the documented channel flow, use defensive headers, and never become a hidden package registry or release generator.
