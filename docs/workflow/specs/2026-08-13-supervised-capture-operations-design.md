# Supervised capture operations design

Issue: [#1951](https://github.com/randomparity/kdive/issues/1951)
Decision: [ADR-0558](../../adr/0558-supervised-capture-operation-processes.md)
Governing decision: [ADR-0556](../../adr/0556-reclaim-orphaned-captures-across-providers.md)

## Scope

This change gives each charged `capture_traffic` attempt a durable, terminable provider-operation
identity. Provider mutation cannot start until the identity is linked durably to the job attempt;
loss of either job heartbeat ownership or the session holding the operation fence starts
cancellation; replacement workers can recover every launch state and prove process and provider
quiescence. It also installs the unconditional offline protocol-3 cutover required before
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
  authenticated worker. A later attempt cannot be charged while any operation for that job lacks
  complete exit evidence; only then may claim clear the prior current link.
- `capture_operation_cutoff`: a singleton row containing protocol 3, `operation_quiescent`, and
  database-clock `cutoff_at`. Migration 0112 owns only this operation evidence. #1952 owns its
  publication-closure schema and the later combined-completion contract consumed by #1946.

All worker writes use security-definer functions that derive the incarnation from the credential,
lock the worker incarnation and job attempt, validate state transitions, and expose no direct
table mutation to the worker role. Reconciler-readable views expose only identities, states,
deadlines, and evidence, never request or packet data.

Owner functions require the operation's exact active incarnation. Live-boundary recovery requires
an authenticated replacement whose immutable binding names the same local host, Compose container,
or Kubernetes Pod UID and that has same-boundary `/proc` visibility. Terminated-boundary recovery
instead accepts a successor in the same configured Compose project/service/ordinal or Kubernetes
namespace/StatefulSet/ordinal scope only after the lifecycle authority has durably terminated the
exact old container identity or Pod UID. The scope comes from authority registration, never worker
input. Recovery acquires old and replacement incarnation locks in lexical order plus the operation
lock. Cross-host, cross-deployment, cross-ordinal, live-old-boundary, stale-credential, and caller-
supplied evidence all fail closed. Repeated recovery by one authorized successor is idempotent.

The operation transition table is:

```
launching -> gated -> running -> exited
    |          |         |
    |          +---------+-------> cancel_requested -> exited
    +---------------------------> exited (pre-release abort only)
```

Repeated exact writes are idempotent. A conflicting identity or backward transition fails. The
`running` write is observability, not authority: a released gate with a still-`gated` row remains
recoverable because every non-`exited` state is treated as possibly mutating.
`launching -> exited` accepts only `aborted_before_spawn` with no child identity, or
`aborted_before_identity` with closed-gate and complete boundary/token-absence evidence. The
database rejects either outcome after identity registration or gate release.

The capture-aware claim function skips a queued or lapsed `capture_traffic` row while any prior
operation for that job is not `exited` with complete process and provider evidence. It does not
consume an attempt or clear the link. Recovery completes the prior operation; a later claim then
charges and links normally. Other job kinds retain the existing claim behavior.

## Launch and data flow

The handler validates the BPF expression before launch and snapshots the Run, System, Resource,
provider kind, and domain under the existing Run lock. It then acquires the per-job session
advisory operation fence on its dedicated autocommit connection.

The launcher creates a private attempt directory beneath the configured KDIVE runtime data root,
writes a canonical JSON request with mode 0600, creates a gate pipe, and starts:

```
python -S -m kdive.capture_bootstrap --launch-token <token> --gate-fd <fd>
```

The fixed `-S` bootstrap runs with loader-affecting variables removed and a supervisor-fixed
package path. It imports only the in-tree sandbox bootstrap, installs the filter, and then reads
the gate; request and provider modules are not imported. The child's cwd is the attempt directory
and the request
basename is the literal `request.json`. Arguments are fixed flags plus the database-generated
token and inherited fds, never a shell command; no tenant-controlled value appears in argv. The
request schema accepts only the
two wired provider kinds, UUID identities, the snaplen and byte/window bounds already validated by
the job payload, and the snapshotted domain and Resource identity. After the filter is installed,
the blocking gate read is the first action that can open request input,
import or assemble provider code, or reach a provider boundary. Only after release does it open
`request.json` relative
to a verified directory fd without following symlinks, verify ownership, modes, digest, and
schema, and assemble the provider. Gate EOF exits without opening request input or crossing a
provider boundary.

The pre-filter interpreter/loader interval is an explicit trusted host prerequisite rather than a
claimed kernel boundary. Deployment records a SHA-256 manifest of the exact Python executable and
the executable ELF dependency closure resolved without environment-controlled search paths. Each
launch recomputes that fingerprint and fails before spawn on drift. `kdive/__init__.py` is inert;
its public version attribute is lazy, so resolving `kdive.capture_bootstrap` executes no version,
metadata, subprocess, or provider import. An isolated import-trace test pins the allowed bootstrap
module set. The gate fd is non-inheritable except for the exact spawned bootstrap and every other
descriptor is closed.

After the bootstrap reports successful filter installation, the supervisor enumerates the
dedicated child process group and `/proc/<pid>/task` twice around a scheduler yield. Both complete
enumerations must contain only the registered pid and its initial task before identity may commit
or the gate may release. Any extra or unreadable member aborts launch and kills the process group.
The attested immutable startup plus this observed-empty handoff is the trusted premise; after the
handoff, the filter enforces no new descendant process. Exact pidfd absence therefore proves the
complete mutation boundary, not arbitrary unapproved interpreter startup.

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
the job. The launcher constructs an environment allowlist containing only locale, Python runtime
keys required to execute the installed package, and provider settings required by the selected
local or remote adapter. It excludes every database URL, object-store setting, OIDC value,
lifecycle credential, worker credential path/value, telemetry exporter credential, and unrelated
provider configuration. Remote private-key settings remain file references governed by existing
provider file permissions; key bytes never enter the environment. Tests seed every forbidden
class in the parent and prove it absent in the child while both adapters still assemble. Packet
data never enters argv, logs, JSON, or the database.

The executable is a single-process mutation boundary after its minimal trusted bootstrap: threads
are permitted, but descendant processes are not. The bootstrap installs a filter that fails closed
on any audit
architecture or syscall ABI other than the supported x86_64 and ppc64le forms. It denies `fork`,
`vfork`, `execve`, and `execveat`; permits legacy `clone` iff
`(flags & (CLONE_VM | CLONE_SIGHAND | CLONE_THREAD))` equals that complete mask; and returns
`ENOSYS` for every `clone3` so libc falls back to the inspectable legacy thread-creation call.
Python then reads the gate; provider modules load only after release. A process-creation attempt
fails with `EPERM`
and becomes an infrastructure failure. Runtime local and remote tests exercise a provider thread,
observe the child process tree through every phase, and invoke each process-creation path in helper
mode on x86_64 and ppc64le. The matrix covers zero flags, `CLONE_VM` without the complete mask, the
complete mask with normal pthread flags, extra flags, direct raw syscalls, `clone3` returning
`ENOSYS`, sanitized loader environment, no task/process creation during trusted bootstrap, denial
of later exec, fingerprint drift, import-trace drift, single/double-enumeration races, unreadable
group/task state, and a real provider thread falling back successfully. Filter setup failure exits
before release or provider mutation. A
provider needing a subprocess requires a different
kernel-owned containment decision.

## Cancellation and recovery

The worker dispatch loop races the handler against the heartbeat loop for capture jobs. A
heartbeat returning false or raising cancels the handler and waits for its cleanup. Immediately
before gate release, the handler executes `SELECT 1` on the session holding the per-job advisory
lock and requires the server response. After release it repeats that round trip every 250
milliseconds, measured per probe on the supervisor monotonic clock, with a one-second client-side
timeout and a one-second PostgreSQL `statement_timeout`. These are per active operation, not a
whole job-flow budget. A false/error/timeout means authority is lost; the consequence is immediate
operation cancellation and barred result/reclamation use. Recovery is the durable startup scan,
and the operator action after an exhausted cancellation is restoring host visibility and
restarting the worker. An explicit canceled job, process stop, or Python task cancellation uses
the same operation-cancel path. The gate release and pre-release round trip execute in one event-
loop critical section with no await between the successful response and the one-byte write.

Cancellation validates host instance, boot id, and start ticks, opens or uses a pidfd, sends
`SIGTERM` to that exact child, and waits five seconds on the supervisor's monotonic clock. If the
exact child remains, it sends `SIGKILL` and waits five more seconds. These are per cancellation
request, not per job flow. Exceeding either bound leaves `cancel_requested`; the consequence is that
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

Same-owner pre-release failure has one explicit abort protocol. If spawn fails, the owner closes
both gate ends and atomically records `exited/aborted_before_spawn`; no process identity is
required because `create_subprocess_exec` returned no child. If PID stat or pidfd acquisition
fails after spawn, it closes the release writer, awaits the child, performs the complete launch-
token absence scan, and records `exited/aborted_before_identity`. If that final database write
fails, durable `launching` remains and startup recovery repeats the token scan. No path records an
abort merely because cleanup was requested.

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

## Unconditional pre-release cutover

Fence protocol advances from 2 to 3 with no compatibility period. The operator stops the whole
worker deployment before applying migration 0112. Existing Compose and Kubernetes lifecycle
authorities record exact termination as they do today; local operators must use the same lifecycle
stop path rather than killing an unrecorded process.

Migration 0112 fails before any schema mutation unless every `worker_incarnations` row with
protocol below 3 has exact durable lifecycle-authority termination and a valid immutable authority
binding. This is the complete registered legacy population, independent of heartbeat, lease, and
job state. A `capture_traffic` job still `running` under one of those incarnations is finalized
offline only after its exact owner's termination is verified. Worker registration and cutoff
acquire the same global capture-protocol advisory lock. The cutoff transaction first persists
minimum protocol 3,
then takes each residual job fence and idempotently moves the row from `running` to `canceled` with
`JobState.CANCELED`, `error_category = NULL`, and
`failure_context = {"reason": "offline_capture_protocol_cutover"}`. It clears worker id, lease,
and heartbeat without charging an attempt or changing queued jobs. A row whose owner lacks exact
termination aborts the transaction. It then rechecks the complete registered and running-job
population and installs protocol-3-only registration,
authentication, and capture claim functions. It inserts `capture_operation_cutoff` with
`operation_quiescent = true` and `cutoff_at = clock_timestamp()` in the same transaction. There is
no drain state for a stale binary to join. A job admitted while the service is stopped remains
queued and is claimed only by a protocol-3 worker after restart.

Every process restart registers a fresh immutable incarnation. A protocol-2 restart that registers
before the cutoff bar appears in the locked population recheck and blocks unless terminated. A
restart racing after the bar is rejected at registration before it can authenticate or claim.
Local, Compose, and Kubernetes use their existing authority-specific fresh incarnation identities;
none may reactivate a terminated row.

Historical capture rows with no operation link are bounded by
`jobs.created_at <= capture_operation_cutoff.cutoff_at`, but the cutoff alone does not authorize
#1946 selection or remove provider state. #1952 defines publication closure and the combined
completion evidence that #1946 must additionally require. Existing pre-release in-flight capture
work is canceled by the offline transaction after owner termination and is not resumed or
preserved. If an assertion fails, the migration rolls back and names each blocking incarnation or
job; the recovery action is to complete the supported lifecycle stop and rerun migration. A
residual running row with a positively terminated owner needs no separate reconciler pass.

Helm and Compose upgrade documentation makes the offline stop a required step and refuses an
in-place rolling upgrade across protocol 2 to 3. Fresh installations create only the
operation-quiescent cutoff; #1952 later installs publication and combined-completion state.

## Error handling and observability

Every log names operation id, job id, attempt, state, and a reason code, but never packet bytes,
request JSON, remote addresses, or credentials. Metrics count gated launches, released launches,
cancellation reasons, TERM/KILL escalation, recovery outcomes, probe failures, and cutover
assertion failures. Database-clock timestamps make durable ordering independent of host clock skew.

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
  identity checks prevent PID reuse or attacker-chosen process targeting.
- Deployment automation performs an offline cutover. Existing lifecycle-witness or Compose
  lifecycle authority supplies termination evidence; worker credentials cannot synthesize it,
  and the migration refuses any un-terminated protocol-2 incarnation.

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

Unit and database tests fault every boundary: before spawn, after spawn before identity
registration, after identity registration before release, after release before `running`, during
each provider method, during TERM and KILL, after process exit before probe, and after probe before
acknowledgment.
Tests replace the child with a real gated helper process so breaking the release ordering makes
the test fail.

Launch tests pin the exact argv, cwd, `request.json` basename, and environment allowlist. They
cover spawn failure, `/proc` stat failure, pidfd failure, gate cleanup failure, and database failure
after cleanup. Each verifies that no provider marker can be created and that recovery reaches only
the stated terminal outcome.

Database concurrency tests prove one operation per `(job_id, attempt)`, exact-attempt transition
fencing, and protocol-3-only registration, authentication, and capture claims. Migration tests
prove any protocol-2 incarnation
without exact lifecycle termination, including a lapsed worker with a terminal job and live
provider thread, aborts the whole migration, while a fully stopped deployment
atomically records an operation-quiescent database-clock cutoff without aggregate completion. They
also prove the migration exposes no publication or aggregate-completion schema owned by #1952. An
abruptly
stopped worker's residual running capture is idempotently canceled only after termination proof.
Queued work remains unchanged and is claimed only after restart by protocol 3. A fresh database
asserts the exact protocol, operation-quiescent, and cutoff fields;
the canceled row assertion pins null error category, bounded reason, cleared worker/lease/heartbeat,
and unchanged attempt. Rolling protocol-2 registration and authentication are rejected. A
retry/cancellation race proves claim does not charge or hide a prior unacknowledged operation.
Provider tests prove local and remote probes reconnect independently and refuse to acknowledge QOM
presence or an unreachable endpoint. Each provider's gated real-stack test delays an accepted
monitor command, kills the client, and proves the independent probe cannot acknowledge before
definitive completion or cancellation. Worker tests cover heartbeat false/error and lock-session
loss before release,
immediately after release, on a stalled half-open probe, and racing a successful probe. A
responsive child is absent before dispatch returns. A child surviving both five-second waits
leaves `cancel_requested` with result and reclamation barred before dispatch returns, then startup
recovery supplies the eventual exit proof.

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
