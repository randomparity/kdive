# Spec — System snapshot / restore / list (#1254)

- **Status:** Draft (for adversarial review)
- **Issue:** #1254 — "Add System Snapshot Tool"
- **ADR:** [ADR-0378](../adr/0378-system-snapshot-restore.md)

## Problem

Kernel debugging is a repro loop: configure a guest (install packages, stage a reproducer, arm
kdump), trigger the bug, inspect, then go back and try again with different breakpoints. Today
the only way back to a clean pre-bug state is a full reboot or `systems.reprovision`, both of
which throw away the configured guest and cost minutes. There is no way to **checkpoint a
fully-configured, running guest and roll back to it in seconds**.

A hypervisor snapshot is the primitive that closes this gap, and it is one the agent cannot get
from inside the guest — freezing live RAM + CPU state is a host/hypervisor operation. libvirt's
`virDomainSnapshotCreateXML` / `revertToSnapshot` provide it for the local-libvirt provider. A
future bare-metal provider cannot, so the capability must be **advertised**, not assumed.

## Scope decision (why this shape)

- **Snapshot scope is caller-selectable RAM+disk (default) or disk-only.** The issue's use case
  — "snapshot just before triggering the bug, restore, retry with different breakpoints" — needs
  live memory: restore must resume at the exact instruction with the armed kdump, staged
  reproducer, and loaded modules intact. A disk-only restore reboots and loses all of that. The
  agent selects per call (`include_memory`, default `true`); disk-only remains available for the
  cheaper "roll back the filesystem" case.
- **Restore can land the guest paused** (`start_paused`, default `false`) so the agent can attach
  a gdbstub `debug.*` session and set breakpoints *before* execution resumes — deterministic
  "restore → break → continue". A paused restore lands the System in a distinct **`PAUSED`**
  state (not `READY`), because a suspended guest is not running (see the state model below).
- **Internal libvirt snapshots**, stored inside the System's qcow2 disk image — not external
  memory-state files and not S3. This makes the "freed on release" guarantee near-free (deleting
  the qcow2 at teardown frees the snapshot data) and keeps the blob out of the object store.
- **Snapshots are NOT a debug input format.** A libvirt memory snapshot is a QEMU `savevm`
  resume image, not a crash-format vmcore; the existing `vmcore.*` / `crash` / drgn-offline
  plane is unaffected and unchanged. Snapshots are a lifecycle/rollback primitive whose payoff is
  realized *through* the live `debug.*` tools after a restore.

## Domain model — a `snapshots` child ledger

A snapshot is a **child of the System** (like `run_steps` under a Run): a lightweight Postgres
ledger row that is the index-of-record for `list_snapshots`, audit, and teardown cleanup, while
libvirt holds the actual RAM+disk data inside the qcow2.

`snapshots` table (migration `0071`):

| column | type | notes |
|---|---|---|
| `id` | uuid PK | minted per snapshot row |
| `system_id` | uuid NOT NULL | `REFERENCES systems(id) ON DELETE CASCADE` — snapshot never outlives its System |
| `name` | text NOT NULL | agent-chosen; the libvirt snapshot name; `UNIQUE (system_id, name)` |
| `include_memory` | boolean NOT NULL | RAM+disk vs disk-only — read back at restore to validate mode |
| `state` | text NOT NULL | `creating` / `available` / `failed`; `snapshots_state_check` |
| attribution | | `principal`, `agent_session`, `project` (the `Attribution` mixin) |
| `created_at` / `updated_at` | timestamptz | DB-owned via a `_set_updated_at` trigger, mirroring `runs` |

`SNAPSHOTS = StatefulRepository(Snapshot, "snapshots", SnapshotState, ...)` in `db/repositories.py`.

`SnapshotState` StrEnum + adjacency table in `domain/capacity/state.py`:
`CREATING → {AVAILABLE, FAILED}`, `AVAILABLE → {FAILED}`, `FAILED` terminal. Row deletion (not a
state) removes a snapshot; it is driven by `systems.delete_snapshot` and by teardown.

