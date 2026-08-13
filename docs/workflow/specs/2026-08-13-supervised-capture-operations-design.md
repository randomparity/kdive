# Supervised capture operations design

Issue: [#1951](https://github.com/randomparity/kdive/issues/1951)
Decision: [ADR-0558](../../adr/0558-supervised-capture-operation-processes.md)
Governing decision: [ADR-0556](../../adr/0556-reclaim-orphaned-captures-across-providers.md)

## Scope

This change gives each charged `capture_traffic` attempt a durable, terminable provider-operation
identity. Provider mutation cannot start until the identity is linked durably to the job attempt;
loss of either job heartbeat ownership or the session holding the operation fence starts
cancellation; replacement workers can recover every launch state and prove process and provider
quiescence. It also installs the positive protocol-2 worker cutover record ADR-0556 requires before
historical capture reclamation can begin.

Artifact publication fencing remains #1952. Candidate selection and reclamation remain #1946;
provider reapers remain #1947 and #1948; immediate cleanup-result recording remains #1949. This
change adds no MCP tool or agent-facing schema.

## Approaches

The selected approach is one exec child per provider phase, gated by the worker until exact
identity registration commits. It creates a hard termination boundary, reconstructs provider
state from a replayable request, and lets a replacement use durable OS identity without adding a
long-lived privileged daemon.

Keeping calls in worker threads is smaller but cannot prove termination. Forking the already
assembled provider avoids reconstruction but is unsafe after Python, libvirt, telemetry, and pool
threads exist. A permanent host supervisor can provide the same gate, but adds a service and IPC
trust boundary that this issue does not otherwise need.

## Durable model

Migration `0112_capture_operation_supervision.sql` adds:

- `capture_operations`: UUID primary key; `job_id`; positive `job_attempt`; exact
  `worker_incarnation`; `provider_kind`; `resource_id`; `system_id`; `domain_name`; request digest;
  random 256-bit `launch_token`; authority-derived `host_instance`; Linux `boot_id`, `pid`, and
  `start_ticks`; state
  (`launching`, `gated`, `running`, `cancel_requested`, `exited`); exit outcome/code; bounded JSON
  quiescence evidence; database-clock timestamps. `(job_id, job_attempt)` is unique.
- `jobs.current_capture_operation_id`, nullable, with a foreign key to `capture_operations`. A
  database function links it only when the row is still the exact running attempt owned by the
  authenticated worker. Recycle and a later charged attempt clear the link.
- `capture_cutovers`: one generation per supported provider kind, with state (`draining`,
  `complete`), start time, database-clock cutoff, and separate operation-quiescent and
  publication-closed flags. #1951 writes only the operation flag; #1952 owns the publication flag.
- `capture_cutover_workers`: the immutable set of active protocol-2 incarnations captured when a
  generation starts and the durable termination observation for each.

All worker writes use security-definer functions that derive the incarnation from the credential,
lock the worker incarnation and job attempt, validate state transitions, and expose no direct
table mutation to the worker role. Reconciler-readable views expose only identities, states,
deadlines, and evidence, never request or packet data.

The operation transition table is:

```
launching -> gated -> running -> exited
    |          |         |
    +----------+-------> cancel_requested -> exited
```

Repeated exact writes are idempotent. A conflicting identity or backward transition fails. The
`running` write is observability, not authority: a released gate with a still-`gated` row remains
recoverable because every non-`exited` state is treated as possibly mutating.

## Launch and data flow

The handler validates the BPF expression before launch and snapshots the Run, System, Resource,
provider kind, and domain under the existing Run lock. It then acquires the per-job session
advisory operation fence on its dedicated autocommit connection.

The launcher creates a private attempt directory beneath the configured KDIVE runtime data root,
writes a canonical JSON request with mode 0600, creates a gate pipe, and starts:

```
python -m kdive capture-operation --request <path> --gate-fd <fd>
```

Arguments are fixed tokens, never a shell command. The request schema accepts only the two wired
provider kinds, UUID identities, the snaplen and byte/window bounds already validated by the job
payload, and the snapshotted domain and Resource identity. The child's first application action is
the blocking one-byte gate read. Only after release does it open the request without following
symlinks, verify directory ownership, file mode, digest, and schema, and assemble the provider.
Gate EOF exits without opening attacker-controlled input or crossing a provider boundary.

Before spawn, the launcher creates and links one `launching` row for the exact charged attempt.
That transaction also persists a random 256-bit launch token. The fixed argv carries the token and
gate fd but no request path, tenant-controlled value, packet data, provider endpoint, or secret;
the child derives its request path from its supervisor-owned current directory after gate release.
Immediately after spawn, it reads the authority-derived host instance, host boot id, and the
child's `/proc/<pid>/stat` start ticks and opens a pidfd. It advances the row to `gated` with that
exact identity. A failed transition closes the gate without writing; EOF makes the child exit
before provider mutation. A committed identity permits exactly one release byte. The parent then
marks `running`; failure of that follow-up write triggers cancellation because durable `gated` is
deliberately recoverable as live.

After release the child reconstructs the local or Resource-bound remote `TrafficCapturer`, then
runs prepare, attach, bounded size polling, detach, bounded fetch, and reclaim. It writes raw pcap
bytes to a temporary mode-0600 result and atomically renames it within the attempt directory. A
small canonical result JSON records success, truncation, or a categorized failure. The parent
waits for process exit, performs the provider quiescence probe, durably acknowledges `exited`, and
only then reads the result. BPF trimming and the existing artifact path stay in the handler.

The child never receives the object store, worker incarnation credential, or authority to update
the job. Its provider configuration comes from the same environment and Resource binding used by
normal provider assembly. Packet data never enters argv, logs, JSON, or the database.
The executable may create threads required by Python or libvirt but may not spawn descendant
processes. A guard scans its owned module and provider-capture call graph for subprocess,
`multiprocessing`, `fork`, and `posix_spawn` use. A future provider that needs helpers must first
adopt a kernel-owned process-tree containment decision.

## Cancellation and recovery

The worker dispatch loop races the handler against the heartbeat loop for capture jobs. A
heartbeat returning false or raising cancels the handler and waits for its cleanup. The capture
handler also probes the lock-owning session while the child runs; any connection exception is
loss of authority. An explicit canceled job, process stop, or Python task cancellation uses the
same operation-cancel path.

Cancellation validates host instance, boot id, and start ticks, opens or uses a pidfd, sends
`SIGTERM` to that exact child, and waits five seconds on the supervisor's monotonic clock. If the
exact child remains, it sends `SIGKILL` and waits five more seconds. These are per cancellation
request,
not per job flow. Exceeding either bound leaves `cancel_requested`; the consequence is that
reclamation and result use remain barred. Recovery is an automatic scan on worker startup on the
same host, or an operator restart after restoring `/proc` visibility. The scan handles:

- `launching` with a live owner: the owner must register identity or close the gate;
- `launching` with a durably terminated owner: every gate writer is closed; record
  `aborted_before_identity` only after authority evidence proves the entire worker container/Pod
  boundary terminated or a same-host observer enumerates `/proc` for the worker uid, exact
  capture-operation executable, and durable launch token, terminates every match, and proves the
  token absent on a second complete enumeration;
- `gated` with a live identity: cancel it, because release may have raced the state write;
- `gated`, `running`, or `cancel_requested` with an absent identity: run the provider probe;
- a live identity owned by a dead or different incarnation: cancel it and probe;
- `exited` with complete evidence: no provider call; clean the spool only when its consumer has
  durably finished;
- host-instance mismatch, PID identity mismatch, unreadable `/proc`, or unreachable provider:
  retain the row without acknowledgment and report the reason.

Recovery ownership follows the worker incarnation's authority binding. A local replacement may
inspect `/proc` only on the configured host instance; a changed boot id is positive evidence that
the prior process cannot survive. Docker and Kubernetes use the existing lifecycle authority's
exact container identity or Pod UID termination to prove the entire isolated boundary exited when
no same-boundary replacement exists. A permanently lost local host without process visibility,
reboot evidence, or authority termination remains fail-closed. Restoring one of those evidence
paths is the operator recovery action; remote-libvirt reachability by itself is insufficient.
The launch token is not used after exact PID registration. Its only purpose is recovering the
spawn-to-registration interval, where pidfd identity does not yet exist. A scan refuses to conclude
absence if any `/proc` entry becomes unreadable during enumeration; the next recovery pass retries.

The provider probe runs only after exact process absence. Both implementations issue an
idempotent detach for `kdive-dump-<job_id>` over a fresh libvirt connection, followed by a QMP
query that proves that id is absent. QEMU serializes monitor commands, so the new query is an
ordering barrier after any `object-add` or `object-del` already accepted from the terminated
client; an adapter unable to establish that ordering cannot acknowledge quiescence. Local uses the
configured local URI. Remote derives the Resource-bound configuration and opens a new TLS
connection; the old child's exited process can no longer own a transport. A concurrency test holds
an earlier fake monitor command in flight and proves the fresh absence query cannot return first.
The local and remote gated `live_vm` suites repeat the fault against real libvirt and QEMU: a test
hook delays an accepted monitor mutation, the supervisor terminates that client, and the fresh
connection must not acknowledge absence before the mutation definitively completes or is
canceled. The fake supplies deterministic unit coverage but cannot satisfy either provider's
quiescence criterion. The evidence records provider kind, Resource, domain, QOM id, probe result,
and database observation time. It records no host address or credential.

## Legacy cutover

Fence protocol advances from 2 to 3. The claim function continues to admit protocol-2 workers for
non-capture kinds, but a `capture_traffic` claim requires protocol 3 once a provider-kind cutover
enters `draining`. Starting a generation takes a database advisory lock, persists the exact active
protocol-2 incarnation set, and installs the bar in the same transaction. This order means a
legacy worker is either in the set or cannot obtain a later capture claim.

The existing lifecycle authority remains the source of exact worker termination. The cutover
observer copies only durable terminated facts; absence, lease expiry, Pod name reuse, and a stopped
Compose process without its lifecycle acknowledgment do not count. For local-libvirt, operation
quiescence requires every recorded worker terminated and each host's supervised-operation scan
complete. For remote-libvirt it additionally requires a fresh transport observation for every
affected Resource. Failures retain `draining` and name the missing incarnation or Resource.

When all operation observations are complete, one transaction sets
`operation_quiescent = true`. The generation becomes `complete` only when #1952's
`publication_closed` is also true; that completion transaction samples `clock_timestamp()` into
`cutoff_at`. A capture admitted at any time during drain is no later than that cutoff. The future
#1946 predicate may accept a missing attempt link only when provider kind matches a complete
generation and `jobs.created_at <= cutoff_at`.

The operator surfaces are idempotent internal CLI commands used by deployment automation:
`capture-cutover begin`, `capture-cutover observe`, and `capture-cutover status`. `begin` and
`observe` are external writes but operate only on durable KDIVE coordination rows. Helm and
Compose rollout scripts call them around the existing worker drain/termination workflow. They do
not terminate workers themselves.

## Error handling and observability

Every log names operation id, job id, attempt, state, and a reason code, but never packet bytes,
request JSON, remote addresses, or credentials. Metrics count gated launches, released launches,
cancellation reasons, TERM/KILL escalation, recovery outcomes, probe failures, and cutover
remaining workers/resources. Database-clock timestamps make durable ordering independent of host
clock skew.

A child failure becomes the same `CategorizedError` the provider would have raised in-process.
Failure serialization is an allowlist of category, terminal flag, reason, and redacted details;
arbitrary exception strings do not cross the child boundary. Failure to prove quiescence takes
precedence over consuming either success or failure output.

## Threat model

### Boundaries and actors

- The authenticated tenant controls the existing capture payload bounds and BPF expression. The
  existing tool authorization and payload validation remain the caller controls; the child sees
  only their validated values.
- The worker writes a request consumed by a less-authoritative child. The new boundary is guarded
  by a mode-0700 directory, mode-0600 regular files opened without symlink following, canonical
  schema validation, and a digest linked in Postgres.
- The child causes local or remote hypervisor mutation. Existing provider configuration,
  Resource binding, TLS identity, and provider-specific QOM naming remain the controls.
- A replacement worker signals an OS process. Exact boot id, start ticks, pidfd use, and process
  group ownership prevent PID reuse or attacker-chosen process targeting.
- Deployment automation advances cutover state. Existing lifecycle-witness or Compose lifecycle
  authority supplies termination evidence; worker credentials cannot synthesize it.

Trusted actors are the host operator, lifecycle authority, Postgres, and the configured libvirt
endpoint. Authenticated tenants are not trusted to select paths, commands, provider endpoints, or
operation identities. Another local uid is outside the KDIVE process trust boundary but must not
read packet data or replace request/result files.

### Controls and exclusions

Fixed argv and `create_subprocess_exec` avoid shell interpretation. UUID-derived basenames,
directory-fd-relative opens, `O_NOFOLLOW`, ownership/mode checks, size bounds, and atomic same-dir
renames control path and file races. Database functions authenticate and fence every durable
transition. Provider error details pass through the existing redaction registry before logs or job
failure storage.

This change does not defend a root user on the worker host, a compromised hypervisor, or a
malicious lifecycle authority. It does not add tenant isolation beyond the existing worker model,
change BPF semantics, or make packet content less sensitive. Those actors already control the
worker or captured network plane and are outside this issue's reachable boundary.

## Verification

Unit and database tests fault every boundary: child exits before identity read, after spawn before
link, after link before release, after release before `running`, during each provider method,
during TERM and KILL, after process exit before probe, and after probe before acknowledgment.
Tests replace the child with a real gated helper process so breaking the release ordering makes
the test fail.

Database concurrency tests prove one operation per `(job_id, attempt)`, exact-attempt transition
fencing, protocol-2 capture claim exclusion with unrelated claims still admitted, immutable drain
membership, and atomic final cutoff sampling with a job admitted during drain. Provider tests
prove local and remote probes reconnect independently and refuse to acknowledge QOM presence or
an unreachable endpoint. Each provider's gated real-stack test delays an accepted monitor command,
kills the client, and proves the independent probe cannot acknowledge before definitive completion
or cancellation. Worker tests prove heartbeat false/error and lock-session failure both terminate
the child before dispatch returns.

The x86_64 host runs focused tests and `just ci`. Architecture-neutral database and process tests
also run in the declared ppc64le CI target. The existing gated local and remote live tiers gain a
capture cancellation proof; they are reported separately when their operator fixtures are
available and skip without claiming proof when absent.

## Durable workflow checkpoint

- Branch: `feat/supervise-capture-operations-1951`
- Base branch: `main`
- Host/targets: x86_64 host; x86_64 and ppc64le targets; host included
- Guardrails: focused `uv run python -m pytest ... -q` during TDD; `just ci` before review and
  delivery; CI additionally invokes its listed `just` recipes independently.
