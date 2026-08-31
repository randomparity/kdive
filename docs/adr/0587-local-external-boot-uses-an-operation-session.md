# 0587 — Local external boot uses an operation session

## Status

Accepted (2026-08-31)

## Context

ADR-0586 established closed, owner-bound local recovery artifacts, but the remaining host seam
groups high-level lifecycle operations. That shape cannot prove that one operation retains the
same libvirt domain, System overlay, artifact root, and inactive libguestfs access while the
caller holds the System serialization lane. Reopening those resources independently would permit
ownership drift or mutation after the domain became active.

## Decision

Local external-boot host access is represented by one operation-scoped
`LocalExternalBootSession`. Its factory requires a live `LocalExternalBootOperationLease`, a
provider-local nominal capability issued only by the System ownership and serialization-lane
context. The lease binds the canonical System id and activation binding and exposes a pin that
keeps the lane held. Session construction acquires one pin before any host resource opens and
retains it until every guest and host wrapper is irrevocably poisoned and its close has been
attempted; lease release fails while a pin exists.
A missing, foreign, or released lease rejects the operation before opening libvirt or filesystem
resources, and the lane therefore cannot disappear between guest operations.
Construction then opens and validates the expected KDIVE domain, exact System overlay, and an
injected owner-bound artifact-root directory descriptor.

The session owns the libvirt connection/domain and artifact-root descriptor until close. It opens
a reopenable libguestfs guest session only after fencing and rechecking the domain inactive, and
binds that guest session to the already-validated overlay identity. It exposes narrow primitives
for closed domain inspection, inactive fencing, artifact descriptor access, inactive guest access,
XML definition, power and readiness, running observation, and payload cleanup. Closed inspection
returns the exact inactive definition and ADR-0583 definition/source-boot identities. The session
does not expose paths or advertise external boot.

Cleanup first poisons the session and every guest wrapper so no subsequent method can reach an
underlying handle. It then attempts every owned resource in dependency-safe reverse acquisition
order: active guest, artifact-root descriptor, domain reference, libvirt connection, then the
operation-lease pin. A close error cannot make the poisoned handle callable again. Failure to close
one resource does not skip later cleanup; the original operation failure remains primary and any
close error is reported after pin release.

## Consequences

Provider-local orchestration can keep all privileged observations and mutations inside one
ownership and serialization lifetime. Unit tests can prove acquisition ordering, inactive-before-
mutation, exact identity binding, reopen behavior, and release without a live hypervisor. The
downstream six-port adapter remains responsible for recovery state decisions and #2140 remains
responsible for authority integration and advertisement.

## Considered & rejected

- **Keep high-level methods on `LocalExternalBootHost`.** judgment: this hides resource acquisition
  and cleanup ordering and cannot make same-domain/same-overlay lifetime a testable invariant.
- **Open a fresh libvirt or libguestfs handle for each primitive.** judgment: independent opens
  weaken the operation fence and multiply substitution windows without adding capability.
- **Pass raw host paths to the adapter.** verified: ADR-0586 requires opaque owner-bound references
  and descriptor-relative access; exposing paths would contradict that accepted boundary.
