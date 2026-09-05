# Commit module-attempt intent before worker volume creation

Issue: [#2251](https://github.com/randomparity/kdive/issues/2251), part of #2170.
Governing decisions: ADR-0588 (durable intent precedes every attempt volume) and ADR-0605
(server-committed receipt with worker read-only verification).

## Goal

Bridge ADR-0588's server-owned durable obligation to the worker that will create remote-module
volumes. The server returns a dispatchable preparation request only after the obligation commit;
the worker independently confirms that exact obligation through its read-only database authority
before the synchronous libvirt helper can receive an authorization value.

## Existing boundary

`RemoteModuleAttemptObligationRepository.open_mutation_obligation` inserts one row keyed by
`(system_id, run_id, operation_nonce)`. Migration 0126 grants insert/update only to `kdive_server`
and grants `kdive_worker` select-only access. Callers currently own transactions, and no production
caller bridges this asynchronous server operation to worker-side volume preparation.

The bridge must not add a job kind or decide orchestration owned by #2173. It supplies a nested,
canonical request value which that future payload can carry unchanged. Volume naming, readback,
wire framing, and libvirt sequencing remain #2170's responsibility.

## Contract values

`ModuleAttemptObligationReceiptV1` is a frozen, strict, closed Pydantic value with exactly:

- `schema = "module-attempt-obligation-receipt-v1"`;
- canonical UUID `system_id` and `run_id` values; and
- `operation_nonce`, exactly 32 lowercase hexadecimal characters.

`ModuleAttemptPreparationRequestV1` is the equally closed envelope with
`schema = "module-attempt-preparation-request-v1"` and one
`module_attempt_obligation` receipt field. Both values serialize as compact sorted UTF-8 JSON,
are bounded to 4,096 bytes, and reject alternate encodings on canonical decode. The bound is far
above the fixed-size document but closes the future decoder before it becomes a durable payload.

The receipt is evidence to look up, not a bearer capability. It contains no credential, host,
path, or operator identifier. Possessing or constructing one never authorizes volume creation by
itself; only a successful read of the matching open row does.

## Server commit service

`open_module_attempt_preparation(pool, repository, attempt)` owns a top-level transaction on a
server-role pool. It calls the existing idempotent open operation, then requires the resulting row
to exist with `mutation_discharged_at IS NULL`. A replay against the same open row succeeds and
returns the same request. A replay after discharge fails rather than reviving or describing a
completed obligation.

The function constructs and returns the request only after leaving the transaction context. Thus
the pool has committed before the caller can receive anything dispatchable. Insert, validation,
commit, or readback failure propagates and produces no request. A committed row with no volume is
the expected resumable residue already accepted by ADR-0588.

## Worker verification service

`verify_module_attempt_preparation(pool, repository, request, expected_attempt)` first requires the
receipt tuple to equal the attempt selected by the worker's enclosing operation, then reads through
a worker-role pool and requires that exact row's mutation obligation to remain open. Missing,
mismatched, discharged, or unreadable state fails with one redacted
`ModuleAttemptObligationVerificationError`; database details and tuple values are not copied into
the error.

Success returns `VerifiedModuleAttemptAuthorization`, a separate immutable type containing the
exact attempt. Its constructor requires a module-private witness, so ordinary callers cannot
accidentally substitute a boolean, callback, coroutine, request, or raw receipt. #2170's
synchronous helper will accept only this type and use its attempt tuple for both volume names.
The type prevents accidental bypass; the database read is the authority check.

Exactly one verification authorizes the helper's single operation that creates both source and
scratch volumes. #2170 will place that call before its first lookup/create sequence. This issue
does not implement or call libvirt.

## Failure and replay behavior

- Strict or canonical decoding rejects unknown fields, wrong schema versions, wrong scalar types,
  malformed UUIDs/nonces, duplicate-key-derived alternate documents, trailing whitespace, and
  noncanonical key ordering before repository access.
- The server never returns a request when opening or committing fails.
- The server returns an equivalent request when the exact obligation is already open.
- A discharged row is terminal for this receipt; neither server replay nor worker verification
  reopens it.
- The worker exposes one redacted verification error for missing, mismatched, discharged, or
  unreadable state.
- A request for another attempt is rejected against the enclosing operation's expected tuple
  before repository access.

## Threat model

### Boundaries

This design adds two internal trust crossings. A server-generated value crosses the durable job
boundary toward a worker, and worker-controlled payload bytes cross the strict decoder before a
read-only database lookup. The existing server-write/worker-read database boundary is used but not
widened.

### Actors

An authenticated tenant can influence the Run/System selected by upstream orchestration but does
not hold either database role. A compromised or defective worker can alter its payload and issue
queries using `kdive_worker`; it must not gain obligation-write authority or turn a forged receipt
into storage-mutation authority. The deployment-operated server is trusted to open obligations for
admitted work.

### Controls

- Closed, strict, versioned canonical models reject payload shape substitution and ambiguity.
- The receipt binds all three attempt fields; the verifier compares and queries all three exactly.
- Verification requires an open committed row, not receipt possession.
- Migration 0126 remains the enforcement for server write and worker read-only privileges; this
  change adds no grant or migration.
- The server transaction exits before returning the request, making commit a prerequisite for
  dispatch.
- Errors omit tuple values and database text.
- The synchronous provider boundary receives only the verified authorization type.

### Out of scope

This design does not protect against a process that has already compromised server database
credentials, nor does it add cryptographic signing inside one trusted deployment. Provider-host
authentication is #2250. Job registration/orchestration is #2173. Terminal discharge and reaping
remain #2172 and #2168.

## Testing

1. Pure model tests prove deterministic canonical round trips and reject unknown fields, wrong
   versions/types, malformed identifiers, alternate ordering, trailing bytes, and oversize input.
2. A real-Postgres server test opens through a `kdive_server` login and observes the row from a
   separate connection after the service returns, proving commit-before-request.
3. An injected persistence failure produces no request, while a replay of an open row returns the
   same canonical bytes and a discharged replay fails.
4. A real `kdive_worker` login verifies an open row and cannot insert, update, discharge, or reopen
   the obligation.
5. Missing, mismatched, discharged, and unreadable rows fail verification with the same redacted
   error before any supplied volume-operation probe can run.
6. A focused composition test passes one verified authorization to a two-create probe and proves
   a raw request, receipt, boolean, callback, or coroutine is rejected by the helper's typed gate.
   #2170 owns the final libvirt implementation of that gate.

The new tests run on the ordinary disposable-Postgres tier. Native ppc64le live tests are excluded
by the campaign and no live provider is needed for this contract-only prerequisite.

## Considered approaches

- **Return the receipt from an uncommitted caller-owned transaction.** Rejected: a caller could
  enqueue before commit, and commit failure would leave a worker holding evidence for no row.
- **Let the worker open the obligation.** Rejected: migration 0126 deliberately grants the worker
  read-only access; widening it would collapse server admission and worker execution authority.
- **Sign the receipt and skip the database read.** Rejected: signature validity cannot show that
  the obligation remains open after discharge, adds key lifecycle, and duplicates the database
  authority already required by the reaper.
- **Pass a callback or boolean into the synchronous helper.** Rejected: neither proves an
  asynchronous transaction committed, and both are easy to invoke or forge at the wrong point.
- **Do nothing until #2173 adds a job payload.** Rejected: #2173 would then have to invent the
  cross-role contract while also orchestrating it, recreating the scope collision that split
  #2251 from #2170.
