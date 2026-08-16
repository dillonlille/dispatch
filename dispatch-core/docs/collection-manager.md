# Collection Manager

Status: **durable orchestration and bounded worker-process supervision implemented**.

Collection Manager is a Dispatch Core feature, not a plugin. Domain plugins register trusted collector runners through `CollectorRegistration`; registration does not make a collector model-facing. A Core installation with zero collectors and zero tasks is healthy and reports `no_collectors`.

## Implemented boundary

Collection Manager provides:

- globally unique, bounded collector identities tied to a plugin ID and release;
- immutable collection requests with at most 16 scalar parameters;
- one private SQLite database at `data/db/collection-manager/collection-manager.sqlite3`;
- transactional task submission and idempotency keys;
- atomic claims with a worker identity and expiring ownership lease;
- spawned worker-process isolation with a fixed execution deadline and periodic ownership heartbeats;
- durable worker PID, Linux process-start identity, and non-sliding deadline attachment before execution;
- explicit `queued`, `retry_wait`, `running`, `waiting_for_user`, `succeeded`, `failed`, `cancelled`, and `uncertain` states;
- an `execution_started` watermark that prevents automatic replay after collector code begins;
- bounded pre-execution retries with exponential delay and a fixed attempt limit;
- persistent cancellation requests and cancellation at safe execution boundaries;
- reconciliation of expired running and manual-intervention tasks;
- collector-owned publication verification returning the exact `PublicationVerification.ABSENT` result, with task-bound verification evidence persisted before retrying uncertain or already-published work;
- fixed-interval durable schedules with stable caller-owned schedule keys, durable occurrence identities, duplicate suppression, pause/resume, and missed-interval coalescing;
- durable, strictly validated, secret-free `CollectionReceipt` data;
- optional Browser Manager collection leases and Authentication coordination;
- headed browser leases plus explicit resume/cancel handling for MFA or CAPTCHA;
- read-only durable-storage health inspection that rejects unsafe existing paths without creating an empty database.
- verified process-group termination before interrupted work is reconciled or becomes reusable.

Task records contain only bounded request metadata, state and lease fields, worker ownership identity, safe error codes, schedule identity, publication-verification evidence, and validated receipt data. Worker process identity is omitted from public task results. Records do not contain credentials, cookies, browser state, exception text, logs, or domain records.

## Safety and recovery

Only failures known to occur before collector execution may retry automatically. Once `execution_started` is durable, a worker crash or result without a valid receipt becomes `uncertain`; it is not replayed. Retrying uncertain or published work requires the registered collector's trusted verifier to return exact absent-publication evidence; Core binds that evidence to the task and publication identity and persists it before requeueing. This protects against duplicate publication when a process fails after a domain plugin commits data but before Core records its receipt.

MFA and CAPTCHA tasks remain durably visible as `waiting_for_user`, while the live browser session remains private to the worker process that opened it. Resume and cancellation requests are durable and are delivered only to that owning worker. If the worker disappears, the supervisor terminates its verified process group before reconciliation; pre-execution work receives a bounded retry while started work becomes uncertain. If the recorded leader is gone while its process group still has a live member, Core retains the durable identity and quarantines the task rather than signaling or reusing ownership it can no longer prove.

Schedules are intentionally fixed intervals rather than a general workflow or cron framework. When several intervals are missed, one due task is enqueued and the next occurrence advances beyond the current time. A schedule advances only after its exact occurrence was created or an existing task with the same schedule/occurrence identity was verified.

## Execution model

`run_next()` remains the direct synchronous one-task API. `CollectionWorkerSupervisor` instead starts a fresh spawn-only process (the inherited-state `fork` context is rejected), waits for that child to claim supported work, durably attaches the child PID/start identity and deadline, then authorizes execution. The supervisor heartbeats the ownership lease without extending the execution deadline. On timeout, crash, state-store failure, or caller interruption it terminates the verified owned process group before calling reconciliation. Terminal state retains worker identity until process-tree cleanup is verified, preventing premature capacity reuse.

`CollectionService.tick()` performs orphan cleanup, expired PID-less lease reconciliation, due-schedule enqueueing, and one supervised worker invocation. `CollectionService.run()` is a foreground, explicitly stoppable loop and starts no hidden threads. The installer runs this foreground command through the user-owned systemd service.

Collector runners own site-specific browsing, staging, schema/domain validation, atomic publication, and publication identity. Core does not interpret domain records or expose a generic collect action to Hermes. Browser cleanup failure overrides apparent task success; if a valid publication receipt already exists, it is retained and the task cannot be retried without publication verification.
