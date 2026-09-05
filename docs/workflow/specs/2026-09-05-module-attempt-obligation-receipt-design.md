# Commit module-attempt intent before worker volume creation

Issue: [#2251](https://github.com/randomparity/kdive/issues/2251), part of #2170.
Governing decisions: ADR-0588 (durable intent precedes every attempt volume) and ADR-0605
(server-committed receipt with worker read-only verification).

## Goal

Bridge ADR-0588's server-owned durable obligation to the worker that will create remote-module
volumes. The server returns a dispatchable preparation request only after the obligation commit;
the worker independently confirms that exact obligation through its read-only database authority
and awaits the bounded source-and-scratch consumer while the verification lock remains held.

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

`run_verified_module_attempt_preparation(pool, repository, request, expected_attempt, consumer)`
opens a worker-role transaction, acquires the transaction-scoped advisory lock for
`expected_attempt.system_id`, requires the receipt tuple to equal the enclosing operation's
expected attempt, and requires that exact row's mutation obligation to remain open. Only then does
the service call and await `consumer(expected_attempt)`. It returns the consumer's result only
after the awaited call finishes and the transaction exits. Missing, mismatched, discharged, or
unreadable state fails with one redacted `ModuleAttemptObligationVerificationError`; database
details and tuple values are not copied into the error.

The consumer is one asynchronous, bounded operation supplied by #2170. It receives only the
validated `ModuleAttempt` tuple, performs the source and scratch lookup/create sequence inline, and
must not detach work into a task that can outlive its return. The service exposes no verified token,
context manager, witness, or unwrapping gate. A retained attempt tuple is data rather than proof and
cannot re-enter the consuming seam without a new database verification. This issue supplies the
generic awaited seam and test consumer but does not implement or call libvirt.

`RemoteModuleAttemptObligationRepository.discharge_mutation_obligation` acquires the same
transaction-scoped System advisory lock before updating the row. The method already owns the only
production mutation-discharge operation; putting the fence there makes every current and future
caller participate without a cross-issue calling convention. A concurrent discharge waits until
the verification-owned consumer and both creates finish. Callers continue to own the transaction,
so the repository-acquired lock survives through their commit or rollback.

The production boundary is repository-mediated access. Direct SQL by a process already holding
the server database credential can bypass application invariants and is outside this contract, as
it is for the repository's state validation generally. A source inventory regression rejects any
production mutation-discharge SQL outside this repository module. Foreign-key cascade remains a
deployment-owned database operation; normal System teardown changes durable state and uses the
repository rather than deleting System rows.

## Failure and replay behavior

- Strict or canonical decoding rejects unknown fields, wrong schema versions, wrong scalar types,
  malformed UUIDs/nonces, duplicate-key-derived alternate documents, trailing whitespace, and
  noncanonical key ordering before repository access.
- The server never returns a request when opening or committing fails.
- The server returns an equivalent request when the exact obligation is already open.
- A discharged row is terminal for this receipt; neither server replay nor worker verification
  reopens it.
- The worker exposes one redacted verification error for missing, mismatched, discharged, or
  unreadable state and releases its transaction/lock on every exit.
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
- The existing System advisory lock spans verification and the complete awaited consumer; the
  mutation-discharge repository method takes the same lock automatically.
- Migration 0126 remains the enforcement for server write and worker read-only privileges; this
  change adds no grant or migration.
- The server transaction exits before returning the request, making commit a prerequisite for
  dispatch.
- Errors omit tuple values and database text.
- No verified capability leaves the service; the consumer receives only the validated attempt data
  while the service retains the lock.

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
6. A concurrent call to the ordinary mutation-discharge repository method remains blocked while
   the verification-owned two-create probe runs, then proceeds after the service returns.
   Rollback, consumer exception, and task cancellation release the worker-held lock.
7. Composition tests deliberately retain the consumer's `ModuleAttempt` argument and prove that it
   cannot authorize a post-return operation. The service directly awaits the two-create probe and
   creates no task of its own; cancellation reaches that inline probe and exposes no reusable
   authorization value.
8. A source inventory test rejects production mutation-discharge SQL outside the repository module
   and proves every production caller reaches the self-fencing method.

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
- **Pass a callback or boolean as proof into the synchronous helper.** Rejected: neither proves an
  asynchronous transaction committed, and both are easy to invoke or forge at the wrong point.
  The chosen consumer is not proof: the verification service invokes it only after the database
  check and while retaining the lock.
- **Release verification state before calling the helper.** Rejected: a concurrent terminal path
  could discharge the row before either create; the existing per-System serialization lock must
  cover both the check and its consuming operation.
- **Yield an immutable authorization from a lock-holding context.** Rejected: the value remains
  type-valid after context exit, rollback, or cancellation, so a caller could reuse it after the
  lock is released. Service-owned invocation exposes no such value.
- **Use a revocable lease token.** Rejected: invalidation and per-entry checks introduce mutable
  state and race surface that a lexical, awaited consumer call avoids.
- **Use an exact PostgreSQL row lock.** Rejected: all four row-lock modes require authority the
  existing select-only worker login does not hold. A narrowly granted function would add schema
  and permission surface; the self-fencing repository method uses the existing advisory mechanism.
- **Do nothing until #2173 adds a job payload.** Rejected: #2173 would then have to invent the
  cross-role contract while also orchestrating it, recreating the scope collision that split
  #2251 from #2170.
