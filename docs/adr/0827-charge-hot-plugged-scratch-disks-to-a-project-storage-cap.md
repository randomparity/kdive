# 0827 — Hot-plugged scratch disks are charged to a per-project storage cap derived from the device ledger

## Status

Proposed

Implementation is tracked by #3720. Nothing in this record is implemented yet.

## Context

ADR-0823 lets a contributor hot-plug up to 8 scratch disks of up to 32 GB into a `READY` System
(`DEVICE_LIMIT`, `DISK_MAX_GB`, `src/kdive/mcp/tools/lifecycle/systems/devices.py:52-54`). The
backing storage is a local qcow2 file or a remote storage volume. ADR-0823 left it uncharged and
told a deployment that needs a limit to file it; #3720 is that issue. The per-System bound (256 GB)
is the only limit today, so a project with many Systems has no ceiling on hot-plugged storage.

The issue frames the work as joining "the existing disk quota/budget dimensions". Checked against
`origin/main` at 76834c6b0, no disk dimension exists in the accounting model:

- `Quota` holds three concurrency caps: `max_concurrent_allocations`, `max_concurrent_systems`,
  `max_pending_allocations` (`src/kdive/domain/accounting/records.py:42-48`). None is a size.
- `Budget` is a kcu running total. The kcu rate is `coeff × (W_CPU × vcpus + W_MEM × memory_gb)`
  (`src/kdive/domain/accounting/cost.py:128-136`); disk is not an input. ADR-0312 records disk as
  "priced-past".
- `validate_disk_against_resource` (`cost.py:225-250`) compares one System's `disk_gb` with the
  host's advertised ceiling at admission and keeps nothing afterwards. The System's own disk is
  bounded per request but not accumulated or charged per project.

The repository does have one per-project storage cap, for private images (ADR-0520,
`services/images/upload.py`). It sums `size_bytes` over the project's live `image_catalog` rows
under the PROJECT lock, refuses with `ErrorCategory.QUOTA_EXCEEDED`, and reads its limit from a
server setting with a non-zero default, `KDIVE_IMAGE_PRIVATE_MAX_BYTES` (50 GiB,
`config/core_settings.py:634-646`), the same for every project. It is the closest precedent for
this decision.

Facts the design rests on:

- `system_devices` carries `kind`, `state` and `size_gb` per row (migration `0200`). A row exists
  from admission until a successful un-plug deletes it, and ADR-0823's invariant is that a row
  exists exactly while kdive may have a device attached or backing storage to free.
- Plug admission runs under the System advisory lock and writes the `plugging` row and enqueues the
  job in one transaction (`devices.py:215-267`, `270-295`).
- Rows are deleted at five places: the un-plug handler (`jobs/handlers/devices.py`, `_drive_unplug`),
  a plug the port rejects with `configuration_error` and no handle (`_delete_row` in the plug
  handler), teardown (`_reclaim_devices`, `jobs/handlers/systems.py:712-723`), reprovision success
  (`jobs/handlers/systems.py:264`), and system-reclaim (`jobs/handlers/system_reclaim.py:113`).
- The lock total order is `PROJECT → RESOURCE → ALLOCATION → SYSTEM` (`db/locks.py:36-43`,
  ADR-0040). Provisioning already counts a project's Systems under the PROJECT lock
  (`services/systems/admission.py:491-514`, `747-756`).
- A refusal writes nothing and is not recorded for idempotent replay
  (`mcp/tools/lifecycle/support/_idempotency.py:156-159`).

## Decision

**A server-configured per-project cap, `KDIVE_HOTPLUG_DISK_PROJECT_MAX_GB`.** An integer number of
gigabytes, default 256 (one System holding eight 32 GB devices), `>= 0`, read by the server process
at admission. Zero refuses every hot-plugged disk. It follows the ADR-0520 pattern: the same cap
for every project, no schema change, no change to `Quota` or `accounting.set_quota`. A deployment
changes it in its environment and restarts the server, as with the image caps.

**The charge is derived from the ledger, not stored.** The charge of a project is

```sql
SELECT coalesce(sum(d.size_gb), 0)
FROM system_devices d JOIN systems s ON s.id = d.system_id
WHERE s.project = %s AND d.kind = 'disk'
```

over every state: a row in any state may have backing storage, and `lost` and `failed` rows keep it
until an un-plug or teardown frees it. Every disk row carries `size_gb` (`devices.py:104-127`). NICs have no
size and are not charged. There is no counter column and no second table, so a charge cannot drift
from the devices the ledger lists.

**The charge is held from admission.** `_admit_plug` inserts the `plugging` row and the charge
exists from that commit. Admission refuses with `QUOTA_EXCEEDED` when
`charged + requested size_gb > cap`. The check runs after the existing refusals (external-boot,
job in flight, device limit) and before the insert. Plug admission takes the project advisory lock
before the System lock (`PROJECT → SYSTEM`, the order ADR-0040 fixes), so two plugs on different
Systems of one project cannot both pass the check against the same charge.

**The refusal.** `ToolResponse.failure` with `ErrorCategory.QUOTA_EXCEEDED`, `data.reason:
hotplug_disk_quota_exceeded`, and the integers `limit_gb`, `charged_gb`, `requested_gb`.
`suggested_next_actions` is `["systems.unplug_device", "systems.list_devices"]`. No row and no job
are written.

