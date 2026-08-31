# Local external-boot operation session design

## Scope

Issue #2143 introduces only the operation-scoped local host capability needed by the later real
adapter. It does not wire the six shared ports or advertise external boot; those remain #2144 and
#2140. The design implements [ADR-0587](../../adr/0587-local-external-boot-uses-an-operation-session.md)
on Python 3.14 for x86_64 and ppc64le without a dependency or schema migration.

## Contract

`LocalExternalBootSessionFactory.open(lease)` accepts only a live nominal
`LocalExternalBootOperationLease`. The provider-local ownership/lane context issues that capability
after resolving an `ExternalBootActivationBinding` to its canonical System and acquiring the
per-System advisory lane. The lease binds both values and exposes `pin()`. The factory acquires one
pin before opening any libvirt/filesystem resource and owns that pin until the session has
irrevocably poisoned every guest/host wrapper and attempted every close. Lease release fails while
any pin exists, so the database lane
cannot be released while a guest context can still observe or mutate the overlay. A missing,
released, or foreign lease therefore opens nothing. #2144 will
adapt its already-held database lane into this provider-local capability; this issue defines and
tests the boundary without wiring the six-port adapter.

The factory is constructed with an injected `open_artifact_root(lease) -> int` callback. That
callback returns a no-follow, owner-validated directory descriptor already scoped to the lease's
System/Run artifact directory. This issue does not introduce or guess a new environment setting:
#2144 will bind the callback to the provider-local artifact/recovery roots it owns. The factory
itself binds only the existing local libvirt URI and overlay-root convention. It validates canonical
System identity before opening resources and rejects domain ownership or overlay identity mismatch.

`LocalExternalBootSession` owns one libvirt connection and domain, the exact overlay identity
(device/inode plus canonical expected path), and an already-open artifact-root directory descriptor.
It exposes only:

- `inspect_closed()` returning immutable XML bytes, active state, ADR-0583 definition identity,
  source-boot identity, domain name, and overlay identity;
- `require_inactive()` and `stop_and_require_inactive()` fencing every disk mutation;
- descriptor-relative artifact opening and recovery-root descriptor access;
- `guest()` returning a reopenable exact-overlay inactive guest-session context;
- inactive XML definition, start/restore-power and readiness, running-kernel observation, and
  owner-bound payload cleanup primitives.

The guest context rechecks inactivity immediately before every open, authenticates the overlay
identity again, and always shuts down and closes libguestfs. The outer lease pin remains held across
all guest operations; an attempted lease release while a guest is open fails without releasing the
lane. Closing first atomically poisons the outer session and active guest wrapper, making every
subsequent method fail before reaching an underlying handle. It then attempts to close the guest
handle, artifact descriptor, domain reference, and libvirt connection in that exact dependency-safe
reverse order, then releases the operation pin. A close fault is reported but cannot re-enable the
poisoned wrapper. All cleanup attempts run even when an earlier close fails.

Closed inspection safe-parses the inactive XML, verifies the KDIVE System metadata and expected
domain naming, finds exactly one writable qcow2 System disk matching the configured overlay, and
uses ADR-0583 canonical definition/boot identity helpers. It never returns live mutable objects or
host paths.

## Failure behavior

Ownership mismatch, absent/duplicate disk identity, a running or indeterminate domain before guest
mutation, descriptor substitution, and use-after-close fail before mutation. Partial construction
poisons and attempts release of every acquired resource. If work and cleanup both fail, the work
failure remains primary and cleanup failures are attached as notes. A close-only failure is reported
after all cleanup attempts and pin release; retained wrappers remain poisoned.

## Threat model

The authenticated worker caller and local operator configuration are trusted; tenant-influenced
artifacts and stale worker requests are not. Added boundaries are libvirt XML into System identity,
configured roots into descriptors, and the inactive overlay into libguestfs. Exact System metadata,
canonical paths, no-follow descriptors, file identity rechecks, private ownership checks, and
inactive fencing control those boundaries. Compromise of the trusted host, libvirtd, or libguestfs
appliance is out of scope. The capability is not composed into advertised support in this change.

## Verification

Unit tests use fake leases, connections, domains, descriptors, and guest handles to prove that a
missing/released/foreign lease opens no resource, factory ordering, wrong-System rejection, exact
ADR-0583 identities, lane pinning across every guest operation, failed release while a guest is
open, and inactive fencing before every guest open,
reopening after a guest context closes, overlay substitution rejection, use-after-close, complete
cleanup on success and on faults, and no capability advertisement. Run focused provider tests,
`just lint`, `just type`, `prek run`, and pre-push `just ci`. Live VM tiers are not run without the
operator-provided hosts.
