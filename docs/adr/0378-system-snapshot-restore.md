# ADR 0378 — System snapshot / restore as a provider-advertised, System-scoped checkpoint

- **Status:** Accepted
- **Date:** 2026-07-17
- **Deciders:** kdive maintainers

## Context

Issue #1254 asks for `systems.snapshot` / `systems.restore` / `systems.list_snapshots` plus a
capability advertisement. Kernel debugging is a repro loop, and today the only recovery from a
panic is a reboot or a full `systems.reprovision` — both discard the configured guest (installed
packages, staged reproducer, armed kdump) and cost minutes. A libvirt disk(+memory) snapshot lets
the agent checkpoint a fully-configured, running guest and roll back in seconds — including
"snapshot just before triggering the bug, restore, retry with different breakpoints." This is
impossible from inside the guest (freezing live RAM + CPU is a hypervisor operation) and fits the
existing async job model (capture returns `{job_id}`). A future bare-metal provider cannot offer
it, so support must be advertised, not assumed.

## Decision

Add three `systems.*` tools backed by a new `Snapshotter` provider port, a `snapshots` Postgres
child ledger, and a static provider capability flag.

- **Scope is caller-selectable RAM+disk (default) or disk-only** (`include_memory`, default
  `true`). The repro-loop use case requires live memory so restore resumes at the exact
  instruction with kdump/reproducer/modules intact; disk-only stays available for the cheap
  filesystem rollback. **Restore can land the guest paused** (`start_paused`, default `false`) via
  `VIR_DOMAIN_SNAPSHOT_REVERT_PAUSED` so the agent can attach a gdbstub `debug.*` session and set
  breakpoints before execution resumes; it resumes with a new `systems.power(action="resume")`
  (`PowerAction.RESUME` → `virDomainResume`).

- **Internal libvirt snapshots** stored inside the System's qcow2 — not external memory-state
  files, not S3. This makes "freed on release" near-free (deleting the qcow2 at teardown frees the
  data) and adds no object-store surface. A libvirt savevm image is **not** a crash-format vmcore,
  so the `vmcore.*` / drgn-offline analysis plane is untouched; snapshots are a lifecycle/rollback
  primitive whose payoff is realized *through* the live `debug.*` tools after a restore.

- **A `snapshots` child ledger** (`system_id → systems(id) ON DELETE CASCADE`, `UNIQUE
  (system_id, name)`, a `SnapshotState` machine `creating → available|failed`) is the Postgres
  index-of-record for listing, audit, and teardown cleanup, while libvirt holds the RAM+disk data.
  A snapshot is a child of the System, exactly like `run_steps` under a Run.

- **Capability advertised via the static `ProviderSupport` descriptor** (ADR-0208 pattern):
  `supports_snapshots: bool = False`, set `True` only in local-libvirt. `systems.get` surfaces
  `data.supports_snapshots` (a constant read, no libvirt I/O) for proactive discovery, and the
  three tools refuse an unsupported provider with the existing `capability_unsupported` envelope.

- **Snapshot stays `READY` and is permitted during a live Run** — the primary use case is
  snapshotting a guest mid-debug — so, unlike reprovision, it does not reject on a live Run and
  does not transition System state; concurrent snapshots serialize via libvirt's per-domain job
  lock. **Restore is destructive to a running Run**, so it rejects a live Run (the reprovision
  rule) and transitions `READY → RESTORING → READY|FAILED` (one new state, migration `0071`) to
  fence reprovision/power/teardown out during the revert. Both use `contributor` RBAC and the
  `advisory_xact_lock(SYSTEM)` pattern; neither uses the `force_crash` destructive-op gate, which
  stays reserved for `force_crash`.

- **Snapshots are freed on release.** Teardown is made snapshot-aware: it deletes libvirt snapshot
  metadata before `undefine` (libvirt refuses to undefine a snapshotted domain without
  `VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA`), the qcow2 deletion frees the data, and the ledger
  rows cascade. A `torn_down` System leaves no rows and no libvirt snapshot metadata.

- **Persistence:** one forward-only migration `0071` creates `snapshots`, widens `jobs_kind_check`
  with `snapshot`/`restore`, and widens `systems_state_check` with `restoring`. `SNAPSHOT`/
  `RESTORE` join `ACTIVE_JOB_KINDS` and `CONTRIBUTOR_CANCELABLE_JOB_KINDS`.

## Consequences

- The panic→retry repro loop drops from minutes (reboot/reprovision) to seconds (restore), with
  identical pre-bug state each iteration — the debugging value the issue targets.
- Snapshot support is discoverable before use (`systems.get.data.supports_snapshots`) and enforced
  at call (`capability_unsupported`), so a future bare-metal provider degrades gracefully with no
  agent-visible surprise.
- One new provider port (`Snapshotter`), one new table, one new System state (`RESTORING`), two
  new job kinds, and one new `PowerAction` (`RESUME`). Local-libvirt only in this change; the port
  makes remote-libvirt a later opt-in.
- Snapshots are strictly System-scoped and released with the System, adding no long-lived storage
  and nothing to bill or leak past a release.
- The live `debug.*` tools gain a "restore to a known-good live state, then attach" workflow;
  `systems.power` gains a resume action for the paused-restore case.

## Alternatives considered

- **Disk-only snapshots only.** Simpler and works on a stopped guest, but restore reboots and
  loses live kernel/RAM state — it cannot deliver "snapshot just before the bug, restore, retry,"
  the issue's stated use case. Rejected as the sole mode; kept as a selectable `include_memory=false`.
- **libvirt as the source of truth** (`list_snapshots` queries `listAllSnapshots`, no table).
  Rejected: breaks the "state of record is Postgres" invariant, makes snapshots invisible to
  audit, teardown reclaim, and the reconciler, forces a live libvirt round-trip on a read, and
  complicates multi-host resolution. A thin Postgres ledger keeps the index of record while libvirt
  keeps the bytes.
- **A full six-object-style durable object** for snapshots (own MCP lifecycle, reconciler orphan
  handling, quota). Rejected as heavier than the data needs — a snapshot is a child ledger row, not
  a first-class allocatable object; the `run_steps` child-table shape fits.
- **External snapshots / S3-stored memory state.** Rejected: scatters memory-state files teardown
  would have to track and delete individually (leak surface), and adds object-store plumbing;
  internal qcow2 snapshots are freed with the disk.
- **Gating restore behind the `force_crash` destructive-op gate** (RBAC role + profile opt-in).
  Rejected: the codebase deliberately keeps that gate to exactly `force_crash`; reprovision and
  teardown — equally destructive — gate with plain `require_role`. Restore follows the reprovision
  path (`contributor` + reject-on-live-Run + a fencing state), not the opt-in gate.
- **A transient `SNAPSHOTTING` state for snapshot too** (symmetric with `RESTORING`). Rejected:
  snapshot must be allowed during a live Run and must not disrupt it, so transitioning the System
  out of `READY` would both forbid the primary use case and perturb an active Run. Snapshot is
  non-destructive to System identity; libvirt's per-domain job lock serializes concurrent captures.
- **Treating a snapshot as a debug/analysis artifact** (feed it to `crash`/drgn like a vmcore).
  Rejected as a category error: a savevm resume image is not a crash-format memory dump; the
  existing `vmcore.*` capture plane already covers offline analysis. Snapshots are for rollback.
- **Resuming a paused restore only via the debug session's `continue`.** Rejected as the sole
  path: an agent may pause-restore to inspect without a gdb session; a provider-level
  `systems.power(action="resume")` is a general, discoverable resume independent of a debug session.
