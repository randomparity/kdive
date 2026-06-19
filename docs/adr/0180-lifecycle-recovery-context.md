# ADR-0180: Lifecycle recovery context on get/list

Status: Accepted

## Context

An agent driving a long kernel build → boot → debug workflow over MCP can lose its local
context (new session, compaction, crash) and must resume from the read tools alone. The
lifecycle `get`/`list` envelopes confirm an object exists but omit the parent ids,
placement, profile/build summary, and timing needed to choose the next tool (issue #568,
`TOOL_ASSESSMENT.md` finding F4). `allocations.get`/`wait`/`list` surface only `project`
and queue position; `systems.get`/`list` surface `project`, `resource_kind` (list only),
and `active_debug_session_ids` (get only); `runs.get` omits `investigation_id`, the build
summary, and the artifact refs. The read surface is otherwise symmetrical and additive
extensions are precedented (ADR-0176 added `active_debug_session_ids` the same way).

The data is almost entirely already on the fetched rows. The one hazard is that
`build_profile` / `provisioning_profile` are opaque agent-submitted JSON carrying free-form
reference strings that can embed inline credentials (a `kernel_source_ref` git remote,
`patch_ref`, rootfs refs, `ssh_credential_ref`, `base_image_volume`, `domain_xml_params`,
`crashkernel`). No read-path response echoes stored profile content today.

## Decision

Add recovery context to the existing `ToolResponse.data` (and, for artifact pointers,
`refs`) on every lifecycle `get`/`list` path. Additive only — no envelope restructuring, no
schema change (per ADR-0113 every tool's `outputSchema` is flat `{"type":"object"}`), no
migration, no new tool.

1. **Allocation** (`allocations.get`/`wait`/`list`) — from the already-fetched row, no new
   query: requested selector (`requested_kind`, `requested_resource_id`,
   `requested_pcie_specs`), `shape`, custom sizing (`requested_vcpus`/`_memory_gb`/
   `_disk_gb`), granted `resource_id`, `lease_expiry`, `active_started_at`/`_ended_at`,
   `created_at`/`updated_at`. Merged into both the success and the `failed` envelope. Queue
   counters are unchanged.

2. **System** (`systems.get`/`list`) — `allocation_id` (row), `resource_id`
   (existing list join via `a.resource_id`; on `get` one `ALLOCATIONS.get` + `resources.kind`
   lookup), `resource_kind`, a redaction-safe profile summary read from the
   `provisioning_profile` column (`arch`, `boot_method`, `vcpu`/`memory_mb`/`disk_gb`),
   `shape`, and `created_at`/`updated_at` — all on both paths with no extra per-row query.
   `get` additionally carries `active_run` (`{id, state}` of the non-terminal run holding the
   System); `active_run` and `active_debug_session_ids` stay `get`-only to keep `list`
   single-query.

3. **Run** (`runs.get`) — `investigation_id` (row), a build summary (`build_source`,
   `build_host`, derived `build_source_provenance` of `git`/`warm-tree`/`external`), and
   `kernel`/`debuginfo` artifact refs in `refs`. Surfaced on both the success and `failed`
   envelopes, so `ToolResponse.failure` gains an additive optional `refs=` parameter.
   "Current step/job ids" is covered by the existing `data.steps` map and `failing_job_id`.

**Redaction is an allowlist.** Read paths resolve no secrets, so the runtime redactor is
empty and cannot scrub as a backstop. A summary surfaces only enumerated discriminators
(`source`, `boot_method`, `arch`, provenance), registry/operator identifiers (`build_host`,
`shape`), structured object ids, object-store artifact keys, sizing integers, queue
counters, and ISO timestamps. It **never** echoes a free-form profile reference string, and
it reads fields with `.get()` rather than re-parsing the stored profile (so a slightly-off
stored document cannot make a read tool raise). A test asserts credential-bearing
`kernel_source_ref`/`ssh_credential_ref` substrings are absent from the envelopes.

## Consequences

- A recovering agent reconstructs the workflow — parent ids, placement, lease deadline,
  build provenance, artifact pointers, and the next tool's inputs — from `get`/`list`
  output without database access.
- `list` paths stay single-query; the per-item recovery fields (`active_run`,
  `active_debug_session_ids`) remain `get`-only.
- The profile summary is intentionally lossy: the exact source remote/ref/patch/rootfs are
  not recoverable from a read response. The requesting agent supplied them; widening the
  summary later is additive if a redaction-safe form is designed.
- `ToolResponse.failure(refs=...)` lets any failure envelope carry artifact references; the
  category-iff-failure invariant is unchanged.

## Considered & rejected

- **Echo the raw `build_profile` / `provisioning_profile` JSON.** Directly leaks the
  credential-bearing free-form refs the allowlist exists to exclude; no read path does this
  today.
- **Re-parse the stored profile (`ProvisioningProfile.parse`) to extract the summary.** A
  stored document that drifts from the current schema would make a read tool raise; reading
  with `.get()` keeps reads total over any persisted shape.
- **Join the granted resource's concrete kind onto the allocation envelope.** For a by-kind
  request it equals `requested_kind`; a by-id request carries `resource_id`. The join is
  unnecessary churn on three read paths for no recovery gain.
- **Surface `active_run` / `active_debug_session_ids` on `systems.list`.** A per-item query
  — an N+1 on a collection path. The `get` path covers the recovery pivot (ADR-0176).
- **Add a current-job id to `runs.get`.** Jobs link to runs only through an unindexed
  `payload` jsonb; the `steps` map and `failing_job_id` already name the in-flight work.
</content>
