# Source test, verification, and health workflow

This reference keeps the historical filename for links, but the workflow is source-owned. There is no plugin artifact builder, immutable runtime copy, activation record, generation selector, or release receipt.

## Verification order

From the plugin clone:

```bash
./scripts/test
./scripts/build
./scripts/verify
./scripts/health
python3 dispatch-core/plugin_policy.py .
```

The commands must run against the maintained clone and use the plugin's own dependencies. `build` may compile or import source in memory; it must not publish files outside the clone. `health` is read-only and reports configuration, data, freshness, and capability readiness honestly.

## Shared-environment setup

Install the clone editable into the shared Dispatch virtual environment:

```bash
python -m pip install -e plugins/<owner>
DISPATCH_ACTIVE_PLUGINS=<owner> dispatch plugin health <owner>
```

Core discovers the installed `dispatch.plugins` entry point and selects only IDs named by `DISPATCH_ACTIVE_PLUGINS`. Do not configure source paths or repository scans. `DISPATCH_PLUGIN_PATHS` is obsolete.

## Proof gates

1. Tests discover and pass a nonzero test count.
2. Source syntax/build checks pass without generating a runtime copy.
3. pyproject identity, capabilities, and entry point are valid.
4. An optional root manifest ID matches pyproject metadata.
5. Lifecycle scripts are executable and not group/world writable.
6. The entry-point health response uses the exact seven-field envelope.
7. Every Hermes projection registers exactly one tool with a closed action schema.
8. Invalid input fails closed and health remains a valid readiness response.
9. Owner data stays outside source and below its documented private root.
10. The selected editable install responds through Core with the expected plugin ID.

## Readiness reporting

Report independently:

- registration;
- runtime integrity;
- query;
- data/domain audit;
- freshness;
- collector;
- authentication;
- delivery;
- overall status.

A healthy query plane may coexist with an unavailable collector. State both facts and never claim readiness from metadata alone.
