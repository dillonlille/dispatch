# Installation contract

Dispatch turns a reviewed Git source ref into a per-user installation. Stable and development channels are separate checkouts; the source tree is not converted into a wheel or authorized by a generated product manifest.

## Ownership model

- user-owned Dispatch clone, environment, and mutable roots live under `${DISPATCH_HOME}`, default `${HOME}/.dispatch`;
- transient sockets and locks live under `${DISPATCH_RUNTIME_ROOT}`;
- private configuration and durable data remain separate from source;
- system-owned browser or sandbox resources, when a future integration needs them, are outside the user source tree and require their own reviewed lifecycle boundary.

Hermes is a user-supplied prerequisite. Dispatch does not install, configure, inspect, create profiles for, or mutate Hermes.

## Installation phases

1. choose an immutable published tag for stable or current `main` for development;
2. clone that exact Git ref into private staging and promote it to `${DISPATCH_HOME}/dispatch`;
3. create `${DISPATCH_HOME}/venv`, install reviewed runtime requirements, and install validated installer/plugin packages from private source copies without mutating the checkout;
4. create the private `config`, `secrets`, `data`, `state`, `cache`, `logs`, and `run` roots with restrictive ownership and modes;
5. publish `${HOME}/.local/bin/dispatch` pointing at the selected checkout and environment;
6. run non-mutating help, health, and verification checks;
7. perform integration setup only after the operator explicitly asks for it.

The installation process must not enumerate release assets, infer packages from a source archive, download a wheel catalog, or import private configuration from the repository. Updates reconcile through the same verified staged-clone path as installation; they do not mutate the active checkout in place or select generated release trees.

## Safety requirements

- reject relative, traversing, symlink-aliased, overlapping, or group/world-writable roots;
- keep source code separate from credentials, databases, browser state, logs, and caches;
- refuse to overwrite an unrelated launcher or private file;
- preserve configuration and durable data during default uninstall;
- require explicit confirmation for purge;
- fail closed when ownership, type, process-quiescence, or path checks are uncertain.

## Acceptance

Source CI runs the source verifier, root tests, Core tests, installer tests, Handbook and Companion Bridge tests, ShellCheck for the canonical root `install.sh`, and `python dispatch-core --help`. It does not claim live browser, account, production service, or machine-lifecycle acceptance. Those are separate tests on an explicitly designated host.
