# GitHub repository controls

This repository is a source repository. GitHub visibility does not make an arbitrary branch an approved stable installation, and GitHub Releases contain source-release notes rather than Dispatch-managed package assets.

## Repository settings

1. Keep the default workflow token permission at **read repository contents**; grant release write permission only to the manual release workflow.
2. Protect `main` and require the `Dispatch CI` checks before merge.
3. Require pull-request review for Core, installer, workflows, path/security contracts, and plugin boundaries.
4. Do not permit force pushes or branch deletion on `main` or `dev`.
5. Keep Cloudflare deployment configuration, credentials, and promotion tooling outside this source repository.

Source CI needs no repository, environment, browser, account, or production secrets. It installs test dependencies and editable source components from the checkout.

## Workflow boundaries

`.github/workflows/ci.yml`:

- checks source safety before tests;
- installs `dispatch-core/requirements-dev.txt` and editable installer/Handbook components;
- runs root, Core, installer, and Handbook tests;
- Shell-checks the canonical root `install.sh`;
- smoke-runs `python dispatch-core --help`.

`.github/workflows/release.yml` is manual-only. The operator supplies an existing tag. The job checks out that exact tag, verifies that `HEAD` equals both the tag target and current `main`, reruns source safety and tests, and creates a published GitHub Release with generated notes. It has no automatic trigger and uploads no wheels, manifests, checksums, installer files, or other Dispatch assets.

There is intentionally no Cloudflare deployment workflow or Cloudflare provider configuration in this source repository. Bootstrap promotion is an explicitly approved operator action performed from a private workspace using the exact latest immutable Release. GitHub Actions therefore requires no Cloudflare account identifier or API token.

## Distribution and Cloudflare authority

The stable channel is a reviewed Git ref. The development channel is `dev`. Cloudflare-hosted bootstrap files are deployment surfaces and require separate review; they must select the documented channel flow, use defensive headers, and never become a hidden package registry or release generator.
