# 0604 — Device identity reuses the authority transport

## Status

Accepted (2026-09-05)

## Context

ADR-0603 requires remote-libvirt preparation to resolve host paths through a bounded, redacted
provider-local identity port. ADR-0606 now places a mutually authenticated, bounded,
Resource-selected IPv4 route beneath a typed worker-owned authority sender while preserving
ADR-0584's Unix listener and closed protocol. A production identity adapter needs to extend that
sender without adding SSH, generic command execution, or caller-selected destinations.

## Decision

Add versioned operation `resolve-device-identity` to the existing authority request envelope and
typed `AuthorityRequestSender`. Its
request contains only one validated absolute path; its response is only bounded absence,
`inode(st_dev, st_ino)`, or `block(st_rdev)`. Authentication occurs before the dedicated host
identity service calls the ADR-0603 `HostStatDeviceIdentity` adapter. The external-boot mutation
service and its existing request operations remain unchanged.

A synchronous remote-libvirt adapter receives the already Resource-bound typed sender and one
monotonic absolute preparation deadline. Each `identity(path)` call computes the remaining
preparation budget before entering a private event loop, converts only that remainder to the new
loop's absolute clock, and awaits the typed sender once. The sender retains ADR-0606's fixed
endpoint, call-local TLS materialization, active-incarnation credential borrowing, and
single-deadline TCP/TLS framing and cleanup. Repeated lookups consume the same captured preparation
deadline rather than resetting a duration. Both ends strictly validate closed shapes and bounds;
the caller receives only redacted conflict or infrastructure errors.

## Consequences

Remote preparation gains one authenticated device-identity path without a second host agent,
second connection configuration, or generic execution surface. The shared transport gains an
additive operation
that must remain compatible with
external-boot consumers, including #2200. #2170 supplies the later preparation orchestration that
calls this adapter.

## Considered & rejected

- **Use SSH or a generic remote command runner.** judgment: it would add destination, command,
  credential, output, and quoting surfaces for one `stat(2)` observation.
- **Create a second client or provider-host listener.** judgment: duplicating Resource selection,
  credential borrowing, authentication, TLS, framing, and deployment readiness adds more surface
  than one closed additive operation on ADR-0606's sender.
- **Perform the lookup on the worker filesystem.** verified: ADR-0603 requires the identity from
  the remote provider host because aliases and device nodes are host-local facts.
- **Expose identity through the generic provider runtime contract.** verified: ADR-0603 explicitly
  keeps host filesystem identity inside remote-libvirt preparation.
