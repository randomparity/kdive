# 0558 — Supervise capture operations as gated child processes

## Status

Proposed (2026-08-13)

## Context

`capture_traffic` currently runs blocking libvirt calls in threads owned by the job handler.
Canceling the handler does not stop a call already executing in a thread, and a failed job
heartbeat or lock-owning database session does not cancel the handler. ADR-0556 requires the
capture-reclamation path to wait for positive proof that the authoritative attempt cannot mutate
provider state. A released database lock, an expired lease, a missing worker, or an unobserved
acknowledgment is not that proof.

The launch boundary has a second race: a process started before its exact identity is durable can
outlive its worker without leaving enough evidence for a replacement to find and terminate it.
The rollout also contains historical workers that do not produce attempt evidence, so historical
reclamation needs a positive fence proving those workers were drained and barred before a
database-clock cutoff was sampled.

## Decision

Each capture provider phase runs in a fresh Linux child process started with
`asyncio.create_subprocess_exec`. The child receives a validated request file and an inherited
one-byte gate. It may parse and assemble its provider before the gate, but it may not call
`prepare`, `attach`, `captured_size`, `detach`, `fetch`, or `reclaim` until it reads the release
byte. Gate EOF is a mandatory no-mutation exit.

Before spawning, the parent creates a durable `launching` operation and links it to the exact
running job attempt in one transaction. A unique `(job_id, job_attempt)` constraint makes that
link the attempt's only current operation. It then starts the gated child and advances the row to
`gated` with `(host_instance, boot_id, pid, start_ticks)` plus the worker incarnation, provider
kind, and request identity. Only that committed exact identity permits release. Signals are sent
to the one child through a pidfd after rechecking boot id and start ticks, so PID reuse cannot
redirect cancellation.

The operation states are `launching`, `gated`, `running`, `cancel_requested`, and `exited`.
Transitions are monotonic and fenced by the exact job attempt and worker credential. If the
launcher dies while `launching`, exact worker-incarnation termination closes every inherited gate
writer; gate EOF proves that an unregistered child could not cross the mutation boundary. A live
launch owner must instead finish identity registration or close its gate before the row can exit.
A release may occur before the `running` write; recovery therefore treats both `gated` and
`running` as possibly mutating and terminates either. The child writes its bounded result to a
supervisor-owned, mode-0600 spool path and exits. Neither a result file nor a child exit alone is
quiescence evidence.

The lock-owning capture connection holds the per-job session advisory fence for the provider
phase. Job cancellation, failed heartbeat ownership, connection loss, handler cancellation, or
worker shutdown requests operation cancellation. Cancellation sends `SIGTERM` through the pidfd,
waits up to five seconds on the supervisor's monotonic clock for the exact child, then sends
`SIGKILL` through the pidfd and waits up to five more seconds. The capture-operation executable is
a single-process boundary and may create threads but no descendant processes; a provider that
needs a helper process cannot implement this lifecycle without a new containment decision.
Exceeding either interval leaves the row in `cancel_requested`;
recovery repeats identity observation and cancellation. The recovery action is the next worker
startup on that host or an operator restart after restoring host process visibility.

Positive exit requires both exact process absence and a provider probe after that absence.
Local-libvirt reconnects locally and proves the attempt's QOM object absent. Remote-libvirt opens
a new independently assembled TLS transport bound to the attempt's Resource and proves the same
QOM object absent. An unreachable provider, an identity mismatch, or an inconclusive probe leaves
the operation unacknowledged. Replacements repeat observation; they never translate missing
evidence into success.

Recovery is authority-bound. The operation records the immutable host boundary from the worker
incarnation: local workers use a configured host identity plus boot id, Docker workers use the
Compose-owned container identity, and Kubernetes workers use the Pod UID. A live-boundary
replacement must run on that boundary to inspect `/proc`. Durable container or Pod termination
from the existing lifecycle authority instead proves every process in that isolated boundary is
gone; a changed boot id proves the prior local process cannot remain. Provider quiescence is still
probed afterwards. A lost local host with neither reboot or termination evidence nor process
visibility remains fail-closed until the operator restores that evidence; remote provider
reachability alone cannot substitute for worker-host process absence.

Worker fence protocol 3 is the only protocol allowed to claim `capture_traffic` after this
migration. Other job kinds retain their current claim behavior. A provider-kind cutover begins by
atomically barring protocol-2 capture claims and recording the active legacy incarnations that
could have claimed that kind. Completion requires durable termination of every recorded
incarnation plus successful provider quiescence observations, and then writes the generation's
completion and `clock_timestamp()` cutoff in one transaction. Jobs admitted while workers drain
are no later than that final database-clock sample and are therefore covered. A worker cannot
rejoin after the bar because the claim function checks the protocol and cutover generation on
every claim.

## Consequences

- Capture provider calls become terminable at an OS process boundary without changing the MCP
  contract or artifact-publication behavior owned by #1952.
- Linux `/proc`, pidfds, a private spool directory, and provider reassembly become
  part of the worker-host contract on both x86_64 and ppc64le.
- Each active capture holds one database session and one child process. Cancellation has a
  ten-second total local wait bound before it becomes recoverable pending work.
- A provider outage fails closed: an exited child may remain without quiescence acknowledgment
  until an independent probe succeeds.
- The rollout is coordinated. Protocol-2 workers may finish unrelated work, but cannot claim new
  capture jobs after the cutover bar.
- The result spool contains sensitive packet data and must be mode 0600 in a mode-0700
  supervisor-owned directory; stale files are removed only after the durable operation is
  terminal and publication no longer needs them.
- A permanently lost local host without authority termination or reboot evidence retains its
  operations unacknowledged. The operator must restore host visibility or supply the existing
  lifecycle authority's exact termination evidence; a database override is not accepted.
- Capture providers used in this lifecycle may create threads but not descendant processes. That
  restriction is held by keeping subprocess APIs out of the child call graph and by a guard test;
  a provider needing helpers requires a different kernel-owned containment design.

## Considered & rejected

- **Keep provider calls in `asyncio.to_thread`.** Python cannot terminate a running thread, so
  task cancellation cannot prove provider quiescence.
- **Fork the assembled provider object from the worker.** Forking a multithreaded Python process
  can inherit locked interpreter and C-extension state. It also makes recovery dependent on
  opaque inherited objects instead of a replayable request.
- **Run a separate long-lived supervisor daemon.** It introduces another privileged deployment
  service and local RPC protocol. A per-operation exec child plus durable database identity gives
  a replacement worker the same recovery evidence with less permanent surface.
- **Treat process exit or lock release as sufficient.** Exit does not prove the last provider
  request left no filter, and lock release says nothing about a thread or orphan process.
- **Use PID alone.** PID reuse can make a replacement signal an unrelated process. Boot id,
  process start ticks, and pidfd signaling bind the observation to one exact process.
- **Sample the historical cutoff when draining starts.** A legacy worker can claim a job during
  the drain and mutate after that early sample. Sampling only in the completion transaction
  covers every job the legacy population could have admitted.
- **Defer supervision and leave reclamation disabled.** This avoids the process, schema, and
  rollout machinery, but #1946 could not enable historical reclamation and attached capture
  filters would continue writing without an outside owner. That does not meet #1951 or accepted
  ADR-0556's dependency for the current work.
