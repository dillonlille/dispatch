# Command Interface

Owns the bounded installed `dispatch-core` command parser and JSON serialization boundary.

It exposes `health`, `verify`, non-mutating `paths`, non-launching `browser-doctor`, generic `plugin list|health|invoke`, bounded `auth status|enroll|remove`, and bounded `collection status|worker-once|reconcile|cancel|resume` actions. Plugin invocation accepts one bounded JSON object and reaches only installer-approved `dispatch.plugins` entry points. Authentication enrollment reads every credential value through hidden terminal prompts; no command accepts secret values in arguments or returns them in JSON. Collection status is read-only, while worker and reconciliation actions explicitly open private durable state. Feature behavior remains in the owning Core packages.