**Name reuse / the UNIQUE constraint.** `UNIQUE (system_id, name)` means at most one row per
`(system, name)` at a time. Because a name is a durable checkpoint identity, admission does **not**
silently overwrite: `systems.snapshot(name)` against an existing **`available`** row is a
`configuration_error` ("name in use — `systems.delete_snapshot` first"); against a **`failed`**
row, admission **deletes the stale row (and any stale libvirt snapshot of that name) and creates
fresh** (auto-reclaim, so a failed capture never wedges the name); against a **`creating`** row it
returns the in-flight job (idempotent replay). `systems.delete_snapshot` frees the name (and the
qcow2 space) before teardown, so a repro-loop agent reclaims checkpoints without tearing the
System down. Chosen over accumulate-until-teardown (which the reviewer flagged: no reclamation
path, single-use names).

Chosen over (a) libvirt-as-source-of-truth (`list_snapshots` queries the hypervisor): loses the
Postgres state-of-record invariant, makes snapshots invisible to audit/teardown/reconciler, and
forces a live libvirt round-trip on a read; and (b) a full six-object-style durable object with
its own MCP lifecycle: heavier than the child-ledger the data needs. See ADR-0378 alternatives.

## System state machine — two new states, `RESTORING` and `PAUSED`

`domain/capacity/state.py` `SystemState` gains:

- **`RESTORING`** — a transient fence during a revert. Edges: `READY → RESTORING`;
  `RESTORING → {READY, PAUSED, FAILED}`.
- **`PAUSED`** — a resting state: the guest exists but its vCPUs are suspended (after a
  `start_paused` restore), awaiting `systems.power(action="resume")`. Edges:
  `PAUSED → {READY, TORN_DOWN, FAILED}`.

`READY`'s successor set gains `RESTORING`. `PAUSED` is **not** `READY`, which keeps the
`READY ⇒ running` invariant intact: snapshot admission (`include_memory` needs a running guest)
and the SSH tools (`ssh_access.py`, `ssh_reachable.py`) that gate on `state is READY` all
correctly exclude a suspended guest. A `PAUSED` guest is not SSH-reachable (its kernel is not
executing); that is expected and documented, not a silent inconsistency.

Adding two `SystemState` values ripples into every state-exhaustive site; the plan enumerates
each as an explicit step, and a guard test (below) fails when a new `SystemState` is missing from
any of them:

