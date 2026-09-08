# Async jobs

Long-running operations such as provisioning, installation, boot, and vmcore capture
use durable jobs in a Postgres-backed queue. The initiating tool returns a job handle;
workers execute the operation separately. Use this guide for the shared polling and
retry workflow, and the [tool reference](reference/tools.md) for each operation's contract.

## Follow a job

For a job-handle response, the envelope's `object_id` is the job ID. Pass that value as
`job_id` to `jobs.wait`. A new job normally starts `queued` or `running`; an existing job
returned by a repeated call may already be terminal. Check the returned status before
choosing the next call. The envelope guide
(resource://kdive/docs/guide/response-envelope.md) explains the common fields.

`jobs.wait` reads the job until it observes a terminal state or its polling window ends.
The window is per call, in seconds, measured against the server process's monotonic clock:
by default 30 seconds, capped at 300. It is not the operation's deadline. Database and
transport latency can make the call take longer than that window. `timeout_s=0` performs
one lookup without a polling sleep.

| Returned job status | What to do |
|---|---|
| `queued` or `running` | Continue with `jobs.wait` when ready; the polling window ending does not cancel or fail the job. |
| `succeeded` | Read the operation's result. `refs.result`, when present, is a tool-specific identifier or key; some jobs produce no reference. |
| `failed` | Inspect `error_category` and any failure details, then follow the errors guide (resource://kdive/docs/guide/errors.md) before retrying the operation. |
| `canceled` | Stop polling for completion; check the affected objects before further mutations. Cancellation does not prove provider cleanup finished. |

A failed *lookup* can also return an error envelope: for example, a job outside the
caller's readable scope appears not found. Do not interpret every `jobs.wait` error as
proof that the underlying job failed. `suggested_next_actions` are guidance; they do not
grant permission to execute the named tools.

Use `jobs.list` to find jobs visible in your projects; follow its pagination cursor to
read further pages. Platform-internal jobs are excluded from these tenant tools.
Operators have a separate [ops reference](reference/ops.md#opsjobs_list) for queue triage.

Allocation admission has its own state machine: `allocations.request` returns an
allocation ID and state. Follow a queued allocation with `allocations.wait`.
Kernel compilation happens in the caller's environment before upload.

## Cancel a job

`jobs.cancel` can transition an authorized, cancelable queued or running job to `canceled`.
It records that state; it does not provide a universal rollback or wait for every provider
operation to stop. Inspect the System or Run and follow the operation's cleanup contract
before assuming its resources can be reused or released.

The required role depends on the job kind. Some safety operations cannot be canceled:
an authority-owned preactivation teardown returns a conflict with `jobs.wait` and
`systems.get` as next actions. To check an already terminal job, use `jobs.wait` instead
of `jobs.cancel`. See the [jobs reference](reference/jobs.md#jobscancel) for the
cancellation role contract.

## Transport resets and retries

A proxy or load balancer can close a held `jobs.wait` request before a response arrives.
The client then sees a transport error, not a job envelope. This does not establish the
job's outcome ([ADR-0138](../adr/0138-transport-reset-retry-contract.md)).

Retrying an idempotent read such as `jobs.wait`, `jobs.list`, `systems.get`, or `runs.get`
is safe. Continue polling the same job ID after a dropped wait; do not enqueue the
operation again just to recover its status. Prefer repeated short waits, starting with
the default window. Short waits reduce the time a request stays open but cannot guarantee
that a proxy will keep it alive. If resets persist, check connectivity and the deployment's
proxy settings instead of assuming each failure will resolve on retry.

## Retrying the initial enqueue (idempotency)

A dropped mutation response leaves its outcome uncertain. For a tool that accepts
`idempotency_key`, choose a fresh key before the first call and reuse it only for the same
logical operation with unchanged inputs. Check that tool's schema; key support is not implied
by a create/enqueue name. Keys are associated with the calling principal, so do not recycle one
across tools, targets, or projects. A key is not a substitute for the call's authorization and
lifecycle preconditions.

The stored-result path replays a recorded successful envelope, which can still say `queued`
or `running` after the job has finished. Poll its job ID with `jobs.wait` for the current state.
Allocation request/renewal uses a different path: the key identifies the allocation and the
response is rebuilt from its current row. Do not depend on byte-identical responses across
all keyed tools or transport settings. The envelope guide
(resource://kdive/docs/guide/response-envelope.md) explains the returned fields.

The shared stored-envelope path accepts keys of 1–200 characters and records results without
an error category inside the mutation transaction. Failure and key-collision behavior can
differ on other paths; follow the tool's contract rather than assuming a universal error code.
Do not change the inputs under an existing key to request different work: stored-result lookup
does not compare them with the original arguments.

The reconciler deletes keys older than its retention interval (default seven days, measured
from the record's creation time using the database clock). Cleanup occurs on a periodic pass,
so this is not an exact expiration instant. Once the record is deleted, its stored response
cannot be replayed. Repeating the call may create or recycle work according to that tool's
job-deduplication policy; there is no blanket guarantee that a same-target retry is harmless.
After an uncertain outcome or a long interruption, inspect known jobs and objects before
issuing another mutation. Continue a known job with `jobs.wait` instead of repeating its enqueue.

## Worker recovery and failure details

A job row persists across worker restarts, but persistence does not guarantee that every
operation resumes automatically. Workers claim eligible queued jobs or reclaim eligible
running jobs after their database-clock lease expires. Each claim spends an attempt,
including a claim whose worker dies before recording a result. A heartbeat renews the
lease; the lease is not a total execution deadline.

On the ordinary worker path, retryable failures can requeue the job while attempts remain;
non-retryable failures or exhausted attempts fail it. The reconciler also fails ordinary
abandoned jobs whose lease and attempts are exhausted. Capture jobs have additional
process-stop checks before reclaim, and authority-managed System and boot jobs use
separate completion and recovery rules. A stalled job therefore needs triage; elapsed
time alone does not justify restarting the operation or bypassing its cleanup gates.

The job row can contain failure context, including a redacted exception message and
selected details. A failed job exposes that context in the envelope's `data`. This is
not a full console log, and redaction is not proof that arbitrary output is safe to share.
See the safety guide (resource://kdive/docs/guide/safety-and-rbac.md) before publishing
troubleshooting evidence.
