# Authenticated remote-host device identity implementation plan

Goal: implement ADR-0603's authenticated, deadline-enforcing remote-host device identity adapter
over ADR-0606's existing Resource-bound authority route.

Architecture: one additive closed operation extends ADR-0606's existing Resource-bound typed sender
and mutual-TLS request envelope. A provider-host service performs only `HostStatDeviceIdentity`; a
synchronous port adapter awaits that sender through a private event loop and consumes one captured
monotonic preparation deadline. Remote-libvirt composition exposes the port factory consumed later
by #2170.

Tech stack: Python 3.14, ADR-0606 asyncio transport, Pydantic, pytest, uv.

Expected implementation size: 650–950 changed lines (L) — closed models, bounded client/server
dispatch, composition seam, and focused trust-boundary and real-host alias tests.

## Global constraints

- Python 3.14; x86_64 host; project targets x86_64 and ppc64le; no new dependency or migration.
- Preserve byte compatibility and behavior for `acknowledge-takeover` and `execute-mutation`.
- ADR-0606's fixed Resource-bound sender owns endpoint selection, call-local TLS materialization,
  and active credential borrowing; caller input cannot select a destination, credential, command,
  argument, or env.
- Request paths are normalized absolute UTF-8, NUL-free, and at most 4,096 encoded bytes.
- Identity components are strict non-boolean unsigned 64-bit integers; block secondary is zero.
- Every potentially blocking client stage consumes the same positive monotonic absolute deadline.
- Provider-host identity lookup uses one service-owned four-worker executor; nonblocking admission
  stays consumed until the underlying future actually completes, and shutdown never waits for a
  blocked filesystem call.
- Errors and durable state contain no path, host, URI, credential, TLS path, or provider output.
- Native ppc64le live tests are excluded by campaign `00bf6369`.
- Guardrails: focused pytest; `just lint`; `just type`; `just ci > <private-file> 2>&1 < /dev/null`.

## Task 1: Closed identity request and response models

Files:

- Create `src/kdive/providers/external_boot_authority/device_identity.py` for versioned request,
  response, strict decoding, the synchronous port adapter, and redacted error translation.
- Create `tests/providers/external_boot_authority/test_device_identity.py`.

Interfaces:

- `DeviceIdentityRequestV1(path: str, version="device-identity-v1")`.
- `DeviceIdentityAbsentV1`, `DeviceIdentityInodeV1`, and `DeviceIdentityBlockV1` as a strict
  discriminated response union.
- `decode_device_identity_request(payload: bytes) -> DeviceIdentityRequestV1` and
  `decode_device_identity_response(payload: bytes) -> DeviceIdentityResponseV1`.
- Later tasks consume the same models; no existing authority protocol model changes.

Steps:

1. Add tests for canonical round trips and every path, discriminator, field, integer, and size
   rejection; run `uv run python -m pytest tests/providers/external_boot_authority/test_device_identity.py -q`
   and expect import failures.
2. Implement the minimum closed strict models and canonical decoders; rerun and expect pass.
3. Run `just lint` and `just type`; expect exit 0, then commit.

Acceptance: only the three documented result shapes and one bounded request shape decode.

## Task 2: Additive authenticated server operation

Files:

- Modify `src/kdive/providers/external_boot_authority/transport.py`.
- Modify `src/kdive/providers/external_boot_authority/host.py` only if host assembly needs to supply
  the identity service independently of the mutation service.
- Modify `tests/providers/external_boot_authority/test_transport.py` and
  `tests/providers/external_boot_authority/test_host.py`.

Interfaces:

- Extend `Operation` with `resolve-device-identity` without changing existing variants.
- Extend `AuthorityService` with an independently optional identity service port, not a mutation
  method; `_dispatch` authenticates before either service.
- `resolve_device_identity(peer, request) -> DeviceIdentityResponseV1` delegates only to
  `HostStatDeviceIdentity.identity` in an offloaded thread and returns redacted closed categories.
