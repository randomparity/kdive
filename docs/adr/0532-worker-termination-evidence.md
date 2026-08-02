# 0532 — Preserve exact worker termination evidence

## Status

Accepted (2026-08-02)

## Context

ADR-0531 permits an admitted install to pin an expired reusable build until its worker finishes.
A process crash can strand that pin. Recovery cannot use heartbeat age, lease expiry, runtime-object
absence, or replacement identity as proof: an old process may still consume an exact object version.

The first ADR-0531 implementation inspected a stopped Compose container or terminal Kubernetes Pod.
Routine Compose recreate and StatefulSet rollout/scale-down can erase those objects before an
operator recovers the pin. The issue #1519 review therefore found that eventual reclamation was not
operable in the reference deployments.

After that bounded review exhausted its original cycle, the operator explicitly authorized a Docker
lifecycle gate and a Kubernetes finalizer/controller boundary on 2026-08-02. A subsequent independent
design review approved the exact protocol before implementation.

## Decision

Every worker registers a permanent, immutable incarnation row before claiming jobs. The row has one
transition, `active -> terminated`; a terminated identity is never reusable or deleted. Creating a
reusable-build use and terminating its holder take the same transaction-scoped incarnation advisory
lock. Use creation requires the row to remain active. This makes termination-versus-use deterministic
and prevents a later replay after evidence retention would otherwise expire.

The reference Compose lifecycle uses a gate with the Docker socket. A supplied wrapper creates the
worker without starting it, injects a random 128-bit incarnation nonce, and binds that nonce to the
exact full container ID before start. Stop/recreate/down retains and inspects that exact labeled
container, commits its terminal transition, and only then removes it. Raw host-root Docker/Compose
operations bypass the gate and are unsupported; bypass remains fail closed.

The Helm worker Pod template carries `kdive.io/worker-termination-evidence` in its initial create.
A bounded reconciler witness reads only configured StatefulSet ordinal names. It accepts only the
same Pod UID in `Succeeded` or `Failed`, commits termination, and then removes its exact finalizer
with resource-version-fenced JSON Patch. API/database failure leaves the Pod and pin retained.
Node loss, force deletion, and manual finalizer removal are unsupported infrastructure bypasses and
never become absence evidence.

The chart retains an ordinal ceiling as recovery history. It may increase but not decrease. The
one upgrade from an unannotated pre-feature chart requires an explicit adoption ceiling; this is safe
because released pre-feature workers could not create reusable-build use rows.

The application processes retain the repository's existing shared database principal. This ADR does
not claim SQL isolation between trusted KDIVE processes. Tenant and operator APIs cannot register or
terminate incarnations; authorized recovery refusals remain durably audited.

## Consequences

Reference lifecycle commands become evidence-preserving and may block when Docker, Kubernetes, or
Postgres cannot prove and persist exact termination. That is intentional. Host-root or force-delete
bypasses can still strand a pin and require infrastructure repair, but cannot cause premature object
deletion.

The permanent incarnation table grows by one fixed-size row per worker incarnation. Queries are
exact-key or bounded diagnostic pages; there is no unbounded cleanup scan and no tombstone expiry.

ADR-0531 remains authoritative for reusable-build ownership and retention. This ADR supersedes only
its worker-termination recovery evidence mechanism.
