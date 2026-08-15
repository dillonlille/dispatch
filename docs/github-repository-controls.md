# GitHub repository controls

This public source repository contains Dispatch Core, the installer, a separately owned example plugin, packaging and release policy, tests, and required documentation. Repository visibility does not make an unapproved source commit installable or production-ready; immutable release artifacts and the promoted stable bootstrap remain separate authorities.

## Repository settings

1. Keep the default workflow token permission at **read repository contents**.
2. Protect `main` and require the `Dispatch CI` checks before merge.
3. Require pull-request review for Core, installer, packaging policy, workflows, and plugin contracts.
4. Do not permit force pushes or branch deletion on protected long-lived branches.
5. Create the `core-artifact-review` environment and configure a required reviewer before using the manual artifact workflow.
6. Keep short-lived review artifacts limited to the configured retention period.

Source CI does not require repository, environment, or organization secrets. Source and wheel verification require no credentials, browser sessions, account access, or live-service access.

## Workflow boundaries

`.github/workflows/ci.yml`:

- verifies the exact reviewed source scope before dependency installation;
- tests Core, installer, and Handbook independently on GitHub-hosted runners;
- builds only `dispatch-core/` and `installer/`;
- checks Core and installer wheels against their reviewed package plans;
- clean-installs built wheels and exercises installer admission policy without running live browser or account acceptance.

`.github/workflows/core-artifact.yml` is manual and verification-only. It uploads only:

- one Dispatch Core wheel;
- one Dispatch installer wheel;
- their SHA-256 list.

It does not publish a GitHub Release, source archive, plugin wheel, browser runtime, production manifest, or installation command.

`.github/workflows/release.yml` is the separately approved immutable publication boundary. It must run only from the exact approved tag, verify exact acceptance evidence tied to that commit, and publish only the reviewed `install.sh` GitHub Release asset. Stable-channel promotion remains a separate approved operation.

## Distribution authority

The repository includes the Apache-2.0 license. Public source availability is not permission to bypass the release process: production installation remains authorized only by the immutable release, exact versioned artifact identities, acceptance evidence from `dispatch-testing`, and the promoted `https://dispatch.dillonlille.com/install.sh` bootstrap.
