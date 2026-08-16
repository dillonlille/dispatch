# Dispatch Core

Dispatch Core is the directly executable application inside the Dispatch repository. It is not built or installed as a wheel.

## Layout

```text
dispatch-core/
├── __main__.py
├── authentication/
├── browser_manager/
├── collection_manager/
├── command_interface/
├── health/
├── paths/
├── plugin_runtime.py
├── plugin_policy.py
├── tests/
├── docs/
├── scripts/
├── requirements.txt
└── requirements-dev.txt
```

There is deliberately no `src/` directory and no nested `dispatch_core` package. Imports inside Core use the feature names directly, such as `from paths import DispatchPaths`.

## Run from source

From the repository root:

```bash
python3 dispatch-core --help
python3 dispatch-core health
```

An installed Dispatch launcher uses `~/.dispatch/venv/bin/python` and the cloned application at `~/.dispatch/dispatch/dispatch-core`.

## Dependencies

Runtime dependencies are pinned in `requirements.txt`. Test and development dependencies are pinned in `requirements-dev.txt`.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r dispatch-core/requirements-dev.txt
```

## Verification

```bash
dispatch-core/scripts/test
dispatch-core/scripts/verify
dispatch-core/scripts/health
```

Core resolves durable user data outside the checkout under `~/.dispatch/`. Code updates replace the cloned source and virtual environment without replacing configuration, secrets, databases, browser profiles, or logs.
