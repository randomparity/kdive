# Fence capture artifact publication — design (#1952)

- **Architecture:** [ADR-0559](../../adr/0559-fence-capture-artifact-publication.md)
- **Depends on:** merged #1951 and accepted ADR-0558
- **Base branch:** `main`
- **Implementation branch:** `feat/fence-capture-publication-1952`
- **Guardrails:** focused pytest during TDD; `just ci` before each implementation/review commit

## Outcome and scope

Only the current, credential-fenced supervised capture attempt may publish its pcap. Cancellation
or worker-session loss either prevents publication or removes an unregistered object before the
attempt becomes terminal. A reaper can distinguish positive publication closure from a missing
or stale acknowledgment.

This change owns capture-operation publication persistence, handler ordering, compensation,
recovery, the publication half of the historical cutoff, and fault tests. Provider supervision is
settled by #1951. Candidate selection and reclamation remain #1946; provider reapers remain #1947
and #1948; succeeded-row cleanup-result mechanics remain #1949. The MCP tool, artifact bytes,
retention, sensitivity, and agent-facing schema do not change.

## Approaches

1. **One supervised operation with a durable publication phase (selected).** Keep the job fence
   across provider execution and publication, journal the deterministic key before PUT, and make
   terminal operation state depend on publication closure. This gives retry and reaping one
   authoritative predicate without holding the Run lock during I/O.
2. **A separate publication object.** This makes publication independently addressable, but adds a
   second one-to-one lifecycle and another current-link invariant without enabling independent
   work. The capture operation already owns the exact attempt.
3. **Compensation without durable phase state.** This is smaller in code but cannot recover a
   worker crash between PUT and compensation, which is the failure the issue requires closing.

## Durable model and invariants

Migration `0113_capture_publication_fence.sql` extends `capture_operations`:

- `state` adds `publishing`; `exited` remains the sole terminal operation state;
- `publication_outcome` is null before publication and then `published` or `discarded`;
- `publication_object_key` is set before PUT and is immutable;
- `publication_etag` is set after PUT and before registration when PUT returned;
- `publication_artifact_id` references the committed artifact row only for `published`;
- `publication_started_at` and `publication_closed_at` use the database clock.

The row checks require publication fields to form one of four complete shapes: not started,
started/no PUT result, PUT returned, or terminal. `exited` requires positive process/provider
quiescence and a terminal publication outcome. `published` requires a matching artifact id, key,
and etag; `discarded` requires no artifact id. Immutable-key and exact-attempt validation live in
security-definer transition functions rather than direct worker table grants.

The existing `capture_operation_cutoff` gains non-null booleans `publication_closed` and
`complete`. Migration takes the protocol advisory fence, locks the singleton, verifies protocol 3
and `operation_quiescent`, verifies all pre-protocol workers remain positively terminated, then
sets `publication_closed = true` and `complete = true` in one transaction. It does not resample
`cutoff_at`; historical eligibility remains bounded by the database-clock instant at which legacy
provider work became quiescent. A failed assertion rolls the migration back; the recovery action
is to terminate the named legacy authority and rerun migration.

## Runtime flow

`CaptureOperationSupervisor.execute` accepts an injected async publisher. It retains the
session-level job fence and authority monitor through these ordered steps:

1. Launch, run, terminate, and prove provider quiescence as in ADR-0558.
2. Transition the exact current operation from `running` to `publishing`, persisting provider-exit
   evidence and the deterministic object key before any PUT.
3. Invoke the publisher. The publisher checks for an existing committed row under the Run lock.
   A sequential replay adopts that row through the publication commit transition and performs no
   PUT.
4. Start the blocking PUT outside the Run lock. Authority loss cancels the async publisher but
   drains the owned thread task so the store call cannot later return unnoticed.
5. Journal the PUT's key and etag through a credential-fenced transition. Under the Run lock,
   revalidate exact current-attempt authority and atomically claim the artifact row, write the
   audit event, and close publication as `published`.
6. Return the artifact id only after publication is durably terminal. Queue success then clears
   the current-operation link using its existing exact-attempt fence.

