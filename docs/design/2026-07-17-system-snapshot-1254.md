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
  "restore → break → continue".
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
| `id` | uuid PK | minted per snapshot |
| `system_id` | uuid NOT NULL | `REFERENCES systems(id) ON DELETE CASCADE` — snapshot never outlives its System |
| `name` | text NOT NULL | agent-chosen; the libvirt snapshot name; `UNIQUE (system_id, name)` |
| `include_memory` | boolean NOT NULL | RAM+disk vs disk-only |
| `state` | text NOT NULL | `creating` / `available` / `failed`; `snapshots_state_check` |
| attribution | | `principal`, `agent_session`, `project` (the `Attribution` mixin) |
| `created_at` / `updated_at` | timestamptz | DB-owned via a `_set_updated_at` trigger, mirroring `runs` |

`SNAPSHOTS = StatefulRepository(Snapshot, "snapshots", SnapshotState, ...)` in `db/repositories.py`.

`SnapshotState` StrEnum + adjacency table in `domain/capacity/state.py`:
`CREATING → {AVAILABLE, FAILED}`, `AVAILABLE → {FAILED}` (a failed revert-in-place does not
consume the snapshot), `FAILED` terminal. Deletion is row removal, not a state.

Chosen over (a) libvirt-as-source-of-truth (`list_snapshots` queries the hypervisor): loses the
Postgres state-of-record invariant, makes snapshots invisible to audit/teardown/reconciler, and
forces a live libvirt round-trip on a read; and (b) a full six-object-style durable object with
its own MCP lifecycle: heavier than the child-ledger the data needs. See ADR-0378 alternatives.

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
- **Enforcement:** `systems.snapshot` / `systems.restore` / `systems.list_snapshots` on a
  provider with `supports_snapshots is False` return the existing `capability_unsupported`
  envelope (`mcp/tools/_common.py`), `capability="snapshot"`, `supported=[]`.

### The `Snapshotter` port

`providers/ports/lifecycle.py` gains a `Snapshotter` Protocol; `ProviderRuntime` gains an
optional `snapshot: Snapshotter | None = None` group (like `debug` / `rootfs`). `None` = plane
unsupported (kept consistent with `supports_snapshots is False`).

```
class Snapshotter(Protocol):
    def create(self, domain_name: str, name: str, *, include_memory: bool) -> None: ...
    def revert(self, domain_name: str, name: str, *, start_paused: bool) -> None: ...
    def delete(self, domain_name: str, name: str) -> None: ...
    def list(self, domain_name: str) -> list[SnapshotInfo]: ...  # for reconcile/verify only
```

`LocalLibvirtSnapshotter` (`local_libvirt/lifecycle/snapshot.py`) mirrors `LocalLibvirtControl`:
a `connect: Callable[[], _LibvirtConn]` factory and a narrow `_LibvirtDomain` Protocol extended
with `snapshotCreateXML` / `revertToSnapshot` / `snapshotLookupByName` /
`listAllSnapshots`. `create` builds the snapshot XML with `<memory snapshot='internal|no'/>`
and `<disk .../>` internal, calling `snapshotCreateXML` (no `DISK_ONLY` flag ⇒ full system
checkpoint when `include_memory`; `VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY` when not). `revert`
calls `revertToSnapshot` with `VIR_DOMAIN_SNAPSHOT_REVERT_RUNNING` or `..._REVERT_PAUSED`.
Errors map to `CategorizedError` (`INFRASTRUCTURE_FAILURE` for libvirt faults,
`CONFIGURATION_ERROR` for a missing snapshot on revert).

## Tools (agent-facing contracts)

All three live in the existing `systems` toolset registrar
(`mcp/tools/lifecycle/systems/registrar.py`), each a new `_register_systems_*` call inside
`register()`. All resolve the provider runtime via `with_runtime_for_system` (which also enforces
`required_role`) and refuse a non-snapshot provider with `capability_unsupported`.

### `systems.snapshot` — long op (`JobKind.SNAPSHOT`)

