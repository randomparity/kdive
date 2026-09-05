# 0604 — Device identity reuses the authority transport

## Status

Accepted (2026-09-05)

## Context

ADR-0603 requires remote-libvirt preparation to resolve host paths through a bounded, redacted
provider-local identity port. ADR-0584 already places a mutually authenticated, bounded Unix
request transport on that provider host, but its closed protocol currently accepts only two
external-boot operations. A production identity adapter needs an authenticated path to the host
without adding SSH, generic command execution, or caller-selected destinations.

## Decision

Add versioned operation `resolve-device-identity` to the existing authority request envelope. Its
request contains only one validated absolute path; its response is only bounded absence,
`inode(st_dev, st_ino)`, or `block(st_rdev)`. Authentication occurs before the dedicated host
identity service calls the ADR-0603 `HostStatDeviceIdentity` adapter. The external-boot mutation
service and its existing request operations remain unchanged.

A synchronous remote-libvirt adapter connects with one injected, fixed mutual-TLS Unix client
configuration, request credential, and monotonic absolute preparation deadline. Each call consumes
that captured deadline across connection, handshake, request, response, and close. Both ends
strictly validate
closed shapes and bounds; the caller receives only redacted conflict or infrastructure errors.

## Consequences

Remote preparation gains one authenticated device-identity path without a second host agent or a
generic execution surface. Its client must run where the configured authority Unix socket and
active incarnation credentials are available. The shared transport gains an additive operation
that must remain compatible with
external-boot consumers, including #2200. #2170 supplies the later preparation orchestration that
calls this adapter.

## Considered & rejected

- **Use SSH or a generic remote command runner.** judgment: it would add destination, command,
  credential, output, and quoting surfaces for one `stat(2)` observation.
- **Create a second provider-host listener.** judgment: duplicating authentication, TLS, framing,
  socket ownership, and deployment readiness adds more surface than one closed additive operation.
- **Perform the lookup on the worker filesystem.** verified: ADR-0603 requires the identity from
  the remote provider host because aliases and device nodes are host-local facts.
- **Expose identity through the generic provider runtime contract.** verified: ADR-0603 explicitly
  keeps host filesystem identity inside remote-libvirt preparation.
