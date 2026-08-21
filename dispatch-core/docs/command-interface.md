# Command Interface

Owns the bounded command parser and JSON serialization boundary for the directly executed Core application.

It exposes `health`, `verify`, non-mutating `paths`, non-launching `browser-doctor`, generic `plugin list|health|invoke`, profile-based `auth list|add|status|remove`, compatibility `auth enroll` and realm-form `auth status|remove`, and bounded `collection status|submit|worker-once|reconcile|cancel|resume` actions. Authentication profile commands never accept secrets in arguments, JSON, or environment values; `add` and interactive setup use hidden terminal prompts only. V1 credential rotation creates and selects a new named profile instead of mutating a profile whose persistent browser state may still be active.

Collection submission accepts one selected collector ID plus a bounded scalar JSON parameter object and writes only a durable task record; it never runs collection inline. Authenticated collectors use the named profile selected during setup; legacy callers may still provide an explicit `--account` alias. The resolved internal alias is persisted with the task so later setup changes cannot silently change queued work. Credentials never enter `CollectionContext` or JSON. Collection status and plugin query/health actions are read-only and do not authenticate, browse, or collect. Feature behavior remains in the owning Core packages.
