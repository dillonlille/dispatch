# Paths

Owns portable, non-mutating resolution of Dispatch code, configuration, data, state, cache, runtime, build, and per-owner roots.

The per-user default is `${DISPATCH_HOME}`, normally `${HOME}/.dispatch`, with `config`, `data`, `state`, and `cache` beneath it. Runtime files prefer `${XDG_RUNTIME_DIR}/dispatch` and otherwise use `${DISPATCH_HOME}/runtime`. Individual absolute `DISPATCH_*_ROOT` settings remain supported.

The implementation rejects relative paths, traversal, unsafe symlinks, root collisions, and private or generated roots inside the canonical code release. It creates no directories; creation and permission enforcement belong to the installer.