- `domain/capacity/state.py` — the adjacency table edges above.
- `db/schema/0071_*.sql` — widen `systems_state_check` with `restoring`, `paused`.
- `reconciler/repairs/allocations.py` `_NON_TERMINAL_SYSTEM` — add `RESTORING`, `PAUSED` (both
  hold a quota slot / a live host domain; the file's own comment says to update this "when
  `SystemState` gains a value").
- `services/systems/admission.py` non-terminal set — add `RESTORING`, `PAUSED` (a live System
  still consumes quota). New-Run admission does **not** treat `PAUSED`/`RESTORING` as launchable:
  a Run requires `READY`, so a paused/reverting System is refused a new Run until resumed.
- `providers/infra/console_hosting.py` live-state set — add `RESTORING`, `PAUSED` (the console
  keeps streaming across a revert and while paused).

## Provider seam — a new `Snapshotter` port + capability advertisement

### Capability advertisement (Pattern A — static `ProviderSupport`)

`ProviderSupport` (`providers/core/runtime.py`) gains `supports_snapshots: bool = False`
(fail-closed default). `local_libvirt/composition.py` sets it `True`; a future bare-metal
provider leaves the default. This is a **static** provider property (no libvirt I/O), so it is
cheap to read at any tool boundary.

Two surfaces, matching existing convention:

- **Proactive discovery:** `systems.get` includes `data.supports_snapshots` (resolve the
  System's `ProviderRuntime` via `resolver.runtime_for_system`, read `runtime.support`; no
  libvirt call). The wrapper docstring names it. This is the "share this info with the agent"
  the issue asks for — the agent checks it before attempting a snapshot.
- **Enforcement:** the snapshot/restore/list/delete tools on a provider with
  `supports_snapshots is False` return the existing `capability_unsupported` envelope
  (`mcp/tools/_common.py`), `capability="snapshot"`, `supported=[]`.

### The `Snapshotter` port

`providers/ports/lifecycle.py` gains a `Snapshotter` Protocol; `ProviderRuntime` gains an
optional `snapshot: Snapshotter | None = None` group (like `debug` / `rootfs`). `None` = plane
unsupported (kept consistent with `supports_snapshots is False`).

```
class Snapshotter(Protocol):
    def create(self, domain_name: str, name: str, *, include_memory: bool) -> None: ...
    def revert(self, domain_name: str, name: str, *, start_paused: bool) -> None: ...
    def delete(self, domain_name: str, name: str) -> None: ...      # idempotent: no-op if absent
    def delete_all(self, domain_name: str) -> None: ...             # teardown sweep
```

`LocalLibvirtSnapshotter` (`local_libvirt/lifecycle/snapshot.py`) mirrors `LocalLibvirtControl`:
a `connect: Callable[[], _LibvirtConn]` factory and a narrow `_LibvirtDomain` Protocol extended
with `snapshotCreateXML` / `revertToSnapshot` / `snapshotLookupByName` / `listAllSnapshots`.
`create` first deletes any stale libvirt snapshot of the same name (defensive, so a recycled name
is clean), then builds the snapshot XML: `include_memory=True` ⇒ a full system checkpoint
(`<memory snapshot='internal'/>` + internal disk); `include_memory=False` ⇒
`VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY` (disk snapshot only). `revert` calls `revertToSnapshot`
with `VIR_DOMAIN_SNAPSHOT_REVERT_RUNNING` or `..._REVERT_PAUSED`. Errors map to
`CategorizedError` (`INFRASTRUCTURE_FAILURE` for libvirt faults; `CONFIGURATION_ERROR` for a
missing snapshot on revert). `delete`/`delete_all` are idempotent (a missing snapshot is a no-op),
so teardown and cancel-cleanup cannot fail on an already-gone snapshot.

## Tools (agent-facing contracts)

All four live in the existing `systems` toolset registrar
(`mcp/tools/lifecycle/systems/registrar.py`), each a new `_register_systems_*` call inside
`register()`. All resolve the provider runtime via `with_runtime_for_system` (which also enforces
`required_role`) and refuse a non-snapshot provider with `capability_unsupported`.

### `systems.snapshot` — long op (`JobKind.SNAPSHOT`)

- **Params:** `system_id: str`; `name: Annotated[str, Field(...)]` (agent-chosen label,
  validated non-empty and to a libvirt-safe charset `[A-Za-z0-9._-]`, ≤64 chars);
  `include_memory: Annotated[bool, Field(...)] = True`.
- **Annotation:** `mutating()`. **RBAC:** `contributor` on the System's project. Registered in
  `exposure.py` as `_CONTRIBUTOR`.
- **Admission (synchronous, under `advisory_xact_lock(SYSTEM)`):** System exists in a granted
  project; caller has `contributor`; provider `supports_snapshots`; System is `READY`
  (`include_memory` requires a running guest, and `READY ⇒ running`). **A live Run does NOT block
  a snapshot** — the primary use case is snapshotting mid-debug. Resolve the name collision per
  the ledger rules above (reject `available`, recycle `failed`, replay `creating`). Insert a
  `snapshots` row in `creating`, audit `snapshot`, enqueue `JobKind.SNAPSHOT` with
  `SnapshotPayload(snapshot_id, system_id, name, include_memory)`, dedup key
  `{system_id}:snapshot:{name}` with `recycle_terminal=True, recycle_canceled=True` (the
  ADR-0367 pattern: a terminal/canceled prior job in that slot is recycled so a retry after a
  failed/canceled capture starts a fresh job rather than returning the dead one). Returns
  `job_envelope(job, "system_id", uid)` → `{job_id, status: queued}`; `suggested_next_actions =
  ["jobs.wait"]`.
- **The System stays `READY` throughout.** Snapshot is non-destructive to System identity; it
  does not transition state. A memory capture pauses the guest for the duration of the RAM write
  (see the pause-duration note); a live Run's SSH stalls then resumes — non-fatal, documented on
  the tool. Concurrent snapshots on one System serialize via libvirt's per-domain job lock.
- **Worker handler** (`snapshot_handler`): loads `SnapshotPayload`, resolves the binding,
  re-verifies `READY` at start, calls `runtime.snapshot.create(domain_name, name,
  include_memory=...)` off-thread; on success transitions the `snapshots` row `creating →
  available` under `advisory_xact_lock(SYSTEM)`; on `CategorizedError` transitions the row
  `creating → failed` and marks the error terminal. The **System row is never touched.** Returns
  `str(snapshot_id)` as `result_ref`.

### `systems.restore` — long op (`JobKind.RESTORE`), fenced by `RESTORING`

- **Params:** `system_id: str`; `name: Annotated[str, Field(...)]` (an existing `available`
  snapshot); `start_paused: Annotated[bool, Field(...)] = False`.
- **Annotation:** `mutating()`. **RBAC:** `contributor` (restore is a leaseholder lifecycle op,
  **not** the `force_crash` destructive gate — that stays reserved for `force_crash`).
- **Admission (synchronous, under `advisory_xact_lock(SYSTEM)`):** System `READY` in a granted
  project; caller `contributor`; provider `supports_snapshots`; the named snapshot exists and is
  `available` (else `configuration_error`); **`start_paused=True` is rejected against an
  `include_memory=False` snapshot** (`configuration_error`, "cannot pause-restore a disk-only
  snapshot: it has no saved CPU/RAM state") — a memoryless revert cannot resume at an
  instruction; **rejects if a live Run exists** (`_has_live_run`, the reprovision rule — restore
  discards the running guest, corrupting an active Run). Transitions the System `READY →
  RESTORING`, audits `ready→restoring`, enqueues `JobKind.RESTORE` with `RestorePayload(system_id,
  name, start_paused)`, dedup key `{system_id}:restore:{name}:{start_paused}` with
  `recycle_terminal=True` (restore is repeatable — a re-issue after a prior restore completed
  starts a fresh one). Returns the job handle.
- **Worker handler** (`restore_handler`): re-verifies `RESTORING`, resolves binding, calls
  `runtime.snapshot.revert(domain_name, name, start_paused=...)` off-thread; on success
  transitions `RESTORING → READY` (running restore) or `RESTORING → PAUSED` (`start_paused`) under
  the SYSTEM lock, auditing the edge; on `CategorizedError` **or a mid-revert cancel** transitions
  `RESTORING → FAILED` (`_record_system_failure`) — a half-reverted guest is indeterminate, so it
  is routed to `FAILED`, never back to `READY`. Returns `str(system_id)`.
- **Disk-only restore reboots.** Reverting a disk-only (`include_memory=False`) snapshot rolls
  back the filesystem and the guest reboots (no saved RAM/CPU to resume) — it lands in `READY`
  after re-boot, not at an instruction. Documented on the tool and in acceptance criterion 3b.
- **Paused restore & resume:** `start_paused=True` reverts into libvirt's *paused* domain state
  (`VIR_DOMAIN_SNAPSHOT_REVERT_PAUSED`) and the System lands in **`PAUSED`**. The agent attaches a
  gdbstub `debug.start_session`, inspects/sets breakpoints, then **resumes with
  `systems.power(system_id, action="resume")`** — a new `PowerAction.RESUME` → `virDomainResume`,
  valid only from `PAUSED` (→ `READY`); other power actions still require `READY`. PowerAction is
  serialized in the job payload (no CHECK constraint), so `RESUME` adds no migration.
  `suggested_next_actions` on a `start_paused` restore names `debug.start_session` and
  `systems.power`. drgn-*live* over SSH does not work against a paused guest (the kernel is not
  executing); gdbstub-based `debug.*` does. Documented on the tool.

### `systems.list_snapshots` — synchronous read

- **Params:** `system_id: str`. **Annotation:** `read_only()`. **RBAC:** `viewer`.
- Returns `ToolResponse.collection` (mirroring `systems.list`) of the System's `snapshots` rows
  from Postgres — `name`, `include_memory`, `state`, `created_at` — newest first. No libvirt
  round-trip. A supported provider with no snapshots returns an empty collection; an unsupported
  provider returns `capability_unsupported`.

### `systems.delete_snapshot` — synchronous mutation

- **Params:** `system_id: str`; `name: str`. **Annotation:** `mutating()`. **RBAC:** `contributor`.
- **Admission (under `advisory_xact_lock(SYSTEM)`):** the named row must exist (else
  `configuration_error`). Rejects deleting a `creating` snapshot (a capture is in flight — cancel
  the job first). Calls `runtime.snapshot.delete(domain_name, name)` (idempotent) to free the
  libvirt snapshot + qcow2 space, then removes the ledger row, audits `delete_snapshot`. Returns a
  success envelope. Synchronous: a libvirt snapshot delete is fast (metadata + qcow2 block
  discard), unlike a capture. This frees the name for reuse and reclaims disk before teardown.

## Teardown — snapshots are freed on release

Snapshots are System-scoped and released with the System (the durable-objects invariant: a child
never outlives its parent). `teardown_handler` (`jobs/handlers/systems.py`) is made
snapshot-aware:

1. **Delete libvirt snapshot metadata before undefine.** libvirt refuses to `undefine` a domain
   that still has snapshot metadata unless `VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA` is passed.
   Teardown calls `runtime.snapshot.delete_all(domain_name)` (idempotent) — reaping every
   libvirt snapshot including any cancel-orphaned one whose ledger row is `failed`/`creating` — and
   passes the undefine-with-snapshots flag as defense in depth, so undefine cannot fail on a
   snapshotted System.
2. **The qcow2 deletion frees the data.** Internal snapshots live inside the disk image teardown
   already removes; no external file or S3 object to leak.
3. **Ledger rows cascade.** `snapshots.system_id → systems(id) ON DELETE CASCADE` removes the
   rows when the System row is deleted; if teardown soft-transitions the System instead of
   deleting the row, the reclaim step deletes the `snapshots` rows explicitly alongside the
   existing System-owned artifact reclaim.

A `torn_down` System leaves **no** `snapshots` rows and **no** libvirt snapshot metadata,
including snapshots orphaned by a canceled capture.

## Concurrency, safety, recovery, observability

- **Advisory lock:** every snapshot/restore/delete admission and every worker-side state commit
  runs under `advisory_xact_lock(conn, LockScope.SYSTEM, system_id)` inside the same transaction,
  the reprovision/power pattern.
- **Restore fences via `RESTORING`:** while a System is `RESTORING`, reprovision/power/teardown/
  another restore/snapshot are refused at admission (they require `READY`), so the disruptive
  revert has exclusive control of the domain. `PAUSED` similarly refuses new Runs and non-resume
  power actions until the agent resumes.
- **Snapshot does not fence via state** (stays `READY`, permitted during a live Run); concurrent
  snapshots on one System serialize via libvirt's per-domain job lock, and a teardown that lands
  mid-snapshot (agent-owned System, unlikely) surfaces as a snapshot-job `INFRASTRUCTURE_FAILURE`
  with the `snapshots` row `failed` — non-fatal to the System, reaped at teardown.
- **Stuck-transition recovery (RESTORING).** There is **no** generic reconciler sweep for
  transient System states today (`repair_stalled_crashing_systems` is CRASHING-specific). A new
  repair **`repair_stalled_restoring_systems`** (`reconciler/repairs/systems.py`, mirroring the
  crashing repair) is added: a System in `RESTORING` with **no active `RESTORE` job** is
  transitioned `RESTORING → FAILED` under the SYSTEM lock. Without it, a canceled restore or a
  worker that dies mid-revert would strand the System in `RESTORING` with every lifecycle op
  fenced out forever (the R3 limbo ADR-0325 fixed for CRASHING). `PAUSED` needs **no** stuck
  repair — it is a resting state (like `READY`) awaiting the agent's explicit resume — but it is
  non-terminal, so a lapsed allocation reaps it via the existing allocation-liveness path (hence
  `PAUSED ∈ _NON_TERMINAL_SYSTEM`).
- **Cancel is best-effort.** `SNAPSHOT`/`RESTORE` join `CONTRIBUTOR_CANCELABLE_JOB_KINDS`
  (otherwise the cancel gate fails closed to operator-only). Cancel flips the job state but cannot
  abort an in-flight off-thread `snapshotCreateXML`/`revertToSnapshot`. Consequences are designed,
  not left implicit: a canceled **snapshot** leaves its row `creating`/`failed`; any libvirt
  snapshot that did materialize is deleted by the next same-name `create` (defensive pre-delete),
  by `systems.delete_snapshot`, or at teardown (`delete_all`) — it never survives release. A
  canceled **restore** routes the System to `FAILED` (indeterminate guest), and the `RESTORING`
  repair backstops a cancel that races the handler.
- **Idempotency:** snapshot dedup `{system_id}:snapshot:{name}` (+ recycle) makes a same-name
  retry idempotent while a capture is in flight and reclaimable after it fails.
- **Audit:** snapshot/restore/list/delete are audited (tool, `system_id`, `name`,
  `include_memory` / `start_paused`, outcome).
- **Telemetry:** per-kind job telemetry surfaces `SNAPSHOT`/`RESTORE` success/failure rates.
- **Redaction:** no guest console/memory content enters any response — the tools return only
  ledger metadata and job handles — so there is no new redaction surface.

## Capture cost & consistency (falsifiable notes, not "briefly")

- **Memory-capture pause scales with guest RAM.** An internal memory snapshot pauses the guest
  while all of guest RAM is written into the qcow2 — order of seconds per GB, so a multi-GB guest
  can pause for tens of seconds. This is the `SNAPSHOT` job's own duration; the agent polls
  `jobs.wait`, so there is no external tool timeout. The only in-guest effect is a live Run's SSH
  stalling for the pause, then resuming (TCP survives the pause). The tool docstring states the
  pause scales with RAM so an agent can size expectations.
- **Disk-only snapshots of a running guest are crash-consistent.** kdive assumes no in-guest
  qemu-guest-agent, so disk-only capture does **not** `fsfreeze`/quiesce; the disk image is
  crash-consistent (equivalent to a hard reset at the capture instant), and a restored disk-only
  snapshot may run a journal recovery / `fsck` on next boot. Documented as a caveat on the
  `include_memory=False` path; memory snapshots do not have this caveat (the FS state is captured
  coherently with the paused RAM).

## Persistence / migration (`0071_system_snapshots.sql`, forward-only)

1. `CREATE TABLE snapshots (...)` with the columns above, `UNIQUE (system_id, name)`,
   `system_id ... ON DELETE CASCADE`, `snapshots_state_check`, and the `_set_updated_at` trigger.
2. Drop-and-recreate `jobs_kind_check` widened with `'snapshot'`, `'restore'` (the `0069`
   pattern; keeps the constraint name for the SQL↔enum tie in `test_migrate.py`).
3. Drop-and-recreate `systems_state_check` widened with `'restoring'`, `'paused'` (the `0065`
   pattern).

No change to `PowerAction` persistence (payload jsonb, no CHECK).

## Out of scope (explicit)

- **Non-local-libvirt providers** — `supports_snapshots` is `False` for remote-libvirt and
  fault-inject in this change; remote-libvirt snapshot support is a follow-up if a need is
  established (the `Snapshotter` port already accommodates it).
- **A snapshot as a debug/analysis input** — a savevm image is not a vmcore; the `vmcore.*` /
  drgn-offline plane is untouched.
- **External snapshots / S3-stored memory state** — internal qcow2 snapshots only.
- **Cross-System / cross-Allocation snapshot transfer, snapshot export/download** — no established
  need; snapshots are ephemeral checkpoints tied to one System's lifetime.
- **A retention cap / auto-expiry of snapshots** — reclamation is agent-driven
  (`systems.delete_snapshot`) plus teardown; a per-System count/disk cap is a follow-up if disk
  pressure is observed (noted, not designed out).

## Acceptance criteria

1. `systems.snapshot(system_id, name)` on a `READY` local-libvirt System inserts a `creating`
   `snapshots` row and enqueues a `SNAPSHOT` job returning `{job_id, status: queued}`; the
   handler creates a libvirt internal snapshot (RAM+disk by default) and drives the row to
   `available`. The System stays `READY`, and the call succeeds **even while a live Run exists**.
2. `include_memory=False` produces a disk-only snapshot (`VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY`);
   `include_memory=True` (default) produces a full system checkpoint.
3. `systems.restore(system_id, name)` on a `READY` System with an `available` memory snapshot and
   **no** live Run transitions `READY → RESTORING`, reverts the domain, and returns it to `READY`
   (running restore). A restore with a live Run is refused `configuration_error`.
3b. Restoring a disk-only snapshot reboots the guest and lands `READY` (not at an instruction);
   `start_paused=True` against a disk-only snapshot is refused `configuration_error` naming the
   mode mismatch.
4. `start_paused=True` (memory snapshot) reverts the guest paused and lands the System in
   `PAUSED`; `systems.power(action="resume")` transitions `PAUSED → READY` and resumes the guest.
   A `PAUSED` System is refused a new Run and non-resume power actions, and is not SSH-reachable.
   `suggested_next_actions` on the paused restore names `debug.start_session` and `systems.power`.
5. `systems.list_snapshots(system_id)` returns the System's snapshots newest-first from Postgres,
   no libvirt round-trip. `systems.delete_snapshot(system_id, name)` deletes the libvirt snapshot
   and the ledger row (freeing the name for reuse); deleting a `creating` snapshot is refused.
6. On a provider with `supports_snapshots is False`, all four tools return `capability_unsupported`
   (`capability="snapshot"`); `systems.get` surfaces `data.supports_snapshots` for both provider
   kinds without a libvirt call.
7. Tearing down a snapshotted System deletes libvirt snapshot metadata (undefine does not fail),
   frees the data with the qcow2, and leaves **no** `snapshots` rows — including a snapshot
   orphaned by a canceled capture.
8. `SNAPSHOT`/`RESTORE` are in `ACTIVE_JOB_KINDS` and `CONTRIBUTOR_CANCELABLE_JOB_KINDS`; a
   contributor can cancel its own snapshot/restore job. A canceled restore lands the System in
   `FAILED`; a System stuck in `RESTORING` with no active `RESTORE` job is recovered to `FAILED`
   by `repair_stalled_restoring_systems`.
9. A guard test fails when a new `SystemState` value is absent from any state-exhaustive site
   (`_NON_TERMINAL_SYSTEM`, admission non-terminal set, `console_hosting` live set, the adjacency
   table). `RESTORING` and `PAUSED` are present in all four.
10. Migration `0071` creates `snapshots`, widens `jobs_kind_check` (`snapshot`,`restore`) and
    `systems_state_check` (`restoring`,`paused`); `test_migrate.py` and the per-migration test
    stay green.
11. The `systems` toolset guide and agent index document the four tools, the capability
    advertisement, the paused-restore→resume workflow, the disk-only crash-consistency and
    memory-pause caveats, and the "freed on release" contract.
