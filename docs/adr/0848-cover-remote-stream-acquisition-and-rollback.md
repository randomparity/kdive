# 0848 — Cover remote stream acquisition and rollback

## Status

Proposed

## Context

The remote pcap, host-dump and qcow2 transfer functions acquire libvirt streams outside their
existing error boundaries. Upload creates its destination first and catches only libvirt errors,
so acquisition and local source-I/O failures can leave an incomplete target behind.

## Decision

Keep each transfer with its existing owner. Acquire the stream inside the existing try, tracking
absence explicitly so cleanup aborts only an acquired stream. Keep download categorized errors
and ceilings intact. Upload catches libvirt and source OSError failures after volume creation,
attempts abort then delete, warns on native deletion failure, and chains its original failure.
Expected abort failures cannot prevent deletion. Pre-create validation, existing-volume policy,
volume creation errors and public signatures remain unchanged.

## Consequences

Setup failures receive the same context as transfer failures. Failed upload cleanup is visible
and preserves the primary cause; successful rollback permits a same-name retry. No generalized
transfer framework, privilege, host prerequisite or dependency is added. Unexpected programming
and process-control failures retain existing propagation; this is not a universal termination
cleanup guarantee.

## Considered & rejected

- **Leave setup outside transfer handling.** verified: at e18de8fde the private transfer
  reproduction has four failures in0.47s; native setup/source errors escape, and the upload
  observation records created destinations left undeleted after setup and source-reopen faults.
- **Create a shared transfer framework.** judgment: three short existing try/cleanup boundaries
  already express their owners' different sinks and contexts; a new abstraction adds indirection.
