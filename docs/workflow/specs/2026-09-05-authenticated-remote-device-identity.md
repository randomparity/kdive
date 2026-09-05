# Authenticated remote-host device identity transport

## Scope and governing decisions

Issue #2250 supplies ADR-0603's remote-host implementation. ADR-0604 selects the additive client
and transport shape over the mutual-TLS Unix authority boundary accepted by ADR-0584 and exposes a
production `RemoteDeviceIdentityPort` adapter for remote-libvirt preparation.

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

`RemoteAuthorityDeviceIdentity` is a synchronous adapter because ADR-0603's inspection port and
libvirt preparation path are synchronous. It opens a mutual-TLS AF_UNIX connection using one
injected, per-preparation client configuration. The configuration contains a fixed socket,
authority server name, TLS CA/certificate/key paths, the active incarnation request credential, and
the preparation's monotonic absolute deadline; `identity(path)` cannot select any of them. The
captured deadline is converted to the positive remaining budget immediately before every
potentially blocking connect, TLS, write, read, and
orderly-close operation. A zero or expired budget fails before connection.

Remote-libvirt composition exposes one factory that accepts only the validated per-preparation
client configuration and returns the port. #2170 will inject that factory into its
server-preparation adapter; this issue
does not invent that not-yet-landed preparation lifecycle. An absent client configuration yields no
port, so callers fail closed before storage mutation. Resource selection stays in the existing
remote-libvirt `config_factory`; neither the wire request nor the identity client can rebind it.

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
- Existing used: injected per-preparation client configuration supplies destination, TLS material,
  active incarnation credential, and deadline. Trusted deployment and #2170 preparation composition
  own it; no request or path can select another destination or credential.

### Actors and controls

An authenticated but stale or compromised worker can request identity only for one bounded absolute
path per frame; it cannot request commands, arguments, environment, a network destination, or
credential. A peer without both the TLS identity and request credential is rejected before lookup.
A compromised authority peer could return malformed bytes; independent client decoding rejects
them without disclosure or mutation. The bounded deadline contains silent or slow peers.

Generic filesystem confidentiality beyond redacted errors is out of scope: an authenticated caller
receives only opaque identity or absence, never metadata or content. Preventing a trusted deployment
from injecting the wrong fixed client configuration is deployment ownership, not request-time host
selection. External-boot mutation authorization remains governed by ADR-0584 and is unchanged.

## Verification

- Existing takeover and mutation request encoding/dispatch tests remain byte-compatible and pass.
- Protocol tests reject wrong versions, unknown fields, bool/integer confusion, overflow, invalid
  block secondary, path violations, excessive nesting, and malformed success/error responses.
- Host tests prove symlink and hard-link alias equality, distinct files differ, duplicate block-node
  aliases match, and distinct block devices differ. Bind-mount equality is exercised when the host
  permits an unprivileged mount namespace and otherwise records a precise skip.
- Transport tests prove unauthenticated and unconfigured requests never call `stat`, operational
  lookup failures are redacted, and stalled connect/read/close stages cannot exceed one absolute
  deadline.
- Composition tests prove missing configuration returns no port and configured construction binds
  only the supplied fixed endpoint and credentials.
- Run focused tests, `just lint`, `just type`, and bare `just ci` with blocking stream redirection.

## Rollback

A normal revert removes the additive operation, client, service, and composition factory. Existing
external-boot operations keep their prior schemas and behavior; there is no persisted data or
migration to unwind.
