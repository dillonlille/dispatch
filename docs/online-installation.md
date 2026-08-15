# Online installation and runtime dependencies

Dispatch installation, upgrades, and repair require an online system. There is no offline bundle, wheelhouse, or offline acceptance contract.

## Current status

The standard-library Phase 5 installer foundation is implemented under `installer/`. It resolves and prepares the private `${HOME}/.dispatch` layout, verifies digest-pinned manifests, strictly downloads approved future-GitHub artifacts, transactionally stages and verifies content-addressed Core releases, and provides non-mutating doctor/verify inspection. Hermes is assumed to be preinstalled; the installer does not inspect or configure it.

Production installation remains fail-closed. `packaging/installation-release-manifest.json` has `ready: false`, and no public `install` orchestration is exposed until approved artifact URLs, sizes, SHA-256 digests, signature authority, browser generation payload, operating-system dependency receipt, and AppArmor/sandbox receipt are complete. The current implementation is a working installation foundation, not clean-machine production acceptance.

After final GitHub publication, the user-facing entry point will be a versioned, digest-pinned terminal command. It will retrieve the reviewed bootstrap and approved release artifacts from the future GitHub repository or separately approved dependency hosts. The command and URLs are not generated now.

The private product-source repository is never an installation payload. It contains only Dispatch-owned built-in plugin source; external plugin source lives in separate repositories. Installation must not use `git clone`, a GitHub source ZIP, archive/zipball/tarball endpoints, raw plugin paths, moving `latest` aliases, or release-asset enumeration. One immutable product manifest identifies the exact mandatory Core closure and the optional built-in plugin catalog. Initial installation downloads only mandatory artifacts; `dispatch setup` downloads only selected, manifest-declared built-in plugin artifacts.

Setup is a separate future phase. Once installation is complete, the `dispatch` CLI will offer **Start setup process** or **Skip for now**. A skipped setup can later be launched with `dispatch setup`. No setup prompts, credentials, account configuration, or provider automation are implemented by this installer phase.

## Core runtime

The immediate distribution remains one package:

```text
dispatch-core==1.0.0
Python >=3.11,<3.14
```

Browser Manager adds the pinned runtime dependency:

```text
playwright==1.62.0
Chromium 151.0.7922.34 (Playwright revision 1234)
```

Authentication also pins:

```text
cryptography==48.0.1
```

The future production release manifest must identify every approved direct and transitive dependency artifact by immutable authority, size, and digest. Index resolution, dependency discovery, and repository-wide downloads are not an acceptable production closure.

The online installer must acquire the approved Chromium artifact for the pinned package version, independently verify it, extract it into an immutable root-owned generation under `/opt/dispatch/browser-runtimes/`, verify required operating-system libraries, and run the bounded sandbox launch probe. Operating-system changes require explicit user approval whenever administrative privileges are needed. Per-user Core releases and mutable Dispatch roots live under `${DISPATCH_HOME}`, defaulting to `${HOME}/.dispatch`.

Production Chromium must run with its sandbox enabled. On Linux hosts that restrict unprivileged user namespaces, the installer must provision and verify an approved AppArmor or Chromium sandbox configuration. Browser Manager fails closed and never silently retries with `--no-sandbox`. A development smoke test may use an explicit test-only executable wrapper on a restricted test host; that does not qualify as production sandbox evidence.

`packaging/browser-runtime-plan.json` records the current development pin and executable proof. It remains explicitly incomplete until approved per-platform artifact URLs, archive sizes, SHA-256 digests, signature policy, and operating-system dependency receipts exist.

After all checks pass, the installer reads three generation-bound receipts only from the fixed root-owned authority `/etc/dispatch/browser-runtime-evidence/<generation>/`: `os-dependencies.json`, `sandbox.json`, and `launch-probe.json`. It validates their closed shapes, ownership, modes, freshness, generation, source-manifest digest, executable digest, and approved sandbox policy; caller-supplied evidence paths or expected evidence digests are not accepted. The installer copies those verified receipts into the immutable generation, derives `installation-evidence.json` from them, binds all four evidence files into the mode-aware `tree-manifest.json`, and writes `installation-receipt.json`. The current synthetic tests create equivalent receipts only beneath temporary isolated authority paths; trusted production receipt producers are not implemented.

Activation atomically replaces only `/etc/dispatch/browser-runtime-active.json`. Rollback requires an explicit retained generation, re-verifies it completely, and uses that same one-selector atomic replacement; there is no independently updated `previous` selector that can diverge during interruption. The future install/upgrade transaction must durably record its explicit rollback target before activation. Browser Manager only reads and validates root-owned authority. It never downloads, extracts, activates, repairs, rolls back, or uninstalls browser assets and never executes from Hermes's or Playwright's shared cache.

The selected-generation Python bootstrap is also intentionally unimplemented. Until a production launcher puts the selected generation's Python root and matching distribution metadata ahead of every ambient Playwright installation, installer doctor reports `browser_launch_composition` and `production_release` as blocked and cannot report overall readiness even when Core and the synthetic browser authority verify independently.

## Download phases

Network downloads are permitted only during:

- initial installation;
- explicit upgrade;
- explicit repair.

Authentication, collection, and uninstallation operations never download Python packages, browsers, drivers, or system dependencies. Missing, incomplete, unsafe, mismatched, or damaged installer authority produces a specific bounded `browser_runtime_*`, `browser_receipt_*`, `browser_executable_mismatch`, `browser_tree_*`, `playwright_*`, or sandbox error. The future installer decides whether an explicit repair operation is appropriate. User-scope uninstall behavior is defined in [`uninstallation.md`](uninstallation.md); root-owned browser removal remains blocked pending the reviewed privileged helper.

## Verification

The installer must fail closed on an unsupported platform, incompatible Python, failed download, unexpected source, version mismatch, or digest mismatch. It stages a release before activation and preserves the previously activated application release for bounded rollback.

Browser profiles and Authentication credentials are private state and are never part of installation artifacts, upgrade staging, package plans, or rollback releases.
