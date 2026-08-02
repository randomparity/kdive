# Worker-crash artifact-use fences design

Issue: #1803
Decision: [ADR-0533](../../adr/0533-role-separated-worker-fence-evidence.md)
Branch: `feat/worker-crash-artifact-fences-1803`
Base: `main`
Guardrails: focused pytest during TDD; `just ci` before each implementation/review commit
ADR index coupling: not coupled; ADR-0504 makes `docs/adr/` the index.

## Frozen authority

- Scope identity: `https://github.com/randomparity/kdive/issues/1803` plus
  `scope-1803-7f7a1ef9-20260802`.
- Interaction: interactive.
- Outcome: exact artifact-use fences survive cancellation and lease overlap and are recoverable only
  after authoritative worker-termination evidence.
- Criteria: a live provider attempt pins every exact version it may consume; cancellation does not
  release while its provider thread runs; dead-worker recovery uses authoritative immutable evidence;
  workers cannot forge evidence or remove another attempt's fence; Compose and Kubernetes preserve
  evidence before runtime objects disappear; unsupported bypasses fail closed; old workers cannot
  claim protocol-required jobs; recovery and GC are bounded, audited, and tenant-safe.
- Provenance: issue #1803's Problem, Expected, Proposed approach, and Remaining blockers sections.
- Exclusions: #1519 reusable-build ownership except direct fence integration; future cloud,
  bare-metal, and PowerVM witnesses; making host-root Docker, force-delete, or manual-finalizer
  bypasses safe.
- Surface: worker/job lifecycle and cancellation; use/incarnation persistence and database roles;
  recovery/GC; Compose and Helm/Kubernetes lifecycle witnesses; deployment configuration; operator
  contracts; focused tests, documentation, decision record, plan, and generated artifacts.
- Ambiguities: none. Issue #1803's later authority requirement supersedes the shared-principal choice
  in the preserved prototype.

## Approaches

### Selected: database-enforced capabilities and protocol claims

Use role-specific credentials, revoke direct writes to the security-sensitive tables, and expose
small role-gated SQL functions. Enforce the worker protocol on the database transition into
`jobs.state = 'running'`. This is the only approach that constrains both a compromised current worker
and an old binary that does not contain new checks.

### Rejected: shared principal plus service-layer checks

This is close to the preserved prototype and minimizes deployment work, but any worker connection can
publish termination or delete a use if it can reach the underlying tables. Old workers also bypass new
Python claim checks.

### Rejected: separate witness service without database privilege separation

A separate container or controller improves operational ownership but is not a security boundary when
all processes use the same database rights. It cannot establish the unforgeability criterion.

## Architecture and invariants

### Durable records

The existing reusable-build tables from `main` remain immutable migrations 0095–0097. Preserved branch
migrations are reconciled into a new monotonic tail; duplicate-version and duplicate-tombstone files are
removed before new migrations run. The tail adds per-attempt use rows, immutable incarnation rows,
recovery audit rows, GC cursors/indexes, protocol metadata, role grants, and guarded functions.

An incarnation is a bounded exact runtime identity plus authority kind and binding. It transitions only
from active to terminated and is never deleted. Its `fence_protocol` is immutable. A use is keyed by a
random `use_id` and also binds investigation generation, job id, charged attempt, and holder
incarnation. A recovery audit copies the immutable termination facts before deleting that exact use.

### Authority matrix

| Actor | Allowed security-sensitive operation | Denied operation |
|---|---|---|
| server/operator | submit and audit a bounded recovery request | publish termination; directly delete uses |
| worker | authenticate an authority-registered active identity; acquire/release its own current attempt | create, rebind, activate, or terminate an identity; release another attempt; mutate evidence |
| lifecycle witness | register an exact Docker/Pod binding; terminate that same binding | claim jobs; acquire/release uses; recover pins |
| reconciler | recover one exact use after matching terminal evidence | create or alter termination evidence |
| migration owner | install schema and grants outside runtime containers | participate in normal runtime |