- **Params:** `system_id: str`; `name: Annotated[str, Field(...)]` (agent-chosen label,
  validated non-empty and to a libvirt-safe charset `[A-Za-z0-9._-]`, ≤64 chars);
  `include_memory: Annotated[bool, Field(...)] = True`.
- **Annotation:** `mutating()`. **RBAC:** `contributor` on the System's project (leaseholder over
  its own System). Registered in `exposure.py` as `_CONTRIBUTOR`.
- **Admission (synchronous, under `advisory_xact_lock(SYSTEM)`):** System exists in a granted
  project; caller has `contributor`; provider `supports_snapshots`; System is `READY`
  (`include_memory` requires a running guest, and `READY` ⇒ running). **A live Run does NOT
  block a snapshot** — the primary use case is snapshotting mid-debug. Inserts a `snapshots` row
  in `creating`, audits `snapshot`, enqueues `JobKind.SNAPSHOT` with
  `SnapshotPayload(system_id, name, include_memory)`, dedup key `{system_id}:snapshot:{name}`
  (so re-issuing the same name returns the same job — idempotent capture). Returns
  `job_envelope(job, "system_id", uid)` → `{job_id, status: queued}`; `suggested_next_actions =
  ["jobs.wait"]`.
- **The System stays `READY` throughout.** Snapshot is non-destructive to System identity; it
  does not transition state. A memory capture briefly pauses the guest (libvirt), so a live
  Run's SSH stalls then resumes — non-fatal, documented on the tool. Concurrent snapshots on one
  System serialize via libvirt's per-domain job lock.
- **Worker handler** (`jobs/handlers/systems.py`, `snapshot_handler`): loads `SnapshotPayload`,
  resolves the binding, re-verifies `READY` at start, calls
  `runtime.snapshot.create(domain_name, name, include_memory=...)` off-thread; on success
  transitions the `snapshots` row `creating → available` under `advisory_xact_lock(SYSTEM)`; on
  `CategorizedError` transitions the row `creating → failed` and marks the error terminal. The
  **System row is never touched.** Returns `str(snapshot_id)` as `result_ref`.

### `systems.restore` — long op (`JobKind.RESTORE`), fenced by `RESTORING`

- **Params:** `system_id: str`; `name: Annotated[str, Field(...)]` (an existing `available`
  snapshot); `start_paused: Annotated[bool, Field(...)] = False`.
- **Annotation:** `mutating()`. **RBAC:** `contributor` (like reprovision — restore is a
  leaseholder lifecycle op, **not** the `force_crash` destructive gate, which stays reserved for
  `force_crash`).
- **Admission (synchronous, under `advisory_xact_lock(SYSTEM)`):** System `READY` in a granted
  project; caller `contributor`; provider `supports_snapshots`; the named snapshot exists and is
  `available` (else `configuration_error`); **rejects if a live Run exists** (`_has_live_run`,
  the reprovision rule) — restore discards the running guest, which would corrupt an active Run.
  Transitions the System `READY → RESTORING` (the new guarded edge), audits
  `ready→restoring`, enqueues `JobKind.RESTORE` with `RestorePayload(system_id, name,
  start_paused)`, dedup key `{system_id}:restore:{name}:{start_paused}`. Returns the job handle.
- **Worker handler** (`restore_handler`): re-verifies `RESTORING`, resolves binding, calls
  `runtime.snapshot.revert(domain_name, name, start_paused=...)` off-thread; on success
  transitions `RESTORING → READY` under the SYSTEM lock (mirroring `_commit_reprovision_result`),
  audits `restoring→ready`; on `CategorizedError` transitions `RESTORING → FAILED`
  (`_record_system_failure`). Returns `str(system_id)`.
