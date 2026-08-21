# Harness Integration Contract

Status: accepted design direction. This document defines how Dispatch relates
to an optional agent *harness* — an external runtime that Dispatch can select,
install, and configure during setup. The first supported harness is
[Hermes Agent](https://hermes-agent.nousresearch.com).

## 1. What changed

Previously the source contract stated that Hermes was entirely outside
Dispatch: never installed, inspected, configured, or removed. That boundary is
revised. Dispatch now treats a harness as an **optional, explicitly selected
component**:

- Harness selection is opt-in. The default path (no selection, or `--harness none`)
  is byte-for-byte today's behavior.
- When the user selects a harness, Dispatch may detect, install, configure,
  verify, and record it — within the limits below.
- Uninstalled/absent harness state is not an error; Core-only operation remains
  fully supported.

## 2. Authority model

Dispatch **configures; it does not own** the harness.

| Concern | Dispatch may | Dispatch must never |
|---|---|---|
| Install | Run the harness's official installer with pinned URL + verified digest + explicit flags | Vendor, fork, or reimplement an installer; pipe remote bytes without digest verification |
| Configure | Create one dedicated profile; write profile-scoped config through the harness's own CLI | Edit another profile; hand-edit harness-owned files when a CLI exists |
| Secrets | Declare that credentials are required; defer to the harness's auth flow | Prompt for, store, copy, or transmit provider credentials itself |
| Remove | Remove exactly the artifacts Dispatch created (tracked by manifest) | Remove the harness install, other profiles, or user data |

The harness outlives Dispatch. Even `uninstall --purge` removes only the
Dispatch-created profile after explicit confirmation — never the harness
installation itself.

## 3. Selection catalog

Harnesses are declared in a closed, versioned catalog inside the installer
(mirroring `provider_catalog.py`). v1 contains exactly one entry: `hermes`.
Selection is recorded in `config/harness.json` and mirrored into
`installation.json`. Absent file = no harness selected.

## 4. Setup flow (selected harness)

1. **Detect** — `$HERMES_HOME` layout check, launcher on PATH, bounded
   `--version` probe. Outcomes: ready / absent / unhealthy (fail closed).
2. **Install** — only when absent and only interactively or with an explicit
   opt-in flag: official installer, digest-verified, `--skip-setup --no-skills`
   so the harness's own wizard never competes with Dispatch setup.
3. **Profile** — create one blank dedicated profile (`dispatch-operations`),
   idempotent on re-run.
4. **Model/provider** — closed menu from the harness's supported catalog;
   recommended defaults tagged. Credentials are handled exclusively by the
   harness's auth flow (device-code OAuth), inline when interactive, otherwise
   deferred with the exact follow-up command as a pending requirement.
5. **Reasoning** — closed menu of the harness's supported levels.
6. **Verify** — new doctor/health planes: binary + version floor, profile
   present, config parses with expected values, credential status reported
   (not assumed).

Headless (`--yes`/`--json`) runs never prompt: they either complete from
declared state or return structured pending requirements.

## 5. Plugin projections (follow-up phase)

Plugins carry their own harness artifacts under the canonical layout already
defined in `dispatch-plugin-standard-v1.md` (`SKILL.md`,
`integration/hermes-plugins/`). Activating a plugin will project those
artifacts into the selected harness profile; deactivation removes exactly what
was projected, tracked by a per-plugin ownership manifest. That mechanism is
specified and built separately; this document only reserves the boundary.

## 6. Source-safety impact

No change to the public-source boundary: harness integration adds no
credentials, logs, databases, or generated state to the repository. Installer
code gains network calls to the pinned installer URL only, subject to the same
digest-verification discipline as the Vercel bootstrap.

## 7. Acceptance

Lifecycle acceptance for harness work runs on `dispatch-testing` per the
testing-environment contract, covering: no-selection parity, fresh install,
already-installed detection, unhealthy-state fail-closed, headless pending
requirements, and uninstall preservation of the harness.