**Release is deleting the row.** Each of the five delete points above lowers the charge the moment
it commits. The un-plug and plug-rejection deletes follow a provider call that freed the storage or
attached nothing; teardown and reprovision success delete after a best-effort `release_all`. This record adds no release code. A row that stays charges: a `failed` plug, a refused
un-plug, a `lost` row after power OFF/CYCLE or restore, and a row the reconciler failed. On a
`READY` System the agent clears each with `systems.unplug_device`, which frees any backing volume
and deletes the row; a `lost` row after a failed reprovision sits on a System that is not `READY`,
so only teardown clears it. The spec lists every path.

**A cap lowered below the charge.** Restarting with a lower cap removes nothing. It refuses new
plugs until un-plugs, teardown or reclaim bring the charge under it.

Exact signatures, the setting definition, lock handling, the failure model and the implementation
split are in the spec, `docs/workflow/specs/2026-11-19-hotplug-disk-quota-3720-design.md`.

## Consequences

- No migration, no `Quota` field and no new tool parameter. The change is one setting, one query,
  one lock acquisition and one refusal, in one PR. The setting joins the generated config
  reference and the operator runbooks.
- The default does not break an upgrade: a project holding one System's maximum (256 GB) is
  admitted, and existing unit fixtures and live cells that plug a few disks pass unchanged.
- Every project shares one cap. A deployment that needs different caps per project cannot have
  them from this record; a per-project override would be an additive column that falls back to the
  setting, owned by no issue until an operator asks.
- Changing the cap needs a server restart, as for every other setting.
- Plug admission now takes the PROJECT lock, so plugs in one project serialize with each other and
  with provisioning for the short admission transaction. Un-plug, teardown and reclaim take no
  project lock: they only lower the charge, and a reader that misses an uncommitted delete refuses
  a plug that would have fit, never admits one that does not.
- A `failed` or `lost` row charges storage that may already be gone (a failed reprovision ran
  `release_all`). The over-charge ends at un-plug or teardown and errs toward refusing. The
  opposite error exists too: teardown and reprovision success delete rows after a best-effort
  `release_all`, so a faulted release stops charging a volume that may still exist (ADR-0823
  accepts that leak).
- A refusal is not audited, like the plug tool's other refusals (`device_limit`, in-flight); the
  image cap audits its denials. An operator learns a project is at its cap from the refusal's
  figures. A deployment that wants a signal can add a log line or metric later.
- During a rolling upgrade, servers still on the old version admit plugs with no check and no
  PROJECT lock, so the cap can be exceeded until every server runs this version.
- The charge is a count of provisioned gigabytes, not a time integral. It adds nothing to the kcu
  budget, and a hot-plugged disk is still free of kcu.
- The host's own disk is not part of the check. A project under its cap can still exceed the
  host's free space; per-host disk capacity is the System-disk ceiling's concern (ADR-0312).
- The tool's `size_gb` bound (1 to 32) and `DEVICE_LIMIT` stay.
- Because the charge reads the ledger, ADR-0823's accepted gaps carry over: a worker that dies
  between the provider call and the commit, or a plug that races teardown, can leave a volume the
  ledger does not show, and that volume is not charged.

## Considered & rejected

- **A per-project column, `quotas.max_hotplug_disk_gb`, set through `accounting.set_quota`.**
  judgment: it gives each project its own cap, but it needs a migration with its window tests, a
  `Quota` field, a `set_quota` parameter with keep-on-omit semantics (the upsert writes every
  column), generated CLI and tool-index updates, and a default. A default of 0 is fail-closed but
  refuses every existing disk plug, unit fixture and lab project until an admin sets it, which
  forces setup scripts and fixtures into the same series; a NULL meaning unlimited is the silent
  default ADR-0007 §4 refuses. The image cap shows a uniform setting is accepted here, and an
  override column can be added later without changing the charge. Flagged for operator review in
  the PR.
- **Join the kcu budget.** judgment: disk is not a kcu input, and ADR-0312 left it unpriced.
  Pricing hot-plugged disk alone would invent a disk rate and coefficient; pricing it consistently
  would change the cost of every System. A budget is spent over time and reconciled to an
  allocation (`LedgerEntry.allocation_id` is required, `records.py:58`, `0002_accounting.sql:48`);
  a plugged disk belongs to a System and its charge ends at an un-plug. Pricing storage is a
  separate decision with no owner here.
- **Reuse `max_concurrent_systems`, or fold the cap into the per-System limit.** judgment: a count
  of Systems is not a size, and the per-System limit (8 × 32 GB) is the bound the issue calls
  insufficient.
- **A stored counter or a charge table.** verified: five code paths delete rows, plus the
  foreign-key cascade, and the reconciler changes row state; a counter needs every one to decrement in the same
  transaction and a repair for the ones that do not, and a missed path silently lowers the
  project's capacity for good. The sum over at most 8 rows per System is the same kind of scan
  `_within_system_quota` already runs on `systems`, and it cannot drift.
- **Charge at settle (when the provider reports `attached`).** judgment: a plug in flight on each
  of N Systems of one project would each pass a check against a charge that includes none of them,
  and overshoot by up to N × 32 GB. The in-flight row is already the durable marker, so charging
  from admission costs nothing extra.
- **Release at a time-based expiry or by reconciler sweep.** judgment: the rows already have
  owners that delete them; a sweep would second-guess the ledger and could free a charge while a
  volume still exists.
- **Charge NICs.** judgment: a NIC has no size and no provider storage.