- `RemoteDeviceIdentityService` owns a four-worker executor, four completion-owned admission slots,
  `resolve(request)`, and non-waiting `close()`; AF_UNIX and network listeners share that one service.

Steps:

1. Add compatibility tests for the exact existing operations and tests proving authentication and
   service configuration precede identity lookup; run the two focused files and expect failures.
2. Add the operation decoder/encoder and separately typed identity-service dispatch while leaving
   both existing branches unchanged.
3. Implement the dedicated four-worker executor and acquire its four-slot gate without waiting
   before submission. Attach release to the concurrent future's actual completion, shield it from
   waiter cancellation, and return `provider-failure` immediately on exhaustion.
4. Own one service across both listeners in `run_authority_host`; close admission, cancel only
   queued work, and invoke executor shutdown without waiting during host cleanup.
5. Add real-host symlink/hard-link and available bind/block alias tests around the host service,
   plus a blocked-lookup regression proving the event loop stays responsive and no late response is
   published after session timeout.
6. Add repeated-cancellation, exact-cap exhaustion, actual-completion recovery, non-waiting
   shutdown, new-work rejection, and default-executor readiness-isolation regressions.
7. Rerun focused tests, lint, and type; expect exit 0, then commit.

Acceptance: the third operation is authenticated, bounded, redacted, and cannot affect either
existing mutation operation.

## Task 3: One-deadline synchronous client and composition

Files:

- Modify `src/kdive/providers/external_boot_authority/device_identity.py`.
- Modify `src/kdive/providers/remote_libvirt/composition.py`.
- Modify `tests/providers/external_boot_authority/test_device_identity.py` and
  `tests/providers/remote_libvirt/test_composition.py`.

Interfaces:

- `AuthorityRequestSender.resolve_device_identity(request, *, deadline)` is the only async wire
  entry point and uses ADR-0606's private transport.
- `RemoteAuthorityDeviceIdentity(sender, preparation_deadline).identity(path: str) ->
  RemoteDeviceIdentity | None` conforms to `RemoteDeviceIdentityPort`; every call recomputes the
  positive remainder from the same captured deadline and translates it once to a private event
  loop's absolute clock.
- `build_remote_device_identity_port(sender, preparation_deadline)` returns no port when the
  Resource has no authority sender and otherwise returns the configured adapter; #2170 consumes it
  in preparation.

Steps:

1. Add controlled sender/transport tests for expired preparation deadline, stalled connection,
   stalled response, malformed response, absence, identity success, operational failure, and
   unconditional cleanup;
   run focused tests and expect missing behavior.
2. Implement one monotonic remaining-budget helper at the synchronous adapter boundary, translate
   only that remainder to the private loop's absolute clock, and rely on ADR-0606's transport to
   apply the resulting deadline across connect, TLS, frame write/read, and close.
3. Implement independent response validation and redacted `CategorizedError` translation.
4. Add composition tests for configured and unconfigured construction and prove the adapter retains
   only the Resource-bound sender and captured deadline, with no per-call endpoint or credential
   selection.
5. Add a protocol-conformance type test and an integrated multi-lookup test proving every lookup
   consumes the same captured enclosing deadline rather than resetting a per-call duration.
6. Add a calling-thread active-event-loop regression; fail closed without nesting or blocking that
   loop and without sending a request.
7. Rerun focused tests, lint, and type; expect exit 0, then commit.

Acceptance: every blocking stage shares one deadline, malformed peers fail closed, and composition
exposes only a fixed configured port.

## Task 4: Integrated verification

Files: no planned new surface; corrections remain within files named above.

Steps:

1. Run all external-authority and remote-module attachment tests together; expect exit 0.
2. Run `just lint` and `just type`; expect exit 0.
3. Run `just ci > <private-file> 2>&1 < /dev/null`; expect exit 0.
4. Inspect `git diff main...HEAD` for scope, redaction, compatibility, and deadline correctness;
   correct only in-scope defects and rerun affected gates.

Acceptance: focused tests and the complete repository gate pass with no out-of-scope changes.