- **Paused restore & resume:** `start_paused=True` reverts into libvirt's *paused* domain state
  (`VIR_DOMAIN_SNAPSHOT_REVERT_PAUSED`); the System returns to `READY` with the guest suspended.
  The agent attaches a gdbstub `debug.start_session`, inspects/sets breakpoints, then **resumes
  the guest with `systems.power(system_id, action="resume")`** — a new `PowerAction.RESUME` →
  `virDomainResume` (an enum member + one `_apply_power` branch; PowerAction is serialized in the
  job payload, no CHECK constraint, so no migration). `suggested_next_actions` on a
  `start_paused` restore names `debug.start_session` and `systems.power`. drgn-*live* over SSH
  does not work against a suspended guest (the kernel is not executing); gdbstub-based `debug.*`
  does. This is documented on the tool.

### `systems.list_snapshots` — synchronous read

- **Params:** `system_id: str`.
- **Annotation:** `read_only()`. **RBAC:** `viewer` on the System's project.
- Returns `ToolResponse.collection` (mirroring `systems.list`) of the System's `snapshots` rows
  from Postgres — `name`, `include_memory`, `state`, `created_at` — newest first. No libvirt
  round-trip. An empty list for a snapshot-incapable provider is preceded by the
  `capability_unsupported` refusal only when the provider lacks support; a supported provider with
  no snapshots returns an empty collection.

## Teardown — snapshots are freed on release

Snapshots are System-scoped and released with the System (the durable-objects invariant: a child
never outlives its parent). `teardown_handler` (`jobs/handlers/systems.py`) is made
snapshot-aware:

1. **Delete libvirt snapshot metadata before undefine.** libvirt refuses to `undefine` a domain
   that still has snapshot metadata unless `VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA` is passed (or
   the snapshots are deleted first). Teardown deletes each snapshot (`runtime.snapshot.delete`) —
   or passes the undefine flag — so undefine cannot fail on a snapshotted System.
2. **The qcow2 deletion frees the data.** Internal snapshots live inside the disk image teardown
   already removes; no external file or S3 object to leak.
3. **Ledger rows cascade.** `snapshots.system_id → systems(id) ON DELETE CASCADE` removes the
   rows when the System row is deleted; if teardown soft-transitions the System instead of
   deleting the row, the reclaim step deletes the `snapshots` rows explicitly alongside the
   existing System-owned artifact reclaim.

A `torn_down` System leaves **no** `snapshots` rows and **no** libvirt snapshot metadata.

## Concurrency, safety, observability

- **Advisory lock:** every snapshot/restore admission and every worker-side state commit runs
  under `advisory_xact_lock(conn, LockScope.SYSTEM, system_id)` inside the same transaction, the
  reprovision/power pattern.
- **Restore fences via `RESTORING`:** while a System is `RESTORING`, reprovision/power/teardown/
  another restore/snapshot are refused at admission (they require `READY`), so the disruptive
  revert has exclusive control of the domain.
- **Snapshot does not fence via state** (stays `READY`, permitted during a live Run); concurrent
  snapshots on one System serialize via libvirt's per-domain job lock, and a teardown that lands
  mid-snapshot (agent-owned System, unlikely) surfaces as a snapshot-job `INFRASTRUCTURE_FAILURE`
  with the `snapshots` row `failed` — non-fatal to the System.
- **Cancelable:** `SNAPSHOT` and `RESTORE` join `CONTRIBUTOR_CANCELABLE_JOB_KINDS` so a
  contributor can cancel its own job (the gate fails closed to operator-only otherwise). A
  canceled snapshot leaves its row in `creating`; a reconcile/re-issue path transitions it
  `creating → failed` (the dedup key is per-name, so a fresh call reuses the slot after the row
  is `failed`/removed). A canceled restore leaves the System `RESTORING`; the reconciler’s
  existing stuck-transition sweep returns it to a terminal state.
- **Idempotency:** snapshot dedup key `{system_id}:snapshot:{name}` makes "snapshot N" idempotent;
  a second call while one is in flight returns the same job.
- **Audit:** snapshot/restore/list are audited (tool, `system_id`, `name`, `include_memory` /
  `start_paused`, outcome), like the other lifecycle ops.
