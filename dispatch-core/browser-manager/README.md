# Browser Manager

Status: **implemented lifecycle and installer-receipt enforcement; production runtime provisioning remains installer-owned**.

Browser Manager is the Core-owned boundary for temporary browser processes and persistent private browser profiles. It provides typed leases to Authentication and activated domain plugins; it does not implement site-specific navigation or store credentials.

## Implemented

- immutable Amazon Operations and Paycom Client realm policy;
- bounded authentication, collection, and health-check purposes;
- isolated profiles keyed by realm, plugin, and account alias;
- owner-only data, state, runtime, profile, database, and lock locations;
- versioned SQLite lease state with deterministic timestamps, bounded transitions, and fail-closed schema validation;
- process-safe global, realm, and profile locks hardened against symlink, hard-link, and nonregular-file substitution;
- temporary Chromium startup through Playwright's private debugging pipe, with no listening CDP port;
- tracked Playwright control and Chromium PIDs, Linux process-start identities, installer generation, executable digests, and `/proc/<pid>/exe` identities bound to the exact locked profile marker, with owned descendant cleanup;
- launch timeout, lease deadline, crash detection, cancellation, cleanup, and shutdown;
- startup reconciliation that quarantines PID-less launch windows until a positively identified late browser is cleaned up, while a recorded Playwright control PID closes the larger driver-to-browser crash window;
- durable quarantine that continues consuming profile, realm, and global capacity until cleanup reconciliation succeeds;
- retained in-process ownership and automatic close retry when durable state temporarily fails;
- deadline-bounded context/driver cleanup followed by identity-checked process-tree termination;
- safe status data reporting process **tracking** state, without claiming liveness or disclosing PID, endpoint, or profile;
- a non-launching `dispatch-core browser-doctor` that validates the active installer selector, installation receipt, executable, complete tree manifest, dependency and sandbox assertions, and install-time probe result.

## Runtime dependency

Dispatch Core pins Playwright `1.62.0`, Chromium `151.0.7922.34`, and Playwright Chromium revision `1234` for the initial Ubuntu 24.04 x86_64 target. The future online installer exclusively owns downloading, verifying, extracting, activating, upgrading, repairing, rolling back, and uninstalling Chromium and its operating-system/sandbox dependencies.

Browser Manager is read-only with respect to those assets. Production composition consumes:

```text
/etc/dispatch/browser-runtime-active.json
/opt/dispatch/browser-runtimes/<generation>/installation-receipt.json
/opt/dispatch/browser-runtimes/<generation>/tree-manifest.json
/opt/dispatch/browser-runtimes/<generation>/<approved Playwright Python package>
/opt/dispatch/browser-runtimes/<generation>/<approved Playwright Node and driver CLI>
/opt/dispatch/browser-runtimes/<generation>/<approved Chromium executable>
```

The selector, receipt, manifest, generation directories, Playwright package/control files, and browser files must be root-owned and not group/world writable. Selector and receipt digests, exact package/browser versions, platform, Playwright module/Node/CLI identities, Chromium executable identity, full tree membership, dependency verification, AppArmor policy identity, and the install-time sandbox probe must agree. Browser Manager rechecks the complete generation immediately before every launch and fails closed if any authority is absent or changed.

Production never resolves `playwright.chromium.executable_path`, `PLAYWRIGHT_BROWSERS_PATH`, Hermes's browser cache, or `AGENT_BROWSER_EXECUTABLE_PATH`. Installed Browser Manager classes contain no test runtime/authority constructors or executable injection parameters; test fixtures are assembled only in the unshipped test source.

Security-sensitive manager composition is slot-backed and is not exposed as writable runtime, realm-registry, clock, layout, store, or capacity attributes. The production constructor accepts only shared Dispatch paths, fixes capacity at two browsers, and always reconciles durable state before use. This is API hardening, not a claim that arbitrary Python execution inside Core is sandboxed; Core must expose only the reviewed bounded Browser Manager interface to plugins and Hermes.

### Installer contract (schema version 1)

The active selector contains exactly:

```json
{
  "schema_version": 1,
  "generation": "chromium-151.0.7922.34-r1234",
  "receipt_sha256": "<64 lowercase hexadecimal characters>"
}
```

The selected generation contains `installation-receipt.json` with exactly these fields:

