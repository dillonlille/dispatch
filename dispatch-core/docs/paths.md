# Paths

Owns portable, non-mutating resolution of the Dispatch checkout plus configuration, secrets, data, state, cache, logs, runtime, build, and per-owner roots.

The per-user default is `${DISPATCH_HOME}`, normally `${HOME}/.dispatch`, with `config`, `secrets`, `data`, `state`, `cache`, `logs`, and `run` beneath it. The application checkout is `${DISPATCH_HOME}/dispatch`. Individual absolute `DISPATCH_*_ROOT` settings remain supported.

The implementation rejects relative paths, traversal, unsafe symlinks, root collisions, and private or generated roots inside the canonical code release. It creates no directories; creation and permission enforcement belong to the installer.
