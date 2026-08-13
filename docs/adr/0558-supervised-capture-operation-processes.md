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
KDIVE remains pre-release with no customers. The operator therefore requires an unconditional
offline cutover rather than compatibility machinery for historical workers. The new deployment
may reject stale workers and in-flight legacy capture work instead of maintaining either.

## Decision

Each capture provider phase runs in a fresh Linux child process started with
`asyncio.create_subprocess_exec` as a fixed `python -S` bootstrap with a sanitized environment.
The bootstrap imports only the in-tree sandbox module, installs the containment filter, and then
reads the gate. Request input, provider modules, configuration assembly, and external endpoints
remain unreachable until both filter installation and release. The trusted interpreter/loader
startup before filter installation receives no tenant input or provider configuration and is
tested to remain one process and one task. After that minimal
bootstrap, the blocking one-byte gate read is the first action that opens request input, imports or
assembles provider code, or can reach a provider boundary. Gate EOF is a mandatory no-mutation
exit.

Before spawning, the parent creates a durable `launching` operation and links it to the exact
running job attempt in one transaction. A unique `(job_id, job_attempt)` constraint makes that
link the attempt's only current operation. The row also persists a random 256-bit launch token
before spawn. The fixed child argv carries that token while request paths and tenant-controlled
values do not. It then starts the gated child and advances the row to `gated` with
`(host_instance, boot_id, pid, start_ticks)` plus the worker incarnation, provider kind, and
request identity. Only that committed exact identity permits release. Signals are sent to the one
child through a pidfd after rechecking boot id and start ticks, so PID reuse cannot redirect
cancellation.

The operation states are `launching`, `gated`, `running`, `cancel_requested`, and `exited`.
Transitions are monotonic and fenced by the exact job attempt and worker credential. If the
launcher dies while `launching`, exact worker-incarnation termination closes every inherited gate
writer; gate EOF proves that an unregistered child could not cross the mutation boundary. The row
still cannot become `exited` until the authority proves the entire worker boundary terminated or a
same-boundary observer enumerates `/proc` for the exact worker uid and fixed capture-operation
executable carrying the durable launch token, terminates every match, and proves the token absent
on a second complete enumeration. A live launch owner must instead finish identity registration
or close its gate before the row can exit. The token is identity only for this pre-registration
recovery; after `gated`, pidfd plus boot id and start ticks is authoritative.
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
The bootstrap filter fails closed unless the audit architecture and syscall ABI are the supported
x86_64 or ppc64le form. It denies `fork`, `vfork`, `execve`, and `execveat`; returns `ENOSYS` for
`clone3`; and allows `clone` only when its flags contain
`CLONE_VM | CLONE_SIGHAND | CLONE_THREAD`. All other `clone` calls return `EPERM`. Filter
installation failure closes the gate path and exits before request or provider access.
Exceeding either interval leaves the row in `cancel_requested`;
recovery repeats identity observation and cancellation. The recovery action is the next worker
startup on that host or an operator restart after restoring host process visibility.

