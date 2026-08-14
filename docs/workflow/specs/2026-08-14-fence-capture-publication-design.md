# Fence capture artifact publication — design (#1952)

- **Architecture:** [ADR-0559](../../adr/0559-fence-capture-artifact-publication.md)
- **Depends on:** merged #1951 and accepted ADR-0558
- **Recovery-policy approval:** operator decision on 2026-08-14 after the bounded spec review
- **Base branch:** `main`
- **Implementation branch:** `feat/fence-capture-publication-1952`
- **Guardrails:** focused pytest during TDD; `just ci` before each implementation/review commit

## Outcome and scope

Only the current, credential-fenced supervised capture attempt may publish its pcap. Cancellation
or worker-session loss either prevents publication or removes an unregistered object before the
attempt becomes terminal. A reaper can distinguish positive publication closure from a missing
or stale acknowledgment.

This change owns fresh-install capture-operation publication persistence, handler ordering,
compensation, recovery, combined cutoff state, and fault tests. Provider supervision is
settled by #1951. Candidate selection and reclamation remain #1946; provider reapers remain #1947
and #1948; succeeded-row cleanup-result mechanics remain #1949. The MCP tool, artifact bytes,
retention, sensitivity, and agent-facing schema do not change. Existing protocol-3 data, workers,
jobs, operations, objects, and in-flight work are not migrated, preserved, reconciled, or cleaned
up; deployment uses a new database and object-store namespace.

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

- ADR-0558's `state` is unchanged and `exited` remains its terminal provider-operation state;
- `publication_state` is `pending`, `publishing`, `canceling`, `published`, or `discarded`;
- `publication_object_key` includes the durable operation id, is set before PUT, and is immutable;
- `publication_etag` is set after PUT and before registration when PUT returned;
- `publication_artifact_id` references the committed artifact row only for `published`;
- `cleanup_capture_version_id` journals a verified capture version before compensation deletes it;
- `publication_tombstone_version` records the retained zero-byte fence for `discarded`;
- `spool_disposed_at` records verified removal of the exact attempt's private packet spool;
- `publication_started_at` and `publication_closed_at` use the database clock.

The row checks require publication fields to form complete pending, publishing, canceling, or
terminal shapes. Provider `exited` requires positive process/provider quiescence; job completion,
retry, cancellation acknowledgment, worker readiness, and later reclamation require the combined
state `(exited, published|discarded, spool_disposed)`. `published` requires a matching artifact id,
key, and etag;
`discarded` requires no artifact id and a verified operation-identity tombstone version.
Immutable-key and
exact-attempt validation live in security-definer transition functions rather than direct worker
table grants.

The existing `capture_operation_cutoff` gains non-null booleans `publication_closed` and
`complete`. Migration raises the worker fence protocol from 3 to 4 only on an empty installation.
Under the protocol advisory fence it refuses any row in `worker_incarnations`, `jobs`,
`capture_operations`, or `artifacts`; replaces registration, authentication, and claim guards with
exact protocol 4; then sets cutoff protocol 4, `operation_quiescent = true`,
`publication_closed = true`, `complete = true`, and a fresh database-clock `cutoff_at` atomically.
A failed emptiness assertion rolls the migration back with the recovery action: provision a new
database and object-store namespace and deploy there. There is no data conversion, offline drain,
upgrade, export/import, or compatibility mode.

## Runtime flow

### Object-store admission

Worker startup extends the existing object-store versioning validation with a live conditional-
create probe. Two concurrent zero-byte `If-None-Match: *` requests target one random internal key;
exactly one must succeed and one must return the store's precondition-failed response. Startup then
HEADs the winner, deletes its immutable version, and verifies cleanup. Any other outcome, store
fault, or cleanup fault keeps the worker unready and names the configured endpoint plus the action
to provide a store with atomic conditional create under versioning and restart. Probe keys carry no
tenant data or credentials. This admission runs once per worker startup before recovery/readiness,
not once per capture.

The integration suite runs the probe against the supported live MinIO fixture and exercises the
same overlap through capture-versus-tombstone arbitration. A fake store covers malformed double-
success, double-failure, missing-version, and cleanup-failure responses. Production logic does not
infer this capability from an API name or bucket-versioning status alone.