Every worker transition derives the incarnation from the credential and requires an active
protocol-3 worker, the matching operation owner, the job's unchanged attempt/current link, and a
job state that still permits this attempt. Idempotent replay accepts byte-identical facts and
rejects conflicting facts.

## Cancellation and recovery

Authority monitoring races publication against the lock-owning connection. If cancellation wins,
the publisher is canceled, any PUT thread is drained, and cleanup uses the operation's durable
key. Under the Run lock it first checks for a committed artifact row:

- a matching committed row closes the operation as `published`; the job result is not rewritten;
- no row causes object deletion followed by an absence check, then closes as `discarded`;
- a conflicting row, failed delete, failed absence proof, unavailable store, or database failure
  leaves the operation in `publishing` for startup recovery.

Replacement recovery runs before readiness. For an operation with provider quiescence but open
publication it repeats the same row-first decision. It never resumes a PUT from buffered packet
bytes: if registration did not commit, it removes the deterministic key and records `discarded`.
This is safe because the job cannot retry while the prior operation is nonterminal. Recovery does
not acknowledge cancellation or expose readiness until object/row publication is terminal.

Normal job cancellation and any future reaper take the same job advisory fence. Therefore neither
can pass the barrier while the live publisher owns it. Process death releases the fence, but the
durable nonterminal state still bars retry/reaping until recovery closes publication.

## Error handling and observability

Publication transition refusal is an infrastructure failure with operation id, job id, attempt,
and reason code; logs never include packet bytes, credentials, or object-store secrets. Cleanup
failure logs the operation and deterministic key only after the standard log redaction path.
Artifact claim conflicts preserve the existing specific error category and leave recoverable
state until compensation proves a terminal outcome. A cancellation is not acknowledged merely
because a delete request was sent: absence or a committed row is required.

No new duration or size limit is introduced. Existing capture limits retain their published
five-part contracts. The only waits are existing store client bounds and the worker's cancellation
drain; a store call that violates its configured bound keeps the operation nonterminal and bars
reaping, with worker restart/recovery as the action.

## Security and trust boundaries

This change does not add an external entry point, but it handles tenant-sensitive packet bytes and
crosses the worker/PostgreSQL/object-store boundary.

- **Authenticated tenant:** can request a bounded capture through the existing MCP authorization
  path. It cannot supply the object key, operation id, or publication transition facts.
- **Worker process:** is trusted to hold a short-lived credential but not to mutate operation rows
  directly. Security-definer functions derive worker identity from the credential and validate the
  exact current attempt and state transition.
- **Object store:** is an external dependency that may delay, fail, or return conflicting
  metadata. The deterministic server-derived key, sensitivity metadata, size bounds, etag journal,
  row-first recovery, and verified deletion bound its effects.
- **PostgreSQL:** is the authority for job ownership, current attempt, artifact metadata, and
  publication closure. Advisory fences and transactions serialize decisions; object I/O remains
  outside transactions.

Out of scope are compromise of the worker host or database administrator, object-store behavior
that violates its API contract after confirmed deletion, changing artifact encryption, and adding
provider families. Existing secret-reference and redaction rules remain in force.

## Testing

Database tests prove every state shape, credential/current-attempt fence, idempotent replay,
conflicting replay refusal, nonterminal retry exclusion, and migration cutoff assertion. Handler
and supervisor tests inject pauses before PUT, during PUT, after PUT journaling, before artifact
claim, during audit, and after row commit. At each pause they race cancellation or authority loss
and assert exactly one terminal result: matching artifact row plus object and `published`, or no
row plus no object and `discarded`.

Crash-recovery tests seed every durable boundary and prove replacement recovery never invents an
artifact row, never deletes a registered object, and does not report readiness while cleanup is
unproven. Concurrent-attempt tests prove the second attempt cannot PUT until the first publication
is terminal. Tests deliberately break transition validation and compensation to show the new
assertions fail before restoring the implementation. `just ci` is the final local gate on both
declared target architectures through architecture-independent Python and PostgreSQL behavior;
no live provider or MCP-contract change is required.