Postgres functions verify `session_user` membership, validate bounded inputs, acquire the incarnation
advisory lock, and perform each transition transactionally. Runtime roles receive no direct mutation
grant on the protected tables. The lifecycle authority alone registers the exact runtime binding,
mints a random 256-bit credential for each incarnation, stores only its hash, and delivers the plaintext
once before worker startup. The worker can authenticate that existing active incarnation but cannot
create, rebind, or reactivate one. Worker functions derive the holder from the credential hash and
derive job/attempt ownership from the locked claim; they never trust a caller-supplied holder. Supported
Compose and Helm manifests provide distinct secret-backed DSNs; the shared migration credential is not
injected into runtime workloads. Local development setup creates equivalent supervisor-owned
credentials rather than weakening the checks.

### Claim and upgrade protocol

Every new worker is registered active with the current fence protocol before its claim loop starts. A
database trigger rejects a transition to `running` when `worker_id` is absent, unknown, terminated, or
on an older protocol. The upgrade runbook therefore performs: stop old workers; install schema/grants;
rotate runtime credentials; start witnesses; start current workers; verify registration; resume queue
processing. If ordering is wrong, jobs remain queued and startup/claim fails visibly.

### Provider cancellation

The install handler acquires a use immediately before provider consumption and runs the synchronous
provider in a supervised task. On coroutine cancellation it records the cancellation, waits for the
thread-backed task to finish, then abandons the run step and releases the use. It re-raises cancellation
after cleanup. If the process dies during that wait, no cleanup runs and the independent committed use
remains pinned. Normal exceptions abandon and release only after the provider call has returned.

No handler may infer safety from lease loss or a replacement worker. Heartbeats may extend diagnostic
lease fields but never delete the use.

### Lifecycle evidence

The Compose gate owns the Docker socket for supported worker lifecycle operations. It creates without
starting, injects a random 128-bit nonce, binds it to the full container ID, registers through the
witness role, starts, and later persists terminal outcome before removal. Create, stop, recreate, and
remove are serialized; a missing registry or database fails before destructive runtime action.

The Kubernetes Pod template carries its finalizer at initial creation and uses the Pod UID as its
incarnation. Before the worker claim loop starts, a controller validates the fixed StatefulSet
name/ordinal and UID, registers that exact binding, and writes the one-time incarnation credential to a
Pod-UID-owned Secret; an init gate blocks until that exact Secret is mounted. The witness scans only the
configured ordinal range and validates namespace, name, UID, resource version, finalizer, and terminal
phase. A terminal Pod that never entered the worker process is already authority-bound by this pre-start
record, so the witness terminates that existing identity rather than synthesizing a post-hoc holder. It
removes only its own finalizer with resource-version, UID, and binding tests. API/database failure leaves
the Pod unchanged. Ordinal history can increase but not decrease.

### Recovery, GC, and tenancy

Operator recovery resolves the caller's project authorization before selecting a use. Inputs and pages
have explicit limits: identity 512 bytes; binding serialization bounded before persistence; actor 255,
evidence 1024, and reason 512 bytes; list pages at most 100 rows with an opaque cursor. Witness and GC
passes use a configured row count with a hard ceiling of 1,000 rows on the database clock; exhaustion
retains work and publishes the continuation cursor. The reconciler function accepts one exact use,
joins through investigation to the authoritative project, verifies the holder and immutable terminal
evidence under lock, writes the full immutable use/termination/actor tuple, and deletes the use
atomically. Audit rows are permanent and page by stable key. A missing, active, mismatched, malformed,
or cross-project identity is a refusal, not a retry-by-time fallback.

GC treats every use row as a pin regardless of lease age. It scans with durable cursors and fixed batch
budgets and deletes only exact object versions after finding no use for the generation. A database or
object-store failure leaves the row/tombstone retryable and never widens the deletion set.

## Failure contracts

- Missing/invalid process-specific DSN: process startup fails with the exact required setting.
- Unknown/old/terminated worker claim: database rejects the claim; the job stays queued.
- Provider cancellation: caller observes cancellation only after the provider thread exits; process
  death retains the use.