`CaptureOperationSupervisor.execute` accepts an injected async publisher. It retains the
session-level job fence and authority monitor through these ordered steps:

1. Launch, run, terminate, and persist provider `exited` plus quiescence as in ADR-0558 while the
   publication state remains `pending`.
2. Transition the exact current operation from `pending` to `publishing`, persisting an
   operation-unique deterministic object key before any PUT. A later attempt receives another key.
3. Invoke the publisher. The publisher checks for an existing committed row under the Run lock.
   A sequential replay adopts that row through the publication commit transition and performs no
   PUT.
4. Start the blocking PUT outside the Run lock. Authority loss cancels the async publisher but
   starts draining the owned thread task. The PUT uses `If-None-Match: *` and operation-id metadata
   so an ambiguous response is resolved by the cancellation arbitration below rather than timing.
5. Journal the PUT's key and etag through a credential-fenced transition. Under the Run lock,
   revalidate exact current-attempt authority and atomically claim the artifact row, write the
   audit event, and close publication as `published`.
6. After publication is durably terminal, remove the exact operation's supervisor-owned packet
   spool directory and record `spool_disposed_at`. Return the artifact id only after both facts are
   durable. Queue success then clears the current-operation link using its exact-attempt fence.

Every worker transition derives the incarnation from the credential and requires an active
protocol-4 worker, the matching operation owner, the job's unchanged attempt/current link, and a
job state that still permits this attempt. Idempotent replay accepts byte-identical facts and
rejects conflicting facts.

## Cancellation and recovery

Authority monitoring races publication against the lock-owning connection. On session loss the
fence-owning task first cancels the publisher, prevents any later database transition on the dead
connection, and drains the PUT thread. It then uses a fresh connection to reacquire the released
job fence. Under the Run lock it revalidates the exact current attempt and atomically moves
`pending|publishing` to `canceling`; every publication-commit transition rejects that state. It
then releases the Run lock and cleans up the operation-unique key. A normal external cancellation
waits on the same job fence; its cancellation point is fence acquisition, so publication that
already committed while the live owner held the fence is a completed publication, not a canceled
attempt.

If PUT may still commit or its response was lost, recovery uses a storage-side serialization
point. It conditionally creates a zero-byte tombstone at the same operation-unique key with
`If-None-Match: *`. Capture and tombstone objects both carry the operation id plus a distinct,
server-derived `publication-kind` metadata value; a tombstone must also have size zero. The store
permits exactly one winner:
if the tombstone wins, the capture PUT cannot later overwrite it; if conditional creation reports
the key already exists, recovery HEADs it and verifies the operation and kind metadata. It deletes
only a verified `capture` version: under the Run lock it first persists that immutable version in
`cleanup_capture_version_id`, then deletes that exact version outside the lock, and verifies
`head(key, version_id)` reports it absent before retrying tombstone creation. A lost delete response
or crash therefore resumes against the same durable version id; current-key absence or a delete
marker is not accepted as proof. It adopts and retains an
existing verified zero-byte `tombstone` version without deleting or replacing it. Because the
operation issues only one capture PUT and
recovery never resumes it, observing its version or winning the tombstone resolves the only
possible stored capture object. Recovery verifies and retains the winning zero-byte tombstone and
records its immutable version. Retention is required because the supported S3 contract has no
durable request-completion receipt: deleting the tombstone could let a delayed conditional PUT
succeed later. The tombstone is publication-fence state owned by the operation, not an artifact
row or unregistered object.
Cleanup follows the row-first decision:

- a matching committed row closes the operation as `published`; the job result is not rewritten;
- no row requires completed conditional-create arbitration and a verified retained tombstone;
  cleanup then reacquires the Run lock and closes as `discarded` with its version;
- a conflicting row or a failure before `canceling` commits leaves the existing nonterminal state;
  a failed delete, tombstone proof, store call, or later database operation retains `canceling` for
  startup recovery. No transition moves `canceling` backward to `publishing`.
