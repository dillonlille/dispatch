# Contributing

Keep changes small, owner-scoped, and verifiable. `main` is the only long-lived
source branch and must remain installable. Create short-lived branches from
current `main` and target pull requests back to `main`. Do not treat a branch,
tag, pull request, or successful CI run as permission to publish or promote a
stable version.

Before proposing a change:

1. read `docs/dispatch-plugin-standard-v1.md` when the change touches the plugin boundary;
2. run `python3 -B scripts/verify-source-export . --json`;
3. install `dispatch-core/requirements-dev.txt` and the editable `installer`, Handbook, and Companion Bridge components;
4. run the affected tests, then the complete source/Core/installer/built-in-plugin test command from the README;
5. shell-check the canonical root `install.sh` and run `python3 dispatch-core --help`;
6. use only synthetic fixtures declared in `synthetic-data.json`;
7. keep credentials, live data, active configuration, browser state, private documents, downloaded dependencies, virtual environments, and deployment state out of Git.

The repository does not accept generated wheels, release manifests, checksum bundles, installation receipts, or build directories as source. Build and test outputs belong outside the checkout or in ignored local directories. A contribution that requires private operational evidence should use a private test package rather than adding that evidence here.

## Pull requests and releases

Pull requests should describe the exact source behavior changed and the checks
actually run. Accepted task PRs merge directly into `main`; there is no aggregate
integration or release branch. Release publication remains a separate explicit
approval boundary. The manual release workflow accepts an existing approved tag,
verifies the exact tagged source against current `main`, tests it again, and
creates a GitHub Release with generated notes and no uploaded assets.