- Witness database/API failure: runtime object and finalizer remain; no termination is inferred.
- Conflicting registration/termination replay: fail closed and preserve the first immutable facts.
- Unauthorized SQL operation: permission denied, with no protected-table mutation.
- Recovery mismatch: audited refusal where the operator surface requires it; use remains pinned.
- Bound exhaustion: stop at the documented page/pass bound and resume with the returned cursor or next
  invocation.

## Threat model

### Actors and trust

Untrusted actors are an authenticated tenant invoking MCP tools, a compromised worker process, an old
worker binary during rollout, and malformed/stale Docker or Kubernetes runtime state. The database
migration owner, lifecycle witness credential, orchestrator control plane, and host/root operator are
trusted within their stated supported boundaries. Host-root and cluster force-delete bypasses are out
of scope and may strand, but never release, pins.

### Boundaries and controls

| Boundary | Input/controller | Control | Failure disclosure |
|---|---|---|---|
| tenant → recovery tool | use id, reason, project context | existing RBAC/project lookup, bounded fields/pages, audit | stable categorized refusal without cross-tenant facts |
| worker → protected DB state | incarnation, job/attempt, use id | worker role plus guarded function, row/advisory locks, exact ownership checks | bounded operation error |
| witness → termination state | container/Pod identity and outcome | witness-only role, immutable authority binding, exact runtime checks | bounded lifecycle error; object retained |
| old worker → job claim | direct state transition | database trigger requires current active protocol incarnation | claim rejected; job remains queued |
| reconciler → use deletion | use and holder ids | reconciler-only function, matching terminal row, atomic audit+delete | refusal; use retained |
| runtime API → witness | Docker JSON or Pod JSON | schema/type/length validation, exact ID/UID/name/resource version | no absence inference |
| GC → object store | stored key plus immutable version | tenant-scoped DB selection, no-use predicate, exact-version delete, batch bound | tombstone retained for retry |

The design adds role-specific DSN boundaries and widens the Compose/Kubernetes witness boundary to
publish exact termination. It reuses existing RBAC, advisory-lock, audit, response-envelope, and
exact-version deletion controls.

Explicitly out of scope: malicious database owners; host-root Docker bypass; Kubernetes force deletion
or manual finalizer removal; witnesses for future providers; making a compromised witness harmless.
These actors already control the evidence boundary or are excluded deployment paths.

## Executable acceptance proofs

1. A focused cancellation test blocks the provider thread, cancels the coroutine, and proves the use
   remains until the thread exits; a process-death simulation proves the committed row remains.
2. Overlapping attempts hold distinct uses; releasing or recovering one cannot unpin the other.
3. SQL-role tests prove worker, server, reconciler, and witness allow/deny matrices, including a worker
   unable to create, rebind, activate, or terminate an incarnation or delete another use.
4. Claim tests prove an unregistered, terminated, and old-protocol worker cannot transition a job to
   running; a current active worker can.
5. Recovery/GC races prove termination-versus-use ordering, atomic audit+delete, tenant isolation,
   bounded scans, and exact-version deletion only after the last use is gone.
6. Compose executable tests cover SIGKILL, create, stop, recreate, remove, database outage, and raw
   bypass refusal while preserving the exact container evidence.
7. Helm/controller tests cover rollout, scale-down, terminal never-registered Pods, API/database
   outage, UID replacement, finalizer fencing, ordinal bounds, and credential separation.
8. Deployment tests prove migration credentials are absent from runtime containers, role-specific
   credentials are wired, and the stop-old-first protocol is documented and structurally enforced.
9. Focused suites pass, then the repository gate `just ci` passes without warnings.

## Rollback

Code can be reverted only after draining current workers and retaining the protected schema and
evidence rows. The role/protocol migration is forward-only: operators may restore the previous binary
for diagnosis, but it cannot claim jobs through the current database. Recovery audit and incarnation
records are never deleted during rollback.