```text
schema_version, generation, installed_at, installer_release,
platform_system, distribution, distribution_version, architecture,
playwright_version,
playwright_module_relative_path, playwright_module_size, playwright_module_sha256,
playwright_driver_executable_relative_path, playwright_driver_executable_size,
playwright_driver_executable_sha256,
playwright_driver_cli_relative_path, playwright_driver_cli_size,
playwright_driver_cli_sha256,
browser_family, browser_version, playwright_revision,
executable_relative_path, executable_size, executable_sha256,
tree_manifest_relative_path, tree_manifest_sha256,
os_dependencies_verified, sandbox_verified, sandbox_policy_id,
launch_probe_passed
```

The initial accepted platform is Linux/Ubuntu/24.04/x86_64; `installer_release` must begin with `dispatch-installer-`; all three verification booleans must be `true`; and `sandbox_policy_id` must be `dispatch-chromium-apparmor-v1`. Unknown or missing fields are rejected.

The tree manifest contains exactly `schema_version` and a non-empty `files` object. Every key is a safe relative regular-file path and every value contains exactly `size` and `sha256`. Constructor, launch, health, doctor, and verify checks compare every declared digest and reject undeclared files, symlinks, hard links, unsafe ownership, and unsafe permissions. The receipt and tree manifest themselves are excluded from the tree member list because the receipt binds the manifest and the selector binds the receipt.

The generation is immutable while Core may use it. The installer must start Core with the selected generation's verified Python root ahead of any other Playwright installation, so Python distribution metadata and the loaded `playwright` module resolve to the receipt-bound files. Installer activation selects a new complete generation atomically; upgrade, rollback, repair, and uninstall must quiesce and restart Core before changing the selected Playwright generation or mutating/removing any generation that a running Browser Manager may have loaded.

## Private layout

The shared path resolver supplies portable roots. Browser Manager creates only its owned locations:

```text
<data>/db/browser-manager/browser-manager.sqlite3
<state>/browser-manager/profiles/<realm>/<plugin>/<account-alias>/
<runtime>/browser-manager/locks/
```

Profiles persist because they contain provider session state. Browser processes and Playwright connections are temporary.

## Internal use

```python
request = BrowserLeaseRequest(
    plugin_id="approved-plugin",
    plugin_release="verified-release",
    realm="amazon-operations",
    purpose=BrowserPurpose.COLLECTION,
)

managed = browser_manager.acquire(request)
authentication.ensure_authenticated(managed.session)
managed.activate()
receipt = plugin.collect(managed.session)
collection_manager.verify_receipt(receipt)
managed.release()
```

Collection Manager remains responsible for queueing, cancellation policy, plugin execution, and publication receipts. Authentication remains responsible for credential retrieval, login forms, challenges, and exact landing-page validation. Browser Manager owns only the browser lease and lifecycle.

## Deliberate boundaries

Browser Manager does not provide arbitrary URLs, flags, JavaScript, shell commands, public CDP access, cookies, credentials, selectors, collection scheduling, retries, or publication logic. `maintain()` is intended to run from the future Core service loop to enforce lease deadlines and detect crashes.

The production constructor and every production launch verify the complete declared generation tree, selected executable, installer receipt, and active selector. This is read-only integrity enforcement; it never repairs or replaces failed files. PID-less launch windows and observed-but-unverified live Playwright control processes remain quarantined rather than being silently treated as cleaned.

Production launch requires Chromium's sandbox. Browser Manager starts the receipt-bound Playwright Node executable under a minimal scrubbed environment, rejects ambient Node/loader/browser authority controls, verifies the loaded Playwright module and driver CLI, and records the Node process identity before launching Chromium. It has no silent sandbox fallback and rejects a started parent process containing `--no-sandbox`, `--disable-setuid-sandbox`, `--disable-seccomp-filter-sandbox`, `--disable-namespace-sandbox`, `--disable-gpu-sandbox`, `--no-zygote`, or `--single-process`. AppArmor and operating-system provisioning still belong exclusively to the future installer.

The current explicit real-browser negative test proves that a development wrapper adding `--no-sandbox` is rejected and cleaned up. A successful production sandbox probe must be performed on a clean supported machine after the future installer provisions the root-owned generation; Browser Manager does not fabricate that acceptance before an installer exists.
