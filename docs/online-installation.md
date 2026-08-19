# Online installation from Git

Dispatch installs from the reviewed Git repository. It does not download Core or installer wheels, release manifests, generated runtime trees, or package catalogs.

## Recommended installation

```bash
curl -fsSL https://dispatch.dillonlille.com/install.sh | bash
```

When a controlling terminal is available, the bootstrap reads its prompts from `/dev/tty` and offers:

1. **Latest Stable** — resolves the newest published, non-draft, non-prerelease GitHub Release, clones that release tag, and leaves the checkout detached at the tag.
2. **Dev Branch** — clones the complete `dev` branch and leaves the checkout attached to `dev`.

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
├── venv/         Python dependencies and editable installer/plugin adapters
├── config/       durable private configuration
├── secrets/      durable credentials and encryption material
├── data/         durable application and plugin data
├── state/        durable operational state
├── cache/        disposable cache, including Playwright Chromium
├── logs/         durable operational logs
├── run/          disposable runtime files
└── installation.json
```

Core runs directly from `~/.dispatch/dispatch/dispatch-core`; it is not installed as a wheel. The installer installs the pinned Core dependencies and selected built-in plugin dependencies. Browser Manager then derives the exact Chromium revision from staged Playwright, reuses a safe matching generation when available, or installs user-owned Chromium under `~/.dispatch/cache/browser-manager/playwright`.

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
publishes a disabled, receipt-owned user-service projection. Companion Bridge
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

A development update refuses a dirty checkout, fetches `origin/dev`, and merges with `--ff-only`. A stable update resolves a published release tag and checks it out detached. Channel switching uses a newly verified clone while preserving `config`, `secrets`, `data`, `state`, and `logs`.

## Boundaries

Installation may modify only the user-owned Dispatch layout, its launcher and systemd user unit, and approved Playwright operating-system dependencies. It never installs, modifies, inspects, or removes Hermes. Authentication and external integration setup remain explicit operations; installation does not read or import credentials. Slack tokens, Amazon credentials, browser profiles, allowlists, conversation mappings, and logs remain below the private Dispatch roots and never enter the Git checkout or service-unit environment.

The canonical bootstrap is the repository-root `install.sh`. Explicitly approved private operator tooling outside this repository fetches that exact file from the latest immutable Release, stages it transiently for Cloudflare, and verifies the public bytes and headers; no second tracked bootstrap implementation exists.
