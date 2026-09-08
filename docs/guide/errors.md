# Errors

Use the error category together with the producing tool's contract and the affected
object's current state to choose recovery. `ErrorCategory` and `RETRYABLE_BY_CATEGORY` in
`src/kdive/domain/errors.py` define the implemented category strings and retry classification.
This guide explains recovery decisions; the [tool reference](reference/index.md) owns
operation-specific reasons and preconditions.

## Identify what failed

Read the envelope guide (resource://kdive/docs/guide/response-envelope.md) for failure fields
and nested results. A request error does not necessarily mean the target object failed,
and a failed job does not establish that its Allocation expired.

Use structured context such as `data.reason`, `data.current_status`, or `data.failing_job_id`
when the producing tool documents it. These fields are not universal. A `current_status`
value can describe a terminal state; it does not promise the object will advance if you wait.
Human-readable `detail` is diagnostic text, not a stable field to parse. A lookup miss or
permission denial can deliberately omit information about the target.

## Interpret `retryable`

`retryable: true` means the category describes a potentially transient condition. It does
not prove that the request had no effects, that repeating a mutation is safe, or that the
job will receive another attempt. Check the affected objects and known job before deciding
to retry. `retryable: false` means a bare retry is not the recovery path; input, permissions,
capacity policy, or lifecycle state may need to change first.

Workers use the same category classification on the ordinary job failure path, but a
handler can explicitly mark an otherwise retryable failure terminal. Attempt exhaustion
can also fail a job with `retryable: true`. Continue an existing job with `jobs.wait`;
do not submit extra work to supply its retries. Tenant job envelopes do not expose attempt
counts. The async-jobs guide (resource://kdive/docs/guide/async-jobs.md) owns worker recovery,
transport resets, and retries after an uncertain mutation response.

## Common recovery decisions

These are examples, not a second copy of the taxonomy. For other categories, follow the
producing tool's result description and recovery guidance.

| Category | Recovery decision |
|---|---|
| `configuration_error` or `conflict` | Check input and lifecycle preconditions. Read the relevant object or job to distinguish work still in progress from a terminal state or a conflicting request. Waiting alone may not resolve it. |
| `not_found` | Verify the ID, project, and caller's access. The object may be absent or invisible to this caller; the error does not prove it was deleted. |
| `missing_dependency` | Identify the upstream requirement from the tool's contract and diagnostic context before creating or replacing anything. |
| `stale_handle` | Stop using the handle for the rejected operation and inspect its current state. For example, `allocations.release` can report a terminal allocation as stale and suggest `allocations.wait`; the row need not be gone. |
| `authorization_denied` | Use `session.whoami` to inspect current grants, then follow the safety-and-RBAC guide (resource://kdive/docs/guide/safety-and-rbac.md). Report the unmet requirement to an operator; repeating the denied call does not add a grant or profile opt-in. |
| `allocation_denied`, `quota_exceeded`, `capacity_exhausted`, or `queue_timeout` | Establish whether the blocker is policy, quota, compatible resources, or temporary capacity. Correct the relevant request or resolve the capacity constraint before requesting more work; see the [allocations reference](reference/allocations.md). |
| `lease_expired` | Determine which lease expired. An abandoned job can carry this category because its worker lease expired and its attempts were exhausted. Read the job and allocation state before deciding a replacement allocation is needed. |
| `transport_conflict` | Check the existing debug session and the attach tool's preconditions. Coordinate detachment with its owner before retrying; do not terminate another caller's session to clear contention. |
| `transport_failure`, `infrastructure_failure`, or `provisioning_failure` | Inspect the failed operation and target state. A transient category alone does not authorize replaying a mutation or recovering a terminal System in place. |
| `symbol_not_found` | Check the symbol and the returned hint. An inlined, optimized-away, or addressless symbol will not become resolvable by repeating the same lookup; see the [debug reference](reference/debug.md#debugresolve_symbol). |

## An incomplete snapshot restore

`restore_incomplete` means the reconciler found a System stuck restoring with no restore
job able to finish it and no more specific retained failure category. The System becomes
`failed`; its disk state cannot be assumed to match either the snapshot or the pre-restore
guest. The category is non-retryable. Other failed restores can retain their original job
category, so this is not the only error a restore can report.

Inspect `systems.get`, any cited job, and the allocation with `allocations.wait`.
The current System state machine has no outgoing transition from `failed`: retrying the
restore cannot recover it, and ordinary `systems.teardown` cannot complete from that state.
Failed-System responses instead suggest `allocations.release` and `allocations.request`;
follow their [preconditions and returned state](reference/allocations.md) before provisioning
a replacement. A terminal allocation may already be unavailable for release.

Releasing the allocation does not establish that the failed guest or provider data was
cleaned up. Preserve needed evidence and ask an operator to triage residual resources;
do not assume they were reclaimed. Snapshots belong to the original System and cannot
be restored onto its replacement. Create new snapshots after rebuilding the guest.