- **Telemetry:** per-kind job telemetry surfaces `SNAPSHOT`/`RESTORE` success/failure rates.
- **Redaction:** no guest console/memory content enters any response — the tools return only
  ledger metadata and job handles — so there is no new redaction surface.

## Persistence / migration (`0071_system_snapshots.sql`, forward-only)

1. `CREATE TABLE snapshots (...)` with the columns above, `UNIQUE (system_id, name)`,
   `system_id ... ON DELETE CASCADE`, `snapshots_state_check`, and the `_set_updated_at` trigger.
2. Drop-and-recreate `jobs_kind_check` widened with `'snapshot'`, `'restore'` (the `0069`
   pattern; keeps the constraint name for the SQL↔enum tie in `test_migrate.py`).
3. Drop-and-recreate `systems_state_check` widened with `'restoring'` (the `0065` pattern).

No change to `PowerAction` persistence (payload jsonb, no CHECK).

## Out of scope (explicit)

- **Non-local-libvirt providers** — `supports_snapshots` is `False` for remote-libvirt and
  fault-inject in this change; remote-libvirt snapshot support is a follow-up if a need is
  established (the provider abstraction already accommodates it via the `Snapshotter` port).
- **A snapshot as a debug/analysis input** — a savevm image is not a vmcore; the `vmcore.*` /
  drgn-offline plane is untouched.
- **External snapshots / S3-stored memory state** — internal qcow2 snapshots only.
- **Cross-System / cross-Allocation snapshot transfer, snapshot export/download** — no established
  need; snapshots are ephemeral checkpoints tied to one System's lifetime.
- **A retention cap / auto-expiry of snapshots** — snapshots are bounded by the qcow2's host disk
  and freed at teardown; a per-System count cap is a follow-up if disk pressure is observed
  (noted, not designed out).

## Acceptance criteria

1. `systems.snapshot(system_id, name)` on a `READY` local-libvirt System inserts a `creating`
   `snapshots` row and enqueues a `SNAPSHOT` job returning `{job_id, status: queued}`; the
   handler creates a libvirt internal snapshot (RAM+disk by default) and drives the row to
   `available`. The System stays `READY`, and the call succeeds **even while a live Run exists**.
2. `include_memory=False` produces a disk-only snapshot (`VIR_DOMAIN_SNAPSHOT_CREATE_DISK_ONLY`);
   `include_memory=True` (default) produces a full system checkpoint.
3. `systems.restore(system_id, name)` on a `READY` System with an `available` snapshot and **no**
   live Run transitions `READY → RESTORING`, enqueues a `RESTORE` job, reverts the domain, and
   returns the System to `READY`. A restore with a live Run is refused `configuration_error`.
4. `start_paused=True` reverts the guest into a paused state; `systems.power(action="resume")`
   resumes it. `suggested_next_actions` names `debug.start_session` and `systems.power`.
5. `systems.list_snapshots(system_id)` returns the System's snapshots (name, include_memory,
   state, created_at) newest-first from Postgres, with no libvirt round-trip.
6. On a provider with `supports_snapshots is False`, all three tools return `capability_unsupported`
   (`capability="snapshot"`); `systems.get` surfaces `data.supports_snapshots` for both provider
   kinds without a libvirt call.
7. Tearing down a snapshotted System deletes the libvirt snapshot metadata (undefine does not
   fail), frees the snapshot data with the qcow2, and leaves **no** `snapshots` rows.
8. `SNAPSHOT`/`RESTORE` are in `ACTIVE_JOB_KINDS` and `CONTRIBUTOR_CANCELABLE_JOB_KINDS`; a
   contributor can cancel its own snapshot/restore job.
9. Migration `0071` creates `snapshots`, widens `jobs_kind_check` (`snapshot`,`restore`) and
   `systems_state_check` (`restoring`); `test_migrate.py` and the per-migration test stay green.
10. The `systems` toolset guide and agent index document the three tools, the capability
    advertisement, the paused-restore→resume workflow, and the "freed on release" contract.