- an object whose operation id, `publication-kind`, or tombstone size does not match remains
  untouched and leaves the operation in `canceling`. Recovery emits the stable reason
  `capture_publication_object_identity_conflict` with operation id and key, keeps readiness and
  acknowledgment barred, and requires an operator to inspect and restore the expected exact object
  identity before restarting recovery. Automatic deletion, adoption, or metadata repair of an
  unverified object is forbidden.

Replacement recovery runs before readiness. For an operation with provider quiescence but open
publication it moves `pending|publishing` to `canceling`, or resumes arbitration directly from an
already-`canceling` row. It never resumes
a PUT from buffered packet bytes: if registration did not commit, it completes conditional-create
arbitration, removes the exact capture version, retains the winning tombstone, and records
`discarded`.
This is safe because the job cannot retry while the prior operation is nonterminal. Recovery does
not acknowledge cancellation or expose readiness until object/row publication is terminal and the
exact private spool is absent. Spool deletion happens after the publication decision so successful
bytes are never removed before their artifact row commits. A deletion fault leaves
`spool_disposed_at` null, keeps the current-operation link, and is retried by startup recovery;
absence of the exact operation directory is idempotent success.

Normal job cancellation and any future reaper take the same job advisory fence. Therefore neither
can pass the barrier while the live publisher owns it. Process death releases the fence, but the
durable nonterminal state still bars retry/reaping until recovery closes publication.

## Error handling and observability

Publication transition refusal is an infrastructure failure with operation id, job id, attempt,
and reason code; logs never include packet bytes, credentials, or object-store secrets. Cleanup
failure logs the operation and deterministic key only after the standard log redaction path.
Artifact claim conflicts preserve the existing specific error category and leave recoverable
state until compensation proves a terminal outcome. A cancellation is not acknowledged merely
because a delete request was sent: a retained operation tombstone or a committed row is required.

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
  metadata. The operation-unique server-derived key, sensitivity metadata, size bounds, etag
  journal, atomic conditional create, operation identity metadata, row-first recovery, conditional
  version deletion, retained fence tombstone, and live startup admission probe bound its effects.
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
and assert exactly one terminal result: matching artifact row plus capture object and `published`,
or no artifact row plus the retained zero-byte fence tombstone and `discarded`.

Crash-recovery tests seed every durable boundary and prove replacement recovery never invents an
artifact row, never deletes a registered object, and does not report readiness while cleanup is
unproven. Fresh-install tests prove protocol 4 and aggregate completion are established only on an
empty database; any protocol-3 worker, job, operation, or artifact population makes migration fail
without mutation. Concurrent-attempt tests prove the second attempt cannot PUT until the first
publication is terminal. An ambiguous-PUT test holds the capture conditional create in flight,
lets replacement recovery acquire the released database fence, and proves it cannot record
`discarded` until capture-versus-tombstone arbitration resolves and the operation-identity
tombstone version is durable. It then lets the delayed PUT evaluate and proves the retained
tombstone makes it fail without creating capture bytes. A re-entrant recovery test crashes after
tombstone durability but before the `discarded` transaction, starts replacement recovery while the
capture PUT remains delayed, and proves it adopts the same tombstone version without reopening the
key. Success, cancellation, and replacement-recovery tests fail spool deletion at each terminal
publication outcome and prove acknowledgment/readiness remain barred until the exact private
operation directory is absent and `spool_disposed_at` commits. Seeded recovery tests start from
`canceling` after each delete, tombstone-proof, store, and database failure and prove monotonic
resume without reopening publication. A lost-delete-response test crashes after the exact-version
delete request and proves recovery uses `cleanup_capture_version_id`, verifies that version's
absence, and cannot close `discarded` while it remains readable. Seeded wrong-operation-id,
unknown-kind, and nonzero-tombstone tests assert the object is untouched, the stable corruption
reason is logged, and acknowledgment/readiness remain barred. Tests
deliberately break transition validation and compensation to show the new
assertions fail before restoring the implementation. `just ci` is the final local gate on both
declared target architectures through architecture-independent Python and PostgreSQL behavior;
the live MinIO integration fixture additionally proves atomic conditional-create admission. No
live provider or MCP-contract change is required.
