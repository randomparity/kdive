# 0800 — Fixed local proof authority inventory collector

## Status

Accepted (2026-10-18)

Implements the reviewed #3237 collector design.
Installation and native permission proof require separately approved execution.

## Context

#3237 needs an unprivileged observer while preserving complete cleanup inventory.
The private authority UID owns its RW daemon socket and mode `0700` rootfs/console trees.
The approved proposal records why an existing server-enforced RO socket is not a
usable basis for this lane; this design does not repeat the earlier operational
experiments. The implementation base verifies the existing root invocation and private
ownership in authority_lane.py and both provisioners. Reusing the authority-owned
venv would let a mutable runtime become the trusted mediator's executable input.

## Decision

Use one dedicated observer UID and a root-owned local systemd socket. Each accepted
request runs one fixed collector as the existing authority UID from a separate
root-owned locked runtime. Authenticate exact peer UID and accept only EOF-framed
zero/one canonical UUID. Return the existing inventory facts; paths, environment,
code, methods and interpreter are fixed. Root provisioning owns installation and
withdrawal; both effective roles share the same tasks and default to disabled.
The production authority protocol, remote branch and private ownership are unchanged.

## Consequences

The observer receives only connect/traversal access to this proof endpoint. Its
caller cannot obtain arbitrary authority execution, a private socket handle or file
contents. The collector and authority remain trusted: read-only connection flags do
not protect against their compromise. Installation adds one locked runtime copy,
two systemd units, one client, one collector and shared opt-in tasks; build/storage
costs require an approved live packet. Bounds can reject oversized or busy proofs;
no success may discard required facts. Real native permission evidence remains
necessary. The design adds no persistent state or general query/API framework.

## Considered & rejected

- Existing private-daemon RO endpoint: verified by the operator-admitted #3237 proposal, which records the prior failed applicability evidence. Not re-tested here; no
  claim of a universal libvirt limitation.
- Run arbitrary Python as authority UID: verified source ownership in both roles'
  install/main tasks gives that UID the RW socket and guest storage;
  the fixed client flag cannot narrow what that identity can request.
- Reuse authority venv as trusted collector runtime: verified same source creates
  its /opt parent authority-owned mode `0700`. The authority can replace execution inputs.
- Separate server-enforced-RO libvirt deployment: judgment: changes lane deployment
  assumptions beyond the admitted narrow mediator and needs another scope decision.
- General proxy or production inventory RPC: judgment: adds capability/interface
  scope unnecessary for zero/one-UUID proof inventory; excluded production redesign.
- Keep root observer or omit unreadable roots: at implementation base
  `91c57c879caa82f2532892491ff135b92635f6eb`, authority_lane.py runs sudo without
  a target UID and compares full inventory for cleanup. Those choices fail observer
  isolation or complete cleanup evidence, respectively.
- Leave honest blocked observer indefinitely: judgment: safe stop when the mediator
  cannot be installed/proven, but does not deliver the user's admitted outcome.
