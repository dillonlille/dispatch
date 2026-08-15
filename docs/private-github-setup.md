# Private GitHub repository setup

This source tree is prepared for a private GitHub product repository containing Core, the installer, and Dispatch-owned built-in plugins. External plugins use separate repositories. Creating the repository does not make Dispatch installable or production-ready.

## Repository settings

1. Create the repository as **private**.
2. Keep the default workflow token permission at **read repository contents**.
3. Protect the default branch and require the `Dispatch CI` checks before merge.
4. Require pull-request review for changes to Core, installer, packaging policy, workflows, and plugin contracts.
5. Do not permit force pushes or branch deletion on the protected default branch.
6. Create the `core-artifact-review` environment and configure a required reviewer before using the manual artifact workflow.
7. Keep Actions logs and artifacts private. The review artifact retention is intentionally limited to three days.

Do not add repository, environment, or organization secrets for source CI. Source and wheel verification require no credentials, browser sessions, account access, or live service access.

## Workflow boundaries

`.github/workflows/ci.yml`:

- verifies the exact reviewed source scope before dependency installation;
- tests Core, installer, and Handbook independently on GitHub-hosted runners;
- builds only `dispatch-core/` and `installer/`;
- checks the Core wheel against `packaging/runtime-package-plan.json`;
- clean-installs the built wheels and stages the actual Core wheel through the installer admission policy without running live browser/account acceptance.

`.github/workflows/core-artifact.yml` is manual and verification-only. It uploads only:

- one Dispatch Core wheel;
- one Dispatch installer wheel;
- their SHA-256 list.

It does not publish a GitHub Release, source archive, plugin wheel, browser runtime, production manifest, or installation command.

## Deferred distribution authority

Private GitHub release-asset retrieval is not implemented by the product installer. Future authenticated retrieval must use a least-privileged GitHub App or fine-grained token, keep credentials out of URLs/manifests/logs/receipts, and never forward authorization to redirected asset hosts. Until that contract is implemented and accepted on the separate test machine, Actions artifacts are review evidence only.

A license remains required before public source or binary distribution.
