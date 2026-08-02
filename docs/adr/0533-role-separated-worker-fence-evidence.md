# 0533 — Separate artifact-fence and termination authorities

## Status

Accepted (2026-08-02)

## Context

ADR-0532 made worker-incarnation termination durable, but its shared database principal lets a
compromised worker publish the evidence used to recover its own artifact fence. It also relies on
application code to reject pre-protocol workers and does not cover a terminal Kubernetes Pod that
never registered. Those properties do not satisfy issue #1803's authority and mixed-version
requirements.

An install provider can continue in a thread after its coroutine is cancelled. Lease expiry, job
reclaim, heartbeat age, and runtime-object absence therefore remain insufficient evidence that the
thread stopped consuming an exact object version.

## Decision

Postgres is the enforcement boundary. Migrations create non-login server, worker, reconciler, and
lifecycle-witness roles. Direct mutation of worker incarnations, build uses, and recovery evidence is
revoked from process roles. Each lifecycle authority mints a random 256-bit per-incarnation credential,
stores only its hash with the immutable runtime binding, and delivers the plaintext once to that exact
worker before its claim loop. Bounded `SECURITY DEFINER` functions expose only the transitions each role
needs. Worker functions require the credential and derive the holder from its hash; callers cannot name
another holder. Acquisition additionally derives the charged attempt from the holder's currently
claimed job. Release accepts a use identifier but deletes it only when the credential-derived holder,
job, and attempt all match. Lifecycle witnesses register exact runtime bindings and publish terminal
evidence; reconcilers recover a use only from a matching terminated incarnation; server/operator paths
request and audit recovery but cannot publish termination.

Every worker incarnation carries the incompatible artifact-fence protocol version. A database trigger
rejects every job transition to `running` unless the claimed worker has an active incarnation with the
current protocol. The deployment procedure stops old workers before installing the enforcement
migration, then starts only workers using role-specific credentials. This prevents old binaries from
claiming installs even if deployment ordering is violated.

Cancellation of the async install waits for the provider thread to finish before releasing or
abandoning the run step. Process death leaves the transaction-independent use row pinned. Recovery
requires a matching immutable termination row and deletes one exact `(use_id, holder, job, attempt)`
fence in the same audited transaction.

The Compose lifecycle gate and Kubernetes witness are the only supported runtime authorities. Compose
serializes create, stop, recreate, and remove around evidence persistence. For Kubernetes, the Pod UID
is the incarnation identifier. A controller observes the initially-finalized Pending Pod, validates its
fixed StatefulSet name/ordinal and UID, and registers that UID before worker startup. A worker init
client presents a short-lived projected service-account token bound by Kubernetes to that Pod UID; the
controller verifies it with TokenReview plus a live UID/resource-version read, consumes the credential
once, and returns it over authenticated cluster TLS into an init-only tmpfs handoff. The long-running
worker receives neither the projected token nor an API-readable Secret. Thus a terminal Pod whose
worker never started still has a pre-start, authority-bound identity; the witness may terminate it but
may not invent a different holder after the fact. The witness compare-and-sets namespace, name, UID,
resource version, and credential-record state before removing the finalizer. API or database failure
retains the runtime object and fence. A lost response after credential consumption is not replayed: the
init remains gated, normal Pod termination/finalizer evidence closes that unused incarnation, and the
StatefulSet replacement receives a fresh UID and credential.

Identity text is at most 512 bytes; serialized authority bindings and Kubernetes names are capped before
persistence; recovery actor, evidence, and reason retain their schema caps of 255, 1024, and 512 bytes.
List requests return at most 100 rows per request using an opaque stable cursor. Each witness or GC pass
processes at most a configured count with a hard ceiling of 1,000 rows, measured on the database clock;
exhaustion retains remaining rows and returns/logs the cursor for another pass. Every protected lookup
joins use -> generation -> investigation -> project before mutation. A recovery audit permanently keeps
`use_id`, project, investigation, generation, job, attempt, holder, authority kind/binding, outcome,
termination time, actor, reason, and database-recorded recovery time. Audit/list pages use stable keys;
permanent growth is operationally monitored and never handled by deleting evidence.

## Consequences

Supported deployments need distinct database credentials, authority delivery of per-incarnation
credentials, and a stop-old-first upgrade. Startup and lifecycle operations fail closed when a required
role credential, incarnation credential, protocol registration, runtime object, or evidence write is
unavailable. The migration owner remains separate from every runtime role.

Worker-incarnation rows and recovery audit rows are permanent. Build-use rows remain until normal
provider completion or evidence-backed recovery. Host-root Docker operations, force-deleted Pods,
manual finalizer removal, and database-owner credentials inside application containers remain
unsupported bypasses that may strand pins but cannot authorize early deletion.

ADR-0532 is superseded. ADR-0531 continues to govern reusable-build ownership and retention.

## Considered & rejected

- **Shared application principal with Python checks.** A worker can call the same mutation path as the
  witness, so the evidence is not independent of the process whose death it claims.
- **Lease or heartbeat timeout recovery.** A provider thread may still be running after either timeout;
  reclaiming on time can delete bytes it still consumes.
- **Runtime-object absence as evidence.** Docker metadata loss, Pod force deletion, and API partitions
  make absence ambiguous.
- **Application-only protocol filtering.** An old binary does not execute the new filter. Database
  enforcement is the only common claim boundary across mixed versions.
- **Expiring incarnation tombstones.** Reuse or delayed writes after expiry can turn old evidence into a
  false proof. Permanent bounded rows preserve monotonic identity.
- **Do nothing and retain crash pins forever.** This avoids false deletion but lets one crash consume
  storage without bound and leaves no supported recovery action, violating eventual bounded recovery.
- **Let the lifecycle witness release every use.** This removes the worker credential but makes normal
  successful completion depend on a separate control-plane round trip and strands all pins whenever the
  witness is unavailable. Authority-minted per-incarnation credentials preserve exact ownership while
  keeping ordinary release local to the completed provider attempt.
