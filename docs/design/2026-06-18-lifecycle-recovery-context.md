# Lifecycle recovery context on get/list (#568)

Status: Draft
ADR: [ADR-0180](../adr/0180-lifecycle-recovery-context.md)

## Problem

An agent driving a long kernel workflow over MCP can lose its local context (a new
session, a compaction, a crash). To resume it must rebuild state from the read tools
alone. Today the lifecycle `get`/`list` envelopes confirm an object *exists* but omit the
parent ids, placement, profile/build summary, and timing an agent needs to pick the next
tool. The gaps (issue #568, `TOOL_ASSESSMENT.md` F4):

- **Allocations** (`allocations.get`/`wait`/`list`) surface only `project` and (when
  queued) queue position. The requested selector, sizing, granted `resource_id`, and lease
  timing — all already on the row — are dropped.
- **Systems** (`systems.get`/`list`) surface `project`, `resource_kind` (list only), and
  `active_debug_session_ids` (get only). The `allocation_id`, granted `resource_id`,
  profile summary, shape/sizing, timestamps, and the run currently using the System are
  dropped.
- **Runs** (`runs.get`) surface `project`, `target_kind`, `system_id`, `steps`,
  `required_cmdline`, `expected_boot_failure`, and `active_debug_session_ids`, but omit
  `investigation_id`, the build source/profile summary, and the build artifact refs.

`runs.list` does not exist; Run recovery is `runs.get`-only.

## Goal

Every lifecycle `get`/`list` response carries enough recovery context that an agent can
resume the workflow — pick the correct next tool and supply its ids — without direct
database access. Additively, with no envelope restructuring, no schema change, no
migration, and no new credential-leak surface.

## Non-goals

- No new tools, columns, or migrations. No `runs.list`.
- No raw profile JSON in any response (see Redaction boundary).
- No per-item extra query on a `list` path (no N+1).

## Redaction boundary (the load-bearing constraint)

`build_profile` and `provisioning_profile` are opaque JSON the agent submitted. They carry
**free-form reference strings that can embed inline credentials**: a `kernel_source_ref`
git remote (a token in the URL userinfo), `patch_ref`, `config` refs, rootfs refs,
`ssh_credential_ref`, `base_image_volume`, `domain_xml_params`, `crashkernel`. No read-path
response today echoes any stored profile content — a deliberate posture this change keeps.

The read paths resolve **no** secrets, so the runtime redaction registry (`security/`) is
empty at read time and cannot scrub anything as a backstop. Safety is therefore an
**allowlist**, not a denylist. A summary may surface only:

- **Enumerated discriminators** derived from the profile: `source` (`server`/`external`),
  `boot_method` (`direct-kernel`/`disk-image`), `arch`, and a derived provenance label
  (`git`/`warm-tree`/`external`).
- **Registry / operator-namespace identifiers**: `build_host` (a registered `build_hosts`
  name), `shape` (a `shapes` catalog label).
- **Structured object ids**: `allocation_id`, `resource_id`, `investigation_id`,
  `system_id`, `resource_kind`.
- **Object-store artifact keys**: `kernel_ref`, `debuginfo_ref` (stored keys, resolved to
  presigned URLs only at fetch time — never credentials).
- **Sizing integers, queue counters, and ISO-8601 timestamps.**

A summary **must never** echo a free-form profile reference string (any of the
credential-bearing fields above). The summary is read field-by-field from the stored
mapping with `.get()` — it does **not** re-parse the profile — so a slightly-off stored
document can never make a read tool raise.

## Field additions

All fields land in the existing `ToolResponse.data` (artifact keys in `refs`). Timestamps
are `.isoformat()` strings; absent/nullable sources serialize to `null` or are omitted as
noted. Existing keys are unchanged.

### Allocation envelope (`allocations.get`, `allocations.wait`, `allocations.list`)

Built from the already-fetched `Allocation` row — **no new query, no join**. Merged into
both the success and the `failed` envelope so a failed allocation still shows what was
requested. Added to `data`:

| key | source | notes |
|-----|--------|-------|
| `requested_kind` | `requested_kind.value` | null for a by-resource-id request |
| `requested_resource_id` | str | null for a by-kind request |
| `requested_pcie_specs` | list[str] | `[]` when none |
| `shape` | `shape` | null for full-custom |
| `requested_vcpus` / `requested_memory_gb` / `requested_disk_gb` | ints | custom capacity; null under a shape; **memory in GB** |
| `resource_id` | str | the granted resource; null while queued |
| `lease_expiry` | iso | null until granted |
| `active_started_at` / `active_ended_at` | iso | billing window; null where unset |
| `created_at` / `updated_at` | iso | always present |

`queue_position` / `queue_ahead` stay as-is (REQUESTED only). The granted resource's
concrete kind is not joined: for a by-kind request it equals `requested_kind`; a by-id
request carries `resource_id` to proceed.

### System envelope (`systems.get`, `systems.list`)

Profile summary and `shape`/sizing come from the `provisioning_profile` column already on
the System row — available on both paths with no extra query. `resource_id` is added to the
existing `list` join (`a.resource_id`); on `get` it (and `resource_kind`) come from one
`ALLOCATIONS.get(system.allocation_id)` + a `resources.kind` lookup. Added to `data`:

| key | get | list | source |
|-----|-----|------|--------|
| `allocation_id` | ✓ | ✓ | row |
| `resource_id` | ✓ | ✓ | allocation (get) / join (list) |
| `resource_kind` | ✓ | ✓ (already) | resources.kind |
| `arch` | ✓ | ✓ | `provisioning_profile.arch` |
| `boot_method` | ✓ | ✓ | `provisioning_profile.boot_method` |
| `vcpu` / `memory_mb` / `disk_gb` | ✓ | ✓ | `provisioning_profile` sizing (**memory in MB**, unlike the allocation's `requested_memory_gb`) |
| `shape` | ✓ | ✓ | row |
| `created_at` / `updated_at` | ✓ | ✓ | row |
| `active_run` | ✓ | — | `{id, state}` of the run using the System; **get-only (N+1)** |
| `active_debug_session_ids` | ✓ (already) | — | get-only (N+1) |

`active_run` is the run on the System whose state is not terminally `FAILED`/`CANCELED`
(i.e. `CREATED`/`RUNNING`/`SUCCEEDED`), selected deterministically by `created_at DESC, id`
and taking the first. Admission enforces single-occupancy only over `RUN_NON_TERMINAL`
(`{CREATED, RUNNING}`) — a `SUCCEEDED` run mid-install/boot is outside that set — so the
selection is defined as best-effort-most-recent rather than assuming exactly one. Omitted
(absent from `data`) when none.

### Run envelope (`runs.get`)

Added to non-failed and failed envelopes from the already-fetched `Run` row — no new query:

| place | key | source | notes |
|-------|-----|--------|-------|
| `data` | `investigation_id` | str | always present |
| `data` | `build_source` | `build_profile.source` (default `"server"`) | enumerated |
| `data` | `build_host` | `build_profile.build_host` | omitted when null (local) |
| `data` | `build_source_provenance` | derived | `git` / `warm-tree` / `external` |
| `refs` | `kernel` | `kernel_ref` | omitted when null |
| `refs` | `debuginfo` | `debuginfo_ref` | omitted when null |

"Current step/job ids" is satisfied by the existing `data.steps` map (current step states)
and `failing_job_id` (on failure). Jobs link to runs only through an unindexed `payload`
jsonb; a current-job scan is out of scope.

Surfacing artifact refs on the **failed** run (build succeeded, install/boot failed) needs
`ToolResponse.failure` to accept an optional `refs=` — an additive, optional parameter on
the shared envelope so both run paths carry artifact pointers in the idiomatic `refs` slot.
The artifact refs are added unconditionally — they are the owned Run's own object-store
keys, not job-derived detail, so they are **not** gated by the no-leak `suppressed_detail`
seam that suppresses `failing_job_id`/`detail` for a no-leak `failure_category`.

## Acceptance criteria

- Allocation `get`/`wait`/`list` responses carry the requested selector, shape/custom
  sizing, `resource_id`, lease timing, and timestamps; queued allocations keep queue detail.
- System `get`/`list` responses carry `allocation_id`, `resource_id`, `resource_kind`,
  profile summary (arch/boot_method/sizing), `shape`, and timestamps; `get` additionally
  carries `active_run`.
- Run `get` responses carry `investigation_id`, the build source/host/provenance summary,
  and `kernel`/`debuginfo` artifact refs, on both success and failure.
- No response echoes any free-form profile reference string. A test asserts a profile
  carrying a credential-bearing `kernel_source_ref` / `ssh_credential_ref` does not appear
  in the envelope.
- No `list` path issues a per-item query (assert one round trip for N rows).
- A resume test reconstructs the next workflow step purely from `get`/`list` output.

## Test plan

- Envelope unit tests per object: each new key present with the expected value across
  states (queued/granted/failed allocation; provisioning/ready/failed system; created/
  running/succeeded/failed run).
- Redaction test: stored profiles with credential-bearing free-form refs → those substrings
  are absent from the serialized envelope.
- N+1 test: `list` over N rows performs a fixed number of queries.
- `ToolResponse.failure(refs=...)` carries refs and still enforces category-iff-failure.
- Resume integration: from `allocations.get` → `systems.get` → `runs.get` output, assert the
  ids needed for `systems.provision` / `runs.bind` / `runs.install` are all present.