Positive exit requires both exact process absence and a provider ordering barrier after that
absence. QEMU monitor commands are serialized by the monitor: a query issued on a fresh libvirt
connection is processed only after any earlier accepted `object-add` or `object-del` command from
the terminated client. Local-libvirt reconnects locally, crosses that barrier, and proves the
attempt's QOM object absent. Remote-libvirt opens a new independently assembled TLS transport
bound to the attempt's Resource, crosses the same QEMU monitor barrier, and proves the QOM object
absent. Tests hold an earlier fake monitor command in flight and require the fresh query to wait
before it can acknowledge absence. The gated local and remote `live_vm` suites also delay an
actual monitor mutation at its acceptance boundary, terminate the supervised client, and prove
the independent connection cannot acknowledge absence until that mutation definitively completes
or is canceled. The fake is deterministic unit coverage, not evidence for real transport
ordering. An unreachable provider, an identity mismatch, an unordered transport, or an
inconclusive probe leaves the operation unacknowledged. Replacements repeat observation; they
never translate missing evidence into success.

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
migration. The deployment is stopped before migration. The migration refuses to establish the
cutover while any protocol-2 worker incarnation remains active or any protocol-2-owned capture job
has no exact lifecycle-authority termination record. The population is every registered
incarnation whose fence protocol is below 3, independent of heartbeat, lease, or job state; an
ambiguous or missing authority binding also fails. A running protocol-2-owned capture job remains
eligible for offline cancellation only after its exact owning incarnation's termination is
verified. The cutoff transaction acquires the job fence, idempotently moves each such row to
`canceled` with the existing cancellation category, then rechecks the complete worker and job
population under the worker-incarnation fence. It then atomically records the singleton as
operation-quiescent with a `clock_timestamp()` cutoff. Aggregate completion remains false until
#1952 records publication closure. New workers require protocol 3 at registration, authentication,
and claim, so a stale
binary cannot rejoin. There is no draining generation, compatibility state, or preservation of
legacy work. This pre-release rollout decision supersedes ADR-0556 only where that record requires
a positive online legacy-worker drain; ADR-0556's attempt quiescence and historical-row cutoff
requirements otherwise remain.

Worker registration and cutoff share a global capture-protocol advisory lock. The cutoff
transaction first installs a durable minimum protocol of 3, then rechecks every registered legacy
incarnation and its authority termination under that lock before sampling the cutoff. Any restart
must register a fresh immutable incarnation; protocol 2 is rejected after the bar, while a restart
that registered before the bar appears in the locked recheck and must already be terminated.

## Consequences

- Capture provider calls become terminable at an OS process boundary without changing the MCP
  contract or artifact-publication behavior owned by #1952.
- Linux `/proc`, pidfds, a private spool directory, and provider reassembly become
  part of the worker-host contract on both x86_64 and ppc64le.
- Each active capture holds one database session and one child process. Cancellation has a
  ten-second total local wait bound before it becomes recoverable pending work.
- A provider outage fails closed: an exited child may remain without quiescence acknowledgment
  until an independent probe succeeds.
- The rollout is an offline breaking cutover. Operators stop every worker and record its lifecycle
  termination before migration; stale or in-flight legacy state makes migration fail instead of
  being maintained.
- Heartbeats, leases, and job states never satisfy the cutover fence. A protocol-2 incarnation
  without exact lifecycle termination blocks migration even when its job is already terminal.
- After owner termination is proven, offline cutover cancels residual running legacy capture rows;
  it never resumes them or changes queued jobs.
- The cutoff is operation-quiescent evidence only. Historical reclamation remains barred until
  #1952 records publication closure and atomically completes the aggregate cutover.
- The result spool contains sensitive packet data and must be mode 0600 in a mode-0700
  supervisor-owned directory; stale files are removed only after the durable operation is
  terminal and publication no longer needs them.
- A permanently lost local host without authority termination or reboot evidence retains its
  operations unacknowledged. The operator must restore host visibility or supply the existing
  lifecycle authority's exact termination evidence; a database override is not accepted.
- Capture providers used in this lifecycle may create threads but not descendant processes. The
  pre-gate seccomp policy enforces that boundary across Python and native libraries; source guards
  are defense in depth only. Filter installation failure exits through the closed gate before
  provider mutation. A provider needing helpers requires a different kernel-owned containment
  design.
- The cross-connection monitor barrier is a live-provider requirement. A provider cannot ship this
  lifecycle based only on a fake; its gated suite must kill a client with an accepted mutation in
  flight and prove the independent absence observation is ordered afterwards.

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
- **Maintain an online legacy drain.** With no customers and no released compatibility contract,
  generation membership, host scans, and per-Resource observations preserve state nobody relies
  on. An offline stop plus fail-loud migration supplies the pre-release fence with less surface.
- **Defer supervision and leave reclamation disabled.** This avoids the process, schema, and
  rollout machinery, but #1946 could not enable historical reclamation and attached capture
  filters would continue writing without an outside owner. That does not meet #1951 or accepted
  ADR-0556's dependency for the current work.
