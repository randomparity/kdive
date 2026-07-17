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
row, admission **deletes the stale ledger row (Postgres-only) and creates fresh** (auto-reclaim,
so a failed capture never wedges the name) — any stale libvirt snapshot of that name is cleaned by
the `SNAPSHOT` handler's own defensive pre-delete, so admission does no libvirt I/O under the
SYSTEM lock; against a **`creating`** row it returns the in-flight job **only if that job is
genuinely non-terminal** (idempotent replay) — if the referenced `SNAPSHOT` job is already
terminal (a worker died or a cancel raced the handler, leaving the row stranded in `creating`),
admission treats the row as stale, deletes it, and creates fresh, so a dead capture cannot wedge
the name (see `repair_stalled_creating_snapshots` below). `systems.delete_snapshot` frees the name (and the
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
  `start_paused` restore), awaiting `control.power(action="resume")`. Edges:
  `PAUSED → {READY, TORN_DOWN, FAILED}`.

`READY`'s successor set gains `RESTORING`. `PAUSED` is **not** `READY`, which keeps the
`READY ⇒ running` invariant intact: snapshot admission (`include_memory` needs a running guest)
and the SSH tools (`ssh_access.py`, `ssh_reachable.py`) that gate on `state is READY` all
correctly exclude a suspended guest. A `PAUSED` guest is not SSH-reachable (its kernel is not
executing); that is expected and documented, not a silent inconsistency.

Adding two `SystemState` values ripples into every state-exhaustive site. Because a hand-picked
list is provably incomplete (this review found three state-keyed gates — the power plane and
`console_rotate` — that an earlier enumeration missed), the guard test is a **discovery sweep**:
it greps the tree for `frozenset[SystemState]` / `SystemState`-membership sets and `state is …
READY` gates and fails when a new `SystemState` is absent from a set it should join (or unlisted
in an explicit allow-list of gates that intentionally exclude it). The plan enumerates each site
as an explicit step; the sweep is the backstop that catches any the plan misses:

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
- `jobs/handlers/console/console_rotate.py` `_LIVE_STATES` — add `RESTORING`, `PAUSED` so console
  **sealing/rotation** keeps running while paused/reverting (this set is distinct from
  `console_hosting`'s live set; leaving it unchanged would silently suspend sealing during those
  windows — conservative, not data loss, but inconsistent with keeping the stream live).
- **`mcp/tools/debug/sessions/lifecycle.py` — the `debug.start_session` gate is widened from
  `state is READY` to `state in {READY, PAUSED}`.** A `PAUSED` guest is exactly the gdbstub
  attach target: with drgn-live unavailable on a suspended guest, a gdbstub `debug.*` session is
  the *only* inspection path, so the `start_paused` workflow is dead unless debug admission
  accepts `PAUSED`. The other `debug.*` tools operate on an already-open session (keyed on the
  `DebugSession` row, not the System state), so only the session-*open* gate needs widening; a
  test asserts `debug.start_session` succeeds against a `PAUSED` System.
- **The `control.power` plane — admission `power_system` and worker `_power_target`.** Both today
  reject *every* action from any non-`READY` state ("power requires a READY system"). They are
  widened for the new `RESUME` action only: `RESUME` is admitted **iff** the System is `PAUSED`
  (rejected from every other state), and every non-`RESUME` action still requires `READY` (so
  `PAUSED`/`RESTORING` continue to refuse ON/OFF/CYCLE/RESET). See the resume-path spec below.
- **`systems.teardown` — admissible from `PAUSED`.** Teardown does not gate on `READY` (it only
  short-circuits an already-`TORN_DOWN` System); the `PAUSED → TORN_DOWN` edge is reachable via
  `systems.teardown`, so an agent that pause-restored can abandon the System without resuming
  first. Confirmed present in the enumeration and covered by a test.
- **`mcp/tools/debug/sessions/lifecycle.py` — the `debug.start_session` gate is widened from
  `state is READY` to `state in {READY, PAUSED}`.** A `PAUSED` guest is exactly the gdbstub
  attach target: with drgn-live unavailable on a suspended guest, a gdbstub `debug.*` session is
  the *only* inspection path, so the `start_paused` workflow is dead unless debug admission
  accepts `PAUSED`. The other `debug.*` tools operate on an already-open session (keyed on the
  `DebugSession` row, not the System state), so only the session-*open* gate needs widening; a
  test asserts `debug.start_session` succeeds against a `PAUSED` System.
