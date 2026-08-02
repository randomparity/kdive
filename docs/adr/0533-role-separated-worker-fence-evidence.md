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
revoked from process roles. Bounded `SECURITY DEFINER` functions expose only the transitions each role
needs: workers acquire and release their own attempt's use; lifecycle witnesses register exact runtime
bindings and publish terminal evidence; reconcilers recover a use only from a matching terminated
incarnation; server/operator paths request and audit recovery but cannot publish termination.

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
serializes create, stop, recreate, and remove around evidence persistence. The Kubernetes witness
handles configured worker ordinals, including terminal Pods that never registered, by atomically
recording the Pod UID binding and termination before removing the exact finalizer. API or database
failure retains the runtime object and fence. Bounds apply to identities, bindings, configured ordinals,
per-pass work, and stored audit text.

## Consequences

Supported deployments need distinct database credentials and a stop-old-first upgrade. Startup and
lifecycle operations fail closed when a required role credential, protocol registration, runtime
object, or evidence write is unavailable. The migration owner remains separate from every runtime
role.

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
