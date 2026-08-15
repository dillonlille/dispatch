# Online installation and runtime dependencies

Dispatch installation, upgrades, and repair require an online system. There is no offline bundle, wheelhouse, or offline acceptance contract.

## Current status

The standard-library installer foundation is implemented under `installer/`. It resolves and prepares the private `${HOME}/.dispatch` layout, verifies digest-pinned product manifests, transactionally stages and verifies content-addressed Core releases, publishes an internal stable launcher plus a receipt-owned `${HOME}/.local/bin/dispatch` command, and provides non-mutating doctor/verify inspection. It refuses to overwrite an unrelated command at that path. Hermes is assumed to be preinstalled; the installer does not inspect or configure it.

The reviewed source manifest remains a fail-closed draft with `ready: false`; production finalization validates acceptance evidence, inserts the exact Core and installer artifact identities, and emits a separate immutable ready manifest. Version `0.0.2` is Core-only: its built-in plugin catalog is empty, while the generic installed-plugin contract remains available for a later release. User-service activation, Core-only setup, command publication and recovery, and clean-machine lifecycle acceptance are complete.

The public domain serves the digest-pinned `0.0.2` bootstrap generated from `installer/deploy/cloudflare/install.sh.in`. `scripts/render-bootstrap` renders it only from a ready manifest and the exact installer wheel named by that manifest. The rendered script pins the manifest and installer bytes, then hands Core installation to the manifest-authorized installer.

The private product-source repository is never an installation payload. It contains only Dispatch-owned built-in plugin source; external plugin source lives in separate repositories. Installation must not use `git clone`, a GitHub source ZIP, archive/zipball/tarball endpoints, raw plugin paths, moving `latest` aliases, or release-asset enumeration. One immutable product manifest identifies the exact mandatory Core closure and the optional built-in plugin catalog. Core has no mandatory third-party runtime dependency. Initial installation downloads only the installer and Core artifacts. `dispatch setup` downloads only selected, manifest-declared built-in plugin artifacts, verifies their exact dependency declaration and wheel contents, activates immutable plugin releases, and records a non-secret setup receipt. A plugin with an unimplemented capability or dependency closure fails closed rather than resolving it dynamically.

Setup is a separate explicit phase. Once Core is active and setup-independent health succeeds, the bootstrap offers **Start Setup** or **Skip for Now**. `dispatch setup` supports interactive selection, repeatable `--plugin ID` selection, `--list`, and non-interactive Core-only completion with `--yes`. The `0.0.2` catalog is empty, so setup completes without downloading optional artifacts. Skipping leaves health at `setup_incomplete`; explicit Core-only setup changes health to `ready`. In future releases with selected plugins, Core requires every selected `dispatch.plugins` entry point to load and report ready before overall health becomes `ready`. Setup never records credentials, account configuration, MFA values, CAPTCHA answers, or provider secrets.

## Core runtime

The immediate distribution remains one package:

```text
dispatch-core==1.0.0
Python >=3.11,<3.14
```

Browser Manager adds the setup-time `browser` capability extra:

```text
playwright==1.62.0
Chromium 151.0.7922.34 (Playwright revision 1234)
```

Authentication adds the setup-time `authentication` capability extra:

```text
cryptography==50.0.0
```

The future production release manifest must identify every approved direct and transitive dependency artifact by immutable authority, size, and digest. Index resolution, dependency discovery, and repository-wide downloads are not an acceptable production closure.

The online installer must acquire the approved Chromium artifact for the pinned package version, independently verify it, extract it into an immutable root-owned generation under `/opt/dispatch/browser-runtimes/`, verify required operating-system libraries, and run the bounded sandbox launch probe. Operating-system changes require explicit user approval whenever administrative privileges are needed. Per-user Core releases and mutable Dispatch roots live under `${DISPATCH_HOME}`, defaulting to `${HOME}/.dispatch`.

Production Chromium must run with its sandbox enabled. On Linux hosts that restrict unprivileged user namespaces, the installer must provision and verify an approved AppArmor or Chromium sandbox configuration. Browser Manager fails closed and never silently retries with `--no-sandbox`. A development smoke test may use an explicit test-only executable wrapper on a restricted test host; that does not qualify as production sandbox evidence.

`packaging/browser-runtime-plan.json` records the current development pin and executable proof. It remains explicitly incomplete until approved per-platform artifact URLs, archive sizes, SHA-256 digests, signature policy, and operating-system dependency receipts exist.

After all checks pass, the installer reads three generation-bound receipts only from the fixed root-owned authority `/etc/dispatch/browser-runtime-evidence/<generation>/`: `os-dependencies.json`, `sandbox.json`, and `launch-probe.json`. It validates their closed shapes, ownership, modes, freshness, generation, source-manifest digest, executable digest, and approved sandbox policy; caller-supplied evidence paths or expected evidence digests are not accepted. The installer copies those verified receipts into the immutable generation, derives `installation-evidence.json` from them, binds all four evidence files into the mode-aware `tree-manifest.json`, and writes `installation-receipt.json`. The current synthetic tests create equivalent receipts only beneath temporary isolated authority paths; trusted production receipt producers are not implemented.

Activation atomically replaces only `/etc/dispatch/browser-runtime-active.json`. Rollback requires an explicit retained generation, re-verifies it completely, and uses that same one-selector atomic replacement; there is no independently updated `previous` selector that can diverge during interruption. The future install/upgrade transaction must durably record its explicit rollback target before activation. Browser Manager only reads and validates root-owned authority. It never downloads, extracts, activates, repairs, rolls back, or uninstalls browser assets and never executes from Hermes's or Playwright's shared cache.

The selected-generation Python bootstrap is also intentionally unimplemented. A release that declares a browser runtime remains blocked until a production launcher puts the selected generation's Python root and matching distribution metadata ahead of every ambient Playwright installation. This does not block the Core-only `0.0.2` release; installer doctor treats browser launch composition as not applicable when the installed release does not require a browser.

## Download phases

Network downloads are permitted only during:

- initial installation;
- explicit upgrade;
- explicit repair.

Authentication, collection, and uninstallation operations never download Python packages, browsers, drivers, or system dependencies. Missing, incomplete, unsafe, mismatched, or damaged installer authority produces a specific bounded error. Re-running the exact `0.0.2` bootstrap is the supported repair path: the installer re-verifies and reuses the immutable Core release, republishes the internal and public launchers plus manifest authority, retries service activation, and completes the durable install transaction. User-scope uninstall behavior is defined in [`uninstallation.md`](uninstallation.md); root-owned browser removal remains blocked pending the reviewed privileged helper.

## Verification

The installer must fail closed on an unsupported platform, incompatible Python, failed download, unexpected source, version mismatch, or digest mismatch. It stages a release before activation and preserves the previously activated application release for bounded rollback.

Browser profiles and Authentication credentials are private state and are never part of installation artifacts, upgrade staging, package plans, or rollback releases.