- **`systems.teardown` — admissible from `PAUSED`.** Teardown does not gate on `READY` (it only
  short-circuits an already-`TORN_DOWN` System); the `PAUSED → TORN_DOWN` edge is reachable via
  `systems.teardown`, so an agent that pause-restored can abandon the System without resuming
  first. Confirmed present in the enumeration and covered by a test.

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
  discards the running guest, corrupting an active Run); **rejects if an active `SNAPSHOT` or
  `DELETE_SNAPSHOT` job exists for the System** (`configuration_error`, "a snapshot capture/delete
  is in progress"). This is what makes the `RESTORING` fence a true domain-exclusivity guarantee:
  both `SNAPSHOT` and `DELETE_SNAPSHOT` stay `READY` and do not fence via state, so without this
  check a restore could be admitted while a capture's off-thread `snapshotCreateXML` — or a
  delete's off-thread multi-GB snapshot merge/removal — still runs on the same domain (a revert
  landing mid-op → the losing job fails, and if the restore loses, `RESTORING → FAILED`). This is
  the mirror of the `delete_snapshot`-rejects-`RESTORING` guard, closing both orderings. Admission
  queries the job queue for a non-terminal `SNAPSHOT` / `RESTORE` / `DELETE_SNAPSHOT` job on this
  `system_id` before transitioning. **Also refuses if a debug session is attached** to the
  System (an open gdbstub `DebugSession` on the System's Run): a revert replaces the machine state
  under the attached gdbstub and would silently break it, so — symmetric with the live-Run guard —
  restore requires the agent to `debug.end_session` first (the paused-restore workflow then attaches
  a *fresh* session post-revert). Transitions the System `READY →
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
- **Paused restore & resume (the `control.power` `RESUME` path):** `start_paused=True` reverts
  into libvirt's *paused* domain state (`VIR_DOMAIN_SNAPSHOT_REVERT_PAUSED`) and the System lands
  in **`PAUSED`**. The agent attaches a gdbstub `debug.start_session`, inspects/sets breakpoints,
  then **resumes with `control.power(system_id, action="resume")`** — a new `PowerAction.RESUME`
  (added to the `PowerAction` enum `ON/OFF/CYCLE/RESET`) → `virDomainResume`. This grafts onto the
  existing `control.power` job with three deliberate changes, each enumerated as a state-site above:
  - **Admission** (`power_system`): today rejects any non-`READY` state for every action. Widened
    so `RESUME` is admitted **iff** `PAUSED` (else `configuration_error`); every other action still
    requires `READY`, so `RESUME` from `READY` and ON/OFF/CYCLE/RESET from `PAUSED`/`RESTORING` are
    all refused.
  - **Worker** (`_power_target` / `power_handler`): today raises "power requires a READY system"
    and is documented to "move no System state". For `RESUME` only, it accepts a `PAUSED` target
    and **commits `PAUSED → READY`** under the SYSTEM lock — an explicit, documented exception to
    the move-no-state rule (every other action still moves no state). A failed `virDomainResume`
    routes **`PAUSED → FAILED`** (`_record_system_failure`); the guest is left in an indeterminate
    power state, so `PAUSED` is not a safe landing.
  - `PowerAction` is serialized in the job payload (no CHECK constraint), so `RESUME` adds no
    migration; only the enum + these two gates change.

  `suggested_next_actions` on a `start_paused` restore names `debug.start_session` and
  `control.power`. drgn-*live* over SSH does not work against a paused guest (the kernel is not
  executing); gdbstub-based `debug.*` does. Documented on the tool.

### `systems.list_snapshots` — synchronous read

- **Params:** `system_id: str`. **Annotation:** `read_only()`. **RBAC:** `viewer`.
- Returns `ToolResponse.collection` (mirroring `systems.list`) of the System's `snapshots` rows
  from Postgres — `name`, `include_memory`, `state`, `created_at` — newest first. No libvirt
  round-trip. A supported provider with no snapshots returns an empty collection; an unsupported
  provider returns `capability_unsupported`.

### `systems.delete_snapshot` — long op (`JobKind.DELETE_SNAPSHOT`)

- **Params:** `system_id: str`; `name: str`. **Annotation:** `mutating()`. **RBAC:** `contributor`.
- **Async, not synchronous:** deleting an internal **memory** snapshot frees/merges the same
  multi-GB qcow2 clusters that made *capture* a job, so a blocking inline delete would stall the
  server request and hold the SYSTEM lock for the whole duration. It is therefore a worker job, on
  the worker plane with the other slow provider ops (the server never blocks on long provider I/O).
- **Admission (under `advisory_xact_lock(SYSTEM)`, Postgres-only):** the named row must exist
  (else `configuration_error`); rejects a `creating` snapshot (cancel the capture first); rejects
  if a `DELETE_SNAPSHOT` job is already in flight for `(system_id, name)`; **rejects while the
  System is `RESTORING`** (a concurrent `restore(name)` could otherwise revert a snapshot this
  delete is removing — the delete winning the race would fail the revert `CONFIGURATION_ERROR →
  FAILED`; the fence closes the mirror of the restore-rejects-in-flight-SNAPSHOT guard). Enqueues
  `JobKind.DELETE_SNAPSHOT` with `SnapshotDeletePayload(system_id, name)`, dedup key
  `{system_id}:delete_snapshot:{name}` with `recycle_terminal=True, recycle_canceled=True`
  (symmetric with snapshot/restore — so a delete that failed on a transient
  `INFRASTRUCTURE_FAILURE` or was canceled mid-merge is retryable instead of returning the dead
  job forever and wedging the name/disk until teardown), audits `delete_snapshot`, returns the job
  handle. **No System-state transition** — deletion does not disturb the guest.
- **Worker handler** (`snapshot_delete_handler`): calls `runtime.snapshot.delete(domain_name,
  name)` (idempotent) off-thread to free the libvirt snapshot + qcow2 space, then removes the
  ledger row under the SYSTEM lock. This frees the name for reuse and reclaims disk before
  teardown. Cancelable (contributor); a mid-delete cancel is safe (idempotent delete, row removal
  is the last step).

## Teardown — snapshots are freed on release

Snapshots are System-scoped and released with the System (the durable-objects invariant: a child
never outlives its parent). `teardown_handler` (`jobs/handlers/systems.py`) is made
snapshot-aware:

1. **Delete libvirt snapshot metadata before undefine.** libvirt refuses to `undefine` a domain
   that still has snapshot metadata unless `VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA` is passed. The
   flag is added to the **shared provider undefine primitive** (`_teardown_domain` in
   `local_libvirt/lifecycle/provisioning.py`), **not** only to the teardown job — that primitive is
   also on the reprovision path (`reprovision = teardown + provision`), so a bare undefine there
   would fail on a snapshotted System too (see the reprovision note below). Teardown additionally
   calls `runtime.snapshot.delete_all(domain_name)` (idempotent) to reap every libvirt snapshot,
   including any cancel-orphaned one whose ledger row is `failed`/`creating`.
2. **The qcow2 deletion frees the data.** Internal snapshots live inside the disk image teardown
   already removes; no external file or S3 object to leak.
3. **Ledger rows cascade.** `snapshots.system_id → systems(id) ON DELETE CASCADE` removes the
   rows when the System row is deleted; if teardown soft-transitions the System instead of
   deleting the row, the reclaim step deletes the `snapshots` rows explicitly alongside the
   existing System-owned artifact reclaim.

A `torn_down` System leaves **no** `snapshots` rows and **no** libvirt snapshot metadata,
including snapshots orphaned by a canceled capture.

### Reprovision invalidates a System's snapshots

`systems.reprovision` is `teardown + provision` on the same provider primitive: it destroys+
undefines the domain (now with the metadata flag, so it no longer fails on a snapshotted System)
and **recreates the qcow2 overlay**, which destroys the internal libvirt snapshots living inside
it. Those snapshots cannot survive a reprovision, and leaving their `available` ledger rows behind
would be a correctness trap — a later `systems.restore` would revert a snapshot that no longer
exists (`CONFIGURATION_ERROR` → `FAILED`). So `reprovision_handler` **deletes the System's
`snapshots` ledger rows** as part of the reprovision commit (the libvirt snapshots are already gone
with the old overlay; `delete_all` on the fresh domain is a harmless no-op). Reprovision is a full
guest rebuild — discarding checkpoints of the old guest is the correct semantics, and it is
stated on the `systems.reprovision` docstring. An acceptance criterion covers reprovision of a
snapshotted System: undefine does not fail, and no stale `available` rows remain.

## Concurrency, safety, recovery, observability

- **Advisory lock:** every snapshot/restore/delete admission and every worker-side state commit
  runs under `advisory_xact_lock(conn, LockScope.SYSTEM, system_id)` inside the same transaction,
  the reprovision/power pattern.
- **Restore fences via `RESTORING`:** while a System is `RESTORING`, reprovision/power/teardown/
  another restore/snapshot/**delete_snapshot** are refused at admission (they require `READY`, and
  `delete_snapshot` explicitly refuses `RESTORING`), so the disruptive revert has exclusive
  control of the domain — **provided** restore admission also rejects an already-in-flight
  `SNAPSHOT` job (above), which a fresh `RESTORING` state cannot recall. `PAUSED` refuses new Runs
  and non-resume power actions until the agent resumes, but **does** admit `debug.start_session`
  (the paused-attach path) and `systems.teardown` (abandon-without-resume).
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
  - **Stranded `creating` snapshot recovery.** Symmetric to the RESTORING limbo, a `SNAPSHOT`
    worker that dies (or a cancel that races the handler) can strand a `snapshots` row in
    `creating` with no active job. A new **`repair_stalled_creating_snapshots`** sweep transitions
    a `creating` row whose `SNAPSHOT` job is terminal (or absent) to `failed`, so it becomes
    reclaimable by the failed-row recycle path. This backstops the admission-time job-liveness
    check above (which handles the common re-issue case) for the case where the agent never
    re-issues the name.
  - **Repair-vs-completing-restore ordering.** The repair cannot clobber a succeeding restore: the
    `restore_handler` commits the `RESTORING → READY|PAUSED` transition (under the SYSTEM lock)
    **before it returns**, and the worker framework marks the `RESTORE` job terminal only *after*
    the handler returns (`worker.py`: `handler(...)` then `queue.complete(...)`). So on the success
    path the System is already `READY`/`PAUSED` when the job becomes terminal — the repair's
    `state is RESTORING` guard fails and it no-ops. The only state a repair can act on is a genuine
    terminal-job-plus-still-`RESTORING`, which is exactly a failed/canceled revert, for which
    `FAILED` is correct. A test races the repair against a completing restore to lock this in.
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
  `jobs.wait`, so there is no external tool timeout. In-guest, a live Run's TCP sockets (SSH)
  survive the pause at the transport layer and resume — but a pause of tens of seconds **can still
  trip application-level timeouts inside the Run** (an SSH command timeout, the reproducer's own
  liveness check, an orchestration heartbeat) even though the socket is intact. The tool docstring
  states the pause scales with guest RAM and surfaces the expectation so an agent can quiesce a
  sensitive Run first or choose `include_memory=False` (no pause of that magnitude).
- **Disk-only snapshots of a running guest are crash-consistent.** kdive assumes no in-guest
  qemu-guest-agent, so disk-only capture does **not** `fsfreeze`/quiesce; the disk image is
  crash-consistent (equivalent to a hard reset at the capture instant), and a restored disk-only
  snapshot may run a journal recovery / `fsck` on next boot. Documented as a caveat on the
  `include_memory=False` path; memory snapshots do not have this caveat (the FS state is captured
  coherently with the paused RAM).

## Persistence / migration (`0071_system_snapshots.sql`, forward-only)

1. `CREATE TABLE snapshots (...)` with the columns above, `UNIQUE (system_id, name)`,
   `system_id ... ON DELETE CASCADE`, `snapshots_state_check`, and the `_set_updated_at` trigger.
2. Drop-and-recreate `jobs_kind_check` widened with `'snapshot'`, `'restore'`,
   `'delete_snapshot'` (the `0069` pattern; keeps the constraint name for the SQL↔enum tie in
   `test_migrate.py`). All three join `ACTIVE_JOB_KINDS`; `SNAPSHOT`/`RESTORE`/`DELETE_SNAPSHOT`
   join `CONTRIBUTOR_CANCELABLE_JOB_KINDS`.
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
   (running restore). A restore with a live Run, with an active `SNAPSHOT` or `DELETE_SNAPSHOT`
   job for the System, or with an attached debug session is refused `configuration_error`.
3b. Restoring a disk-only snapshot reboots the guest and lands `READY` (not at an instruction);
   `start_paused=True` against a disk-only snapshot is refused `configuration_error` naming the
   mode mismatch.
4. `start_paused=True` (memory snapshot) reverts the guest paused and lands the System in
   `PAUSED`; `control.power(action="resume")` is admitted only from `PAUSED`, transitions
   `PAUSED → READY`, and resumes the guest; a failed `virDomainResume` routes `PAUSED → FAILED`.
   `RESUME` from `READY`, and ON/OFF/CYCLE/RESET from `PAUSED`/`RESTORING`, are all refused
   `configuration_error`. A `PAUSED` System is refused a new Run and is not SSH-reachable, **but
   `debug.start_session` succeeds against it** (the gdbstub attach path) and `systems.teardown` is
   admissible from it. `suggested_next_actions` on the paused restore names `debug.start_session`
   and `control.power`.
5. `systems.list_snapshots(system_id)` returns the System's snapshots newest-first from Postgres,
   no libvirt round-trip. `systems.delete_snapshot(system_id, name)` enqueues a `DELETE_SNAPSHOT`
   job that deletes the libvirt snapshot and removes the ledger row (freeing the name for reuse);
   deleting a `creating` snapshot is refused, admission does no libvirt I/O under the lock, and
   `delete_snapshot` is refused while the System is `RESTORING`. A retry after a failed/canceled
   `DELETE_SNAPSHOT` starts a fresh job (recycle flags) and eventually frees the name + disk.
6. On a provider with `supports_snapshots is False`, all four tools return `capability_unsupported`
   (`capability="snapshot"`); `systems.get` surfaces `data.supports_snapshots` for both provider
   kinds without a libvirt call.
7. Tearing down a snapshotted System deletes libvirt snapshot metadata (undefine does not fail),
   frees the data with the qcow2, and leaves **no** `snapshots` rows — including a snapshot
   orphaned by a canceled capture.
7b. `systems.reprovision` of a snapshotted System does not fail at undefine (the metadata flag is
   in the shared provider primitive), and the reprovision commit deletes the System's `snapshots`
   ledger rows so no stale `available` row survives pointing at the destroyed overlay.
8. `SNAPSHOT`/`RESTORE`/`DELETE_SNAPSHOT` are in `ACTIVE_JOB_KINDS` and
   `CONTRIBUTOR_CANCELABLE_JOB_KINDS`; a contributor can cancel its own job. A canceled restore
   lands the System in `FAILED`; a System stuck in `RESTORING` with no active `RESTORE` job is
   recovered to `FAILED` by `repair_stalled_restoring_systems`, and that repair no-ops against a
   restore that has already committed `READY`/`PAUSED`. A `snapshots` row stranded in `creating`
   (worker death / raced cancel) is recovered to `failed` by `repair_stalled_creating_snapshots`
   and by the admission-time job-liveness check, so a dead capture never wedges the name.
9. A **discovery-sweep** guard test enumerates the tree's `SystemState`-membership sets and
   `state is …READY` gates and fails when a new `SystemState` is unaccounted for — covering
   `_NON_TERMINAL_SYSTEM`, admission's non-terminal set, `console_hosting` live set,
   `console_rotate._LIVE_STATES`, the adjacency table, the `debug.start_session` gate, and the
   `control.power` admission/worker gates. `RESTORING`/`PAUSED` are present in each set they must
   join, and each intentional exclusion (e.g. `PAUSED` not launchable for a new Run) is an explicit
   allow-list entry, so the guard is not a hand-picked subset that silently misses a site.
10. Migration `0071` creates `snapshots`, widens `jobs_kind_check`
    (`snapshot`,`restore`,`delete_snapshot`) and `systems_state_check` (`restoring`,`paused`);
    `test_migrate.py` and the per-migration test stay green.
11. The `systems` toolset guide and agent index document the four tools, the capability
    advertisement, the paused-restore→resume workflow, the disk-only crash-consistency and
    memory-pause caveats, and the "freed on release" contract.
