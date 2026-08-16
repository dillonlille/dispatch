# Contributing

Keep changes small, owner-scoped, and verifiable. `main` is the stable source channel; `dev` is the integration channel. Do not treat a branch, tag, or pull request as permission to publish or promote production.

Before proposing a change:

1. read `docs/dispatch-plugin-standard-v1.md` when the change touches the plugin boundary;
2. run `python3 -B scripts/verify-source-export . --json`;
3. install `dispatch-core/requirements-dev.txt` and the editable `installer` and Handbook components;
4. run the affected tests, then the complete source/Core/installer/Handbook test command from the README;
5. shell-check the canonical root `install.sh` and run `python3 dispatch-core --help`;
6. use only synthetic fixtures declared in `synthetic-data.json`;
7. keep credentials, live data, active configuration, browser state, private documents, downloaded dependencies, virtual environments, and deployment state out of Git.

The repository does not accept generated wheels, release manifests, checksum bundles, installation receipts, or build directories as source. Build and test outputs belong outside the checkout or in ignored local directories. A contribution that requires private operational evidence should use a private test package rather than adding that evidence here.

## Pull requests and releases

Pull requests should describe the exact source behavior changed and the checks actually run. Release publication is a separate conversational approval boundary. The manual release workflow accepts an existing tag, verifies the exact tagged source and tests it again, then creates a GitHub Release with generated notes and no uploaded assets.
