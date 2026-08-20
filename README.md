# Dispatch

Dispatch is a per-user Linux application developed and installed directly from Git source. The repository is the source of truth; it is not a wheel catalog, artifact registry, or generated installation manifest.

## Repository structure

```text
dispatch-core/                 directly executable Core source
installer/                     source installer and lifecycle helpers
plugins/handbook/              independently owned built-in example plugin
plugins/companion-bridge/      optional DSP Companion Slack service plugin
plugins/paycom/                source-owned Paycom query and collectors
scripts/                       source-safety checks
.github/workflows/             source CI and manual release workflow
docs/                          installation, path, release, and security contracts
```

Core is a root-level application component, not a plugin. The Handbook proves the
bounded query-plugin boundary. Companion Bridge proves the separately selected,
configured, and enabled long-running service boundary; it is not a Hermes tool.
Paycom proves the separately registered collector-provider boundary while retaining
one bounded read-only plugin handler that a future external adapter can invoke.

## Install channels

- **Latest Stable:** the newest published, non-draft, non-prerelease GitHub Release tag; installed detached at that immutable tag.
- **Development:** the complete current `main` branch; installed attached to `main` and refreshed through a clean staged-clone replacement.

`main` is the only long-lived source branch and the latest reviewed, integrated,
potentially unreleased source. Stable installations never follow mutable `main`;
they resolve only immutable published Release tags. The public channel name remains
`dev` for compatibility even though it tracks `main`.

Recommended installation:

```bash
curl -fsSL https://dispatch.dillonlille.com/install.sh | bash
```

Automated installations must select a channel:

```bash
curl -fsSL https://dispatch.dillonlille.com/install.sh | bash -s -- --channel stable
curl -fsSL https://dispatch.dillonlille.com/install.sh | bash -s -- --channel dev
```

The bootstrap reads interactive prompts from `/dev/tty`, clones into `~/.dispatch/dispatch`, creates `~/.dispatch/venv`, installs dependencies and user-owned Playwright Chromium, starts Core, then offers **Start Setup** or **Skip for Now**.

## Installed layout

```text
~/.dispatch/
├── dispatch/                  selected Git checkout
├── venv/                      per-installation Python environment
├── config/                    durable private configuration
├── secrets/                   durable secret storage
├── data/                      durable user and plugin data
├── state/                     durable operational state
├── cache/                     disposable cache and Chromium
├── logs/                      durable operational logs
├── run/                       disposable runtime files
└── installation.json         selected channel/ref/commit record
```

Core executes directly from `dispatch-core/`; it is not installed as a wheel. `DISPATCH_HOME` may override the default root and must be absolute and non-symlinked. The launcher is normally `${HOME}/.local/bin/dispatch`. Hermes is entirely outside this contract and is never installed, inspected, modified, or removed by Dispatch.

See [`docs/online-installation.md`](docs/online-installation.md), [`docs/path-configuration.md`](docs/path-configuration.md), and [`docs/uninstallation.md`](docs/uninstallation.md).

## Source and privacy boundary

The repository may contain source, tests, schemas, documentation, and explicitly declared synthetic fixtures. It must not contain credentials, active configuration, databases, browser profiles, logs, private documents, business data, downloaded dependencies, virtual environments, or generated deployment state. `scripts/verify-source-export` checks these safety properties directly; `synthetic-data.json` declares allowed fixture provenance and digests.

## Development checks

Install the test dependencies and editable installer/plugin adapters:

```bash
python3 -m pip install -r dispatch-core/requirements-dev.txt
python3 -m pip install --no-deps --editable ./installer --editable ./plugins/handbook --editable ./plugins/paycom
python3 -m pip install --editable ./plugins/companion-bridge
```

Then run:

```bash
python3 -B scripts/verify-source-export . --json
python3 -B -m pytest -q -p no:cacheprovider tests dispatch-core/tests installer/tests plugins/handbook/tests plugins/companion-bridge/tests plugins/paycom/tests
shellcheck install.sh
sh -n install.sh
python3 dispatch-core --help
```

These commands do not perform live account, browser-launch, production-service, release, or deployment acceptance.

## Release and bootstrap publication

A release is a reviewed Git tag on the exact approved `main` commit. The manual release workflow reruns source safety and tests, then creates a published GitHub Release with generated notes and no custom assets.

Bootstrap hosting is automated through the repository-linked Vercel project. Publishing an immutable Release triggers a GitHub-hosted workflow that revalidates the Release tag against current `main`, runs source and shell gates, stages the canonical root `install.sh`, triggers the project-scoped Vercel deployment, and requires exact public bytes plus defensive headers. Ordinary Vercel Git auto-deployments are disabled; an explicit workflow dispatch is available for reviewed recovery. The public bootstrap dynamically resolves stable to the latest published immutable Release and development to current `main`. Real installed-system acceptance remains a separate agent/operator check when requested. Cloudflare continues serving the production domain until the independently verified Vercel custom-domain cutover is complete.

See [`docs/releasing.md`](docs/releasing.md).

## License

Copyright 2026 Dillon Lille.

Dispatch is licensed under the [Apache License 2.0](LICENSE).
