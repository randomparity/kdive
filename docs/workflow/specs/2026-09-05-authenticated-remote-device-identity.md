# Authenticated remote-host device identity transport

## Scope and governing decisions

Issue #2250 supplies ADR-0603's remote-host implementation. ADR-0604 selects an additive typed
operation over ADR-0606's Resource-bound mutual-TLS authority route, which preserves the Unix
boundary accepted by ADR-0584, and exposes a production `RemoteDeviceIdentityPort` adapter for
remote-libvirt preparation.

The change does not add generic remote execution, SSH, caller-selected hosts or credentials,
volume creation, obligation persistence, reaping, or external-boot mutation behavior. It keeps
`acknowledge-takeover` and `execute-mutation` byte-compatible. Native ppc64le live testing is
excluded by campaign `00bf6369`.

## Architecture

The transport keeps its existing authenticated request envelope and adds operation
`resolve-device-identity` with a closed `device-identity-v1` request. The request contains only a
version discriminator and one normalized absolute path of at most 4,096 UTF-8 bytes. The authority
dispatcher authenticates the peer before calling a dedicated device-identity service; it never
passes the request to the external-boot mutation service.

The provider-host service delegates only to `HostStatDeviceIdentity`. Its response is one closed
value: `absent`, `inode` with unsigned 64-bit `st_dev` and `st_ino`, or `block` with unsigned
64-bit `st_rdev`. It emits no path or host detail. The existing authority listener remains the
single server endpoint, and the two existing operation schemas and dispatch paths do not change.
The dispatcher offloads the following `stat(2)` lookup from the event loop, and the existing
bounded server session owns the await. A stalled host filesystem can outlive the abandoned worker
thread, but it cannot retain the network session, delay other authority requests, or produce a late
response.

`RemoteAuthorityDeviceIdentity` is synchronous because ADR-0603's inspection port and libvirt
preparation path are synchronous. It receives only the Resource-bound `AuthorityRequestSender` and
the preparation's monotonic absolute deadline; `identity(path)` cannot select an endpoint, TLS
reference, authority identity, or credential. Immediately before each lookup it computes the
positive remainder from the captured preparation deadline. It enters one private event loop,
translates that remainder to the loop's absolute monotonic clock, and awaits the sender's typed
`resolve_device_identity` method. The sender then applies that single deadline across TCP connect,
TLS handshake, write, response read, and close. A zero or expired preparation budget fails before
sender or network use. Repeated identity calls always consume the same captured preparation
deadline. The adapter is valid only in the existing synchronous preparation worker context; if it
detects a running event loop in its calling thread, it fails with the same redacted infrastructure
error instead of nesting or blocking that loop.

Remote-libvirt composition exposes one factory that accepts only the already Resource-bound sender
and captured preparation deadline and returns the synchronous port. #2170 will inject that factory
into its server-preparation adapter; this issue does not invent that not-yet-landed preparation
lifecycle. An absent Resource authority sender yields no port, so callers fail closed before
storage mutation. Resource selection, TLS references, and active credential borrowing stay in
ADR-0606's existing worker composition; neither the wire request nor the identity adapter can
rebind them.

## Data contracts

The request is canonical JSON inside the existing length-prefixed envelope:

```json
{"credential":"…","operation":"resolve-device-identity","request":{"path":"/absolute/path","version":"device-identity-v1"}}
```

The successful response's `value` is exactly one of:

```json
{"kind":"absent","version":"device-identity-v1"}
{"kind":"inode","primary":1,"secondary":2,"version":"device-identity-v1"}
{"kind":"block","primary":3,"secondary":0,"version":"device-identity-v1"}
```

Boolean values are rejected even though Python treats them as integers. Identity components must
be in `0..2^64-1`; block `secondary` must be zero. Unknown fields, discriminators, versions,
non-canonical JSON, oversized frames, invalid UTF-8, non-absolute paths, NUL, and paths over 4,096
encoded bytes are rejected. The client validates the response independently of the server.

## Error contract

