# Security policy

Do not report credentials, private data, or exploitable details in public issues. Until a formal security contact is published, contact the repository administrators through the approved private channel.

Never attach live databases, browser profiles, environment files, cookies, tokens, private documents, logs, or business records to an issue, pull request, or repository checkout. Remove secrets from command output before sharing it.

Dispatch source is installed from a reviewed Git ref. The development channel
tracks current reviewed `main`; the stable channel resolves only immutable
published Release tags. Never make a production installation follow mutable
`main`, an unreviewed branch, or a local copy. The repository contains no package,
wheel, or release-manifest authority. The manual release workflow verifies the
exact approved tag and reruns source tests before publishing release notes.

`scripts/verify-source-export` scans the candidate tree for private data, secret-shaped values, unsafe paths, generated runtime state, and undeclared fixture data. It is a defense-in-depth check, not a substitute for reviewing `git diff --cached` and the final staged path list. Synthetic fixtures must be declared with provenance and a matching digest in `synthetic-data.json`.

Long-running plugin services must read credentials only from their owner-private
secret store or a selected named Core authentication profile. Slack tokens, Amazon cookies,
CSRF material, browser profiles, MFA values, channel/user allowlists, and
conversation mappings must not appear in source, command arguments, service-unit
environment entries, installation receipts, health output, or logs.

## Credential vault trust model

The Core credential vault (Fernet-encrypted `credentials.enc`, key in the OS
keyring or a 0600 fallback file) defends against: other local users reading
credential material, stolen disks/backups/snapshots, plugin overreach beyond
selected profiles, and leakage through output, errors, or receipts. Its
integrity is actively enforced — ancestor-directory permissions and symlink
components are re-checked on every store open, the profile registry carries an
HMAC integrity seal, rotation journals the retired key so an interrupted
rotation recovers instead of bricking, lock acquisition is bounded with
lock-inode identity verification, and size caps apply at write time.

The vault explicitly does NOT defend against:

- **Same-user code execution.** Any process running as your user can read the
  vault key and rewrite records. Fernet's authenticity protects against third
  parties, not same-UID processes.
- **Memory inspection.** Python strings are immutable; decrypted credentials
  cannot be reliably zeroized. A core dump or ptrace of a Dispatch process can
  observe plaintext.
- **Hostile root.** Root bypasses all permission checks by definition.
- **Keyring availability.** Headless hosts without a Secret Service fall back
  to a disk key (`vault.key`), which is a documented trust-model limit, not a
  security boundary. Health reports `authentication.keyring_available` so this
  state is visible.
