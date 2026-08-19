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
secret store or a fixed Core authentication realm. Slack tokens, Amazon cookies,
CSRF material, browser profiles, MFA values, channel/user allowlists, and
conversation mappings must not appear in source, command arguments, service-unit
environment entries, installation receipts, health output, or logs.