The host maps missing paths to `absent`. Malformed request/result state returns a closed
`invalid-request` transport category. Authentication failure remains `unauthenticated`; an
unconfigured identity service returns `provider-not-configured`; host lookup failure returns
`provider-failure`. These categories and all local TLS/socket/timeout failures are translated by
the client to `CategorizedError("remote device identity lookup failed",
INFRASTRUCTURE_FAILURE)`. A well-formed `absent` or malformed success value is translated to the
redacted `CONFLICT` behavior already enforced by ADR-0603's inspector. No exception contains the
path, socket, authority instance, credential, TLS filename, remote output, or underlying error.

Cancellation and `BaseException` still close the socket. Close uses only the remaining deadline;
failure to complete it does not replace an already-produced identity, but no new blocking wait is
allowed after the budget expires.

## Threat model

### Boundaries

- Added: an authenticated worker sends a path to the provider-host authority. The path is under the
  caller's control after remote-libvirt validation. Both client and server require the closed
  absolute-path shape and size bound; the provider host executes only following `stat(2)`.
- Added: an authenticated provider-host response reaches the worker. The server is trusted to
  observe its own filesystem but its bytes are treated as malformed until strict canonical shape,
  discriminator, integer, and bound validation completes.
- Existing widened: the mutual-TLS AF_UNIX listener accepts a third operation. Existing TLS 1.3,
  client-certificate authentication, request credential authentication, frame bound, session
  timeout, and socket ownership/ACL checks remain unchanged.
- Existing widened: a provider-host path reaches following `stat(2)`. Closed path validation occurs
  before dispatch; the lookup is offloaded from the authority event loop and its response await is
  bounded by the existing session timeout. The abandoned thread may remain kernel-blocked, but it
  owns no socket writer or request credential and cannot publish after timeout.
- Existing used: ADR-0606's Resource-bound sender supplies fixed destination selection, call-local
  TLS material, and active-incarnation credential borrowing. #2170 preparation composition supplies
  only that sender and the enclosing deadline; no request or path can select another destination or
  credential.

### Actors and controls

An authenticated but stale or compromised worker can request identity only for one bounded absolute
path per frame; it cannot request commands, arguments, environment, a network destination, or
credential. A peer without both the TLS identity and request credential is rejected before lookup.
A compromised authority peer could return malformed bytes; independent client decoding rejects
them without disclosure or mutation. The bounded deadline contains silent or slow peers.

Generic filesystem confidentiality beyond redacted errors is out of scope: an authenticated caller
receives only opaque identity or absence, never metadata or content. Preventing trusted inventory
from binding a Resource to the wrong authority remains deployment ownership, not request-time host
selection. External-boot mutation authorization remains governed by ADR-0584 and is unchanged.

## Verification

- Existing takeover and mutation request encoding/dispatch tests remain byte-compatible and pass.
- Protocol tests reject wrong versions, unknown fields, bool/integer confusion, overflow, invalid
  block secondary, path violations, excessive nesting, and malformed success/error responses.
- Host tests prove symlink and hard-link alias equality, distinct files differ, duplicate block-node
  aliases match, and distinct block devices differ. Bind-mount equality is exercised when the host
  permits an unprivileged mount namespace and otherwise records a precise skip.
- Transport tests prove unauthenticated and unconfigured requests never call `stat`, operational
  lookup failures are redacted, a blocked host lookup cannot block the event loop or publish after
  session timeout, and stalled connect/read/close stages cannot exceed one absolute deadline.
- Adapter tests prove an active event loop in the calling thread fails closed without starting a
  nested loop or sending a request.
- Composition tests prove a missing Resource-bound sender returns no port and configured
  construction retains only that sender plus the captured preparation deadline.
- Run focused tests, `just lint`, `just type`, and bare `just ci` with blocking stream redirection.

## Rollback

A normal revert removes the additive operation, client, service, and composition factory. Existing
external-boot operations keep their prior schemas and behavior; there is no persisted data or
migration to unwind.
