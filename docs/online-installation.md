# Online installation from Git

Dispatch installs from the reviewed Git repository. It does not download Core or installer wheels, release manifests, generated runtime trees, or package catalogs.

## Recommended installation

```bash
curl -fsSL https://dispatch.dillonlille.com/install.sh | bash
```

When a controlling terminal is available, the bootstrap reads its prompts from `/dev/tty` and offers:

1. **Latest Stable** — resolves the newest published, non-draft, non-prerelease GitHub Release, clones that release tag, and leaves the checkout detached at the tag.
2. **Development** — clones current `main` and leaves the checkout attached to `main`.

For automation or any process without a controlling terminal, select the channel explicitly:

```bash
curl -fsSL https://dispatch.dillonlille.com/install.sh | bash -s -- --channel stable
curl -fsSL https://dispatch.dillonlille.com/install.sh | bash -s -- --channel dev
```

An omitted channel fails in noninteractive use. A specific stable version may be selected only when it is an existing published, non-draft, non-prerelease GitHub Release:

```bash
curl -fsSL https://dispatch.dillonlille.com/install.sh | bash -s -- --channel stable --version TAG
```

## Installed layout

The application checkout and dependency environment are separate from durable user data:

```text
~/.dispatch/
├── dispatch/     selected Git checkout
├── venv/         Python dependencies and installed source packages
├── config/       durable private configuration
├── secrets/      durable credentials and encryption material
├── data/         durable application and plugin data
├── state/        durable operational state
├── cache/        disposable cache, including Playwright Chromium
├── logs/         durable operational logs
├── run/          disposable runtime files
└── installation.json
```

Core runs directly from `~/.dispatch/dispatch/dispatch-core`; it is not installed as a wheel. The installer copies validated installer and selected plugin projects into owner-private temporary build state, installs them into the replacement environment with the pinned build backend, and leaves the checkout clean. Browser Manager then derives the exact Chromium revision from staged Playwright, reuses a safe matching generation when available, or installs user-owned Chromium under `~/.dispatch/cache/browser-manager/playwright`.

Browser Manager scans Chromium's shared libraries before requesting elevation. On a prepared host, no browser system command runs. When libraries are missing on a supported Linux host, Playwright's dependency operation may request normal administrator authorization; denial or unavailable privilege fails before activation rather than leaving a nominally ready browser. A bounded sandboxed `about:blank` launch must pass before the checkout, venv, and browser generation activate together.

Sensitive roots and records are owner-only. The public launcher is `${HOME}/.local/bin/dispatch`, and the service is a systemd user service.

## Setup and lifecycle

After installation, the bootstrap offers **Start Setup** or **Skip for Now**. Setup can also be run later:

```text
dispatch setup
dispatch setup --plugin handbook --yes
dispatch setup --plugin companion-bridge --yes
dispatch plugin configure companion-bridge
dispatch auth enroll amazon-operations
dispatch plugin-service enable companion-bridge
```

Selecting a long-running plugin installs its approved dependency closure and
publishes a disabled, exactly generated user-service projection. Companion Bridge
still requires an explicit private configurator run for Slack credentials and
allowlists before its service may be enabled. Installation and plugin selection
never import credentials from another application or operational checkout.

Lifecycle commands are explicit:

```text
dispatch doctor
dispatch update
dispatch repair --yes
dispatch channel stable
dispatch channel dev
dispatch uninstall --plan
```

A development update refuses a dirty checkout, clones current `main` into verified private staging, and replaces the checkout and environment through the same reconciliation path used by installation. A stable update stages the selected published Release tag detached. Channel switching uses that same verified clone path while preserving `config`, `secrets`, `data`, `state`, and `logs`. The channel name remains `dev` for CLI compatibility; no long-lived Git branch named `dev` is required.

## Boundaries

Installation may modify only the user-owned Dispatch layout, its launcher and systemd user unit, and approved Playwright operating-system dependencies. It never installs, modifies, inspects, or removes Hermes. Authentication and external integration setup remain explicit operations; installation does not read or import credentials. Slack tokens, Amazon credentials, browser profiles, allowlists, conversation mappings, and logs remain below the private Dispatch roots and never enter the Git checkout or service-unit environment.

The canonical bootstrap is repository-root `install.sh`; no second tracked installer implementation exists. A repository-linked Vercel project has ordinary Git auto-deployments disabled. Release publication triggers a GitHub-hosted workflow that verifies the exact Release/current-`main` authority, stages only canonical `install.sh` plus `robots.txt`, triggers a project-scoped deploy hook, and requires exact deployed bytes and defensive headers. An explicit workflow dispatch supports reviewed recovery. The bootstrap resolves stable to the latest published immutable Release and development to current `main`. Cloudflare remains the production host only until the verified Vercel custom-domain cutover is complete.
