# Bound automatic ordinary teardown recovery

## Problem and ownership

A real failing provider through `teardown_handler`, credential-bound `Worker` and PostgreSQL
leaves the System `tearing_down` and exhausts its teardown. Three repair passes each recycle the
same failed job, producing `[1, 1, 1]` instead of `[1, 0, 0]`. The counter resets on each recycle.
The queue owns durable payload replacement, the worker owns the actual terminal decision, and
the System-locked repair owns admission. Their existing seams suffice; no extra durable object.
[ADR-0843](../../adr/0843-bound-automatic-ordinary-teardown-replay.md) changes only automatic replay.

## Payload and failure evidence

Add optional `automatic_teardown_replay_v1: Literal["consumed"] | None = None` to
`TeardownPayload`. Existing `exclude_none` serialization leaves fresh ordinary and authority
payload bytes unchanged. Only this repair writes `consumed`. Validation rejects other values.
The current job payload is the request allowance, not a new public parameter or request UUID.

In the ordinary exception-finalization path, compute existing category and terminal values
unchanged. Build the existing redacted failure context unchanged, then for TEARDOWN only add
`teardown_recovery_reason`: terminal decision first => `teardown_terminal_failure`; otherwise
consumed marker => `teardown_automatic_replay_exhausted`; otherwise
`teardown_automatic_replay_available`. Pass it to the same credential/attempt-fenced finalizer.
Do not change generic queue functions, SQL, retry counts, other kinds or authority finalizers.
The field is stored only on final failure by existing queue behavior; intermediate requeues
still clear failure context. A malformed ordinary teardown fails validation as before and
cannot gain available evidence from its permanent configuration failure.

## Admission and reset

Retain the current candidate selection, progress, warning ownership and System lock transaction.
Retain System state, live/canceled jobs, both authority marker guards and latest activation
settlement checks, including their warning precedence. After those guards:

- No job: enqueue one consumed ordinary teardown. This is its only automatic admission.
- Existing job: require kind TEARDOWN, state FAILED, attempt >= max_attempts, retryable non-null
  category, and available worker evidence. Decode its typed payload and require the same System
  UUID and absent consumed marker. Any failed predicate yields no enqueue.
- Enqueue with a new consumed typed payload and `JobRecyclePolicy.FAILED`. Admission and allowance
  consumption are the same existing payload update/transaction; count only after commit.

Use a small private admission-reason helper if it keeps the transaction readable. A denied
ordinary row warns with a stable reason (`teardown_automatic_replay_exhausted` for consumed,
`teardown_terminal_failure` for nonrecoverable/unknown outcomes, or
`teardown_replay_evidence_invalid` for malformed/mismatched payload), job ID, existing state and
`systems.teardown` remedy, using the current bounded warning map. Do not persist observation-only
warning state or mutate an operator-canceled row. Failed rows remain available for inspection.

All existing explicit ordinary constructors omit the marker. `systems.teardown`, break-glass
and the settled-authority ordinary route already replace the payload only on actual allowed
terminal recycle. Their policies, actor audits, matrix gates and first-enqueuer authorization
remain unchanged. Same-key and live-job replays do not reset the marker. Different/new/omitted
keys only reset it when the existing explicit path actually recycles a failed job. Investigation
force-close and orphan repair continue NEVER; neither silently becomes new recovery intent.

## Agent and operator guidance

Add wrapper prose to `systems.teardown`, regenerate its reference, and add a concise ordinary
recovery section to the existing stuck-System runbook. State the allowance in job admissions,
per current teardown request, enforced by durable payload rather than a clock: one automatic
admission after explicitly retryable exhaustion, each using the job's existing attempt budget.
There is no timed retry promise. Exhaustion or terminal/unknown evidence stops automatic work;
inspect failure details, correct the cause, invoke `systems.teardown` with a new or omitted key,
then `jobs.wait`/`systems.get`; release Allocation after proven torn_down as before. Existing
external-boot runbook guidance remains applicable to its separate authority owner.

## Failure model

- An ordinary job's durable consumed marker survives worker and reconciler restart. Concurrent
  repairs serialize under the System lock; live-job guards and queue dedup prevent double admission.
- A transaction error or cancellation rolls admission and consumed payload back together. A
  successful commit with lost response is still consumed. No separate decrement can be lost.
- Worker terminal escalation overrides category, including on the final attempt. Only positive
  actual worker evidence permits replay; old/manual/abandoned jobs without it need operator action.
- Succeeded, canceled, authority-marked, malformed and unsettled-activation work cannot be reopened
  by this lane. A consumed or permanent failure remains failed, with original provider diagnostics.
- Explicit operator retries can repeat intentionally. The existing audit attributes a recycle to
  the current requester while job authorization stays the first enqueuer. Same-key replay is not
  a fresh retry. This is a bounded automatic policy, not guaranteed cleanup or a recovery deadline.
- Warning suppression remains page-local and runner-owned; restart or paging can warn again.
  No new role, migration, provider operation, public parameter or response schema is introduced.

## Validation

Preserve and rerun the private actual-provider/worker reproduction unchanged. Promote it into
permanent tests, then cover a missing job consuming its allowance immediately, repeated fresh
runner/connection recovery and two actual concurrent repairs admitting exactly once. Exercise
explicit systems.teardown intent, same key/live replay, new/omitted key reset, first-enqueuer
preservation and actual audit rows. Include break-glass/control and settled ordinary constructors
in existing policy tests without changing their bodies.

Exercise retryable and nonretryable failures, terminal escalation before and at exhaustion,
unknown legacy evidence, malformed consumed markers, wrong System/kind, success/cancellation,
both authority markers and unsettled history. Use real PostgreSQL locks for repair rollback and
cancellation, assert connection recovery and retry with allowance intact. Update older fixtures
only to represent positively retryable exhaustion; change old succeeded-recycle assertions
because that automatic policy is deliberately replaced. Preserve paging/warning isolation proofs.

Inject removal of consumed marker, evidence and terminal checks, reset-on-replay or misplaced
transaction consumption; require named failures and restore exact bytes. Run focused real-DB
worker/repair/operator tests, whole-tree lint/types, relevant documentation/record/metadata gates,
exact unchanged generic queue/SQL/explicit-writer and other-worker-kind checks, hooks and an actual
wheel import/source proof. Fresh scoped code/security review closes this unit; campaign reviews
and full CI remain separate final requirements.
