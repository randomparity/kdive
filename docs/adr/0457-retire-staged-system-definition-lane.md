# ADR 0457 — Retire the staged System definition lane

- **Status:** Accepted
- **Date:** 2026-07-27
- **Issue:** #1580
- **Epic:** #1576
- **Partially supersedes:** [ADR-0025](0025-provisioning-plane-libvirt.md) §10 — the
  `systems.define` producer, the `defined → provisioning` admission branch it created, and the
  upload-window rationale both rest on. ADR-0025 §1–§9 are untouched.
- **Depends on:** [ADR-0441](0441-investigation-scoped-uploaded-rootfs.md) §3, which removed the
  System-scoped upload path this lane existed to hold open.

## Context

The staged System definition lane is two MCP tools:

- `systems.define(allocation_id, profile, …)` — validates a provisioning profile, inserts a System
  row at `defined`, flips its Allocation `granted → active`, and claims a `max_concurrent_systems`
  slot. It does no provider work and returns a System envelope, not a job handle.
- `systems.provision_defined(system_id, …)` — re-reads that stored profile and enqueues the
  provision job.

ADR-0025 §10 introduced the lane for one reason, stated there in one clause: `define` "is the one
tool that opens an upload window". An agent created a System at `defined`, uploaded a rootfs qcow2
against that System's object key, and then provisioned; the pause between the two tools was the
upload's lifetime.

**That upload window no longer exists.** ADR-0441 §3 moved uploaded-rootfs ownership to the
Investigation and removed the System-scoped path outright — `artifacts.create_system_upload`, the
`_SYSTEM_UPLOAD` spec, `_commit_uploaded_rootfs`, `_system_accepts_upload`, and the
`rootfs_upload_window_allowed` policy hook. In the current tree `create_system_upload`,
`_system_accepts_upload`, and `rootfs_upload_window_allowed` have **zero occurrences** under
`src/`, and `UploadOwnerKind` (`src/kdive/artifacts/upload_manifest.py:24`) is
`Literal["runs", "investigations"]` — there is no `systems` owner kind for an upload window to be
minted against. A rootfs is uploaded and finalized against the Investigation *before* any System
row is written, and `systems.provision` resolves it by checksum at provision time (ADR-0441 §4).

The lane's agent-facing surface was not updated with the code. `systems.define`'s `profile` field
still reads "an 'upload' rootfs opens a pre-provision rootfs-upload window"
(`src/kdive/mcp/tools/lifecycle/systems/registrar.py:180`), its docstring still says "opening a
pre-provision rootfs-upload window; follow with `systems.provision_defined` once the upload is
done" (`:197`), and `systems.provision_defined` still describes itself as admitting a System
"after its upload window is complete" (`:305`) — text that has already been baked into the
generated CLI help (`src/kdive/cli/commands/_generated_verbs.py:4309`). Agents read those wrapper
docstrings and `Field` descriptions as the contract. The lane therefore advertises a capability
that no longer exists: a phantom feature, which this repository's contributor guidance forbids.

With the upload gone, nothing occupies the pause. `define` validates and stores a profile;
`provision_defined` re-parses the same stored profile and enqueues. `systems.provision`'s create
branch does both in one transaction (ADR-0025 §1), against the same admission checks, and is the
only lane that can bind a rootfs upload today.

The lane is also already absent from the paths the project treats as authoritative:

- **No live coverage.** No test under `tests/live_vm/` or `tests/live_stack/` references
  `systems.define`, `systems.provision_defined`, or `define_system`. The lane has never been
  proven end-to-end against a real host. Twenty test files under `tests/` reference the tool names
  (twenty-eight including `SystemState.DEFINED` directly); all are unit or integration tests.
- **The onboarding chain already dead-ends on it.** `docs/guide/core-path.md:25` ends its core-path
  table at `systems.define`, and the `start_investigation` prompt
  (`src/kdive/mcp/prompts/registrar.py:127-131`) ends its step list at `systems.define` with
  `provides=("system",)`. Neither goes on to provision. Because `RUN_HOSTABLE` is
  `frozenset({SystemState.READY})` (`src/kdive/services/runs/states.py:7`), a `defined` System
  cannot host a Run, so both surfaces guide an agent to a state from which the next step is
  unreachable. ADR-0133 already dropped `systems.define` from the `profile_examples` discovery
  chain in favor of one-shot `systems.provision` for exactly this reason.

Epic #1576 targets at most 123 registered tools after its unconditional children, and 121 if this
lane is retired.

### The case for keeping the lane

The strongest argument for keeping it does not depend on the upload window, and survives its
removal intact. `systems.define` is the **only** way to reach a durable, recoverable checkpoint
between "I have a granted Allocation and a profile" and "a provider is doing work":

- It is the only producer of a System row that exists without any provider work having started.
- It performs the full admission set — profile validation, arch validation and accel
  commitment before `_new_system_allowed` (ADR-0339's Decision), the `granted → active` allocation
  flip, and the `max_concurrent_systems` slot claim — and makes all of it **durable**. An agent
  that defines has provably reserved capacity and provably resolved its host bindings; it can
  crash, reconnect, and find that reservation still there.
- The failure modes differ. A rejected `define` fails synchronously with a `configuration_error`
  and nothing was started. A rejected one-shot `provision` may fail the same way, but a profile
  that passes admission and fails later fails in a worker, as a job, with a torn-down System.
  ADR-0339's Decision records that `provision_defined` deliberately does not re-validate arch
  precisely because the arch was already committed at `define` — the lane's checkpoint semantics
  are load-bearing for that decision.

There is no direct replacement for that checkpoint. Retiring the lane means an agent that wants to
validate a profile and reserve capacity without provisioning has no tool that does it. This is a
real capability loss, not a redundancy removal, and the decision below accepts it on the grounds
that no shipped flow, prompt, doc, or live test uses it.

## Decision

### 1. Retire the lane

We will remove `systems.define` and `systems.provision_defined` from the MCP registry. There will
be no compatibility alias, no deprecation period, and no dual name. `systems.provision`'s one-shot
create branch is the sole lane for creating and provisioning a System.

The capability loss recorded above is accepted. It is accepted because the checkpoint it protects
is not reachable by any current caller: no live test exercises it, the two onboarding surfaces that
mention it dead-end before provisioning, ADR-0133 already routed profile discovery away from it,
and the pre-provision upload it was built for has moved to the Investigation. Retaining two tools,
a durable state, and a state-machine branch to serve a checkpoint nothing uses is the cost this ADR
declines to keep paying.

If profile-validation-without-provisioning is later wanted as a capability in its own right, it
should be designed as one — a read-only validation tool with no row, no allocation flip, and no
quota claim — rather than recovered by keeping a state-materializing mutation tool alive for it.

### 2. `SystemState.DEFINED` is retired with the tools

`defined` has exactly one producer (`systems.define`) and one consumer that advances it
(`systems.provision_defined`). Removing both makes the state unreachable, so it is removed rather
than left as a value no code path can write — the same replace-don't-deprecate rule the tools get.
This removes the `defined → provisioning` and `defined → torn_down` edges from the transition map.

### 3. Historical `defined` rows require a data migration

This is the sharpest cost of retirement and it must not be discovered during implementation.

`systems.provision` today **rejects** an existing `defined` System: admission returns an
`AdmissionFailure` with reason `SYSTEM_ALREADY_DEFINED` and recovery
`PROVISION_DEFINED_SYSTEM` (`src/kdive/services/systems/admission.py:701`) — it tells the caller to
use the very tool being retired. A `defined` row that outlives the removal is therefore stranded:
it holds a `max_concurrent_systems` slot and an `active` Allocation, `systems.provision` refuses to
advance it, and the tool its error message names no longer exists.

Retirement therefore **requires** a data migration that resolves every pre-existing `defined` row,
shipped in the same change as the tool removal. A migration cannot enqueue a provision job, so the
row must be resolved to a terminal state that releases the quota slot and lets the allocation
repair reclaim its Allocation. The mechanics — which terminal state, whether the Allocation is
reverted or left for `reap_orphaned_active_allocations`, and what audit rows the transition
writes — are implementation work with their own consequences, delegated to the implementation issue.
What this ADR fixes is that the migration is **mandatory and not optional**: removing the enum
value without it leaves rows carrying a state the code cannot read.

ADR-0441's "pre-1.0, fresh DBs, no backfill" reasoning does **not** license skipping it here. That
argument applied to *artifacts* nothing would strand; here the stranded object holds live capacity
and blocks its Allocation.

### 4. The three non-tool `DEFINED` consumers each need a re-read, not a blind delete

Each of the three is a state classification that some other decision's safety rests on. The
constants themselves stay; `DEFINED` leaves each set, which is correct only because the state
ceases to exist. Each site must be re-read to confirm the surrounding invariant still holds without
it.

- `ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES` (`src/kdive/domain/capacity/state.py:100`) — ADR-0441 §6
  condition (b). Its guard `test_reclaim_classification_is_exhaustive` will redden if the set and
  the enum drift, which is the intended behavior; the remaining members
  (`provisioning`/`reprovisioning`/`restoring`) still cover the pre-overlay and re-materialize
  windows.
- `_LIVE_SYSTEM_STATES` (`src/kdive/reconciler/repairs/allocations.py:50`) — the set of states in
  which a System legitimately occupies its Allocation. Dropping `DEFINED` here is what lets the
  orphaned-allocation repair reclaim an allocation whose only System was `defined`; it must be
  sequenced *after* the migration in §3, not before, or the repair races rows the migration is
  about to resolve. (The identically-named constant in
  `src/kdive/reconciler/repairs/console_rotation.py:35` does not contain `DEFINED` and is
  unaffected.)
- `_NON_TERMINAL_SYSTEM` (`src/kdive/services/systems/admission.py:71`) — the quota-slot set, whose
  comment names `systems.define` as the producer. It is the documented complement of
  `_LIVE_SYSTEM_STATES` above; the two must be changed together.

### 5. Implementation is a separate issue

This ADR records a decision and changes no behavior. The retirement ships under its own issue,
whose scope is:

1. Remove the `systems.define` and `systems.provision_defined` tool registrations, handlers, and
   admission branches.
2. Remove `SystemState.DEFINED` and its transitions from the state machine (§2).
3. The three non-tool `DEFINED` consumers in §4.
4. The mandatory historical-row migration in §3.
5. `src/kdive/mcp/exposure.py`, `src/kdive/mcp/schema/tool_index.py`, and the `systems.define`
   entry in `src/kdive/mcp/middleware/binding_errors.py:115`.
6. The generated CLI verbs (`src/kdive/cli/commands/_generated_verbs.py`), regenerated, not
   hand-edited.
7. The `start_investigation` prompt chain (`src/kdive/mcp/prompts/registrar.py:127-131`), which
   must be re-pointed at `systems.provision` so it terminates in a `READY` System a Run can be
   hosted on rather than dead-ending.
8. The served documents and guides: `docs/guide/core-path.md`, `agent-index.md`,
   `toolsets-systems.md`, `safety-and-rbac.md`, and `response-envelope.md` under
   `src/kdive/mcp/resources/_content/`.
9. The twenty test files naming the tools and the eight further files naming `SystemState.DEFINED`.
10. Retained gateway search vocabulary so `systems.define` and `systems.provision_defined` remain
    discoverable terms resolving to `systems.provision` (ADR-0456 §3), rather than aliases.

This ADR advanced to Accepted when that change merged (#1600).

## Consequences

- The registered tool count drops by two, taking epic #1576's target from at most 123 to at most
  121.
- The `systems.*` surface loses its phantom upload-window text. An agent reading
  `systems.provision`'s schema gets the only lane that exists, and the ADR-0441 investigation-scoped
  upload is the only rootfs-upload story on the surface.
- The core-path guide and the `start_investigation` prompt gain an ending that actually reaches a
  hostable System, closing a dead end that predates this decision.
- **Against ADR-0149.** Its Context enumerates the `existing is None` (mint), `DEFINED` (route to
  `provision_defined`), and `PROVISIONING` admission cases. The `DEFINED` arm and the
  `PROVISION_DEFINED_SYSTEM` recovery action it returns are removed; the mint and `PROVISIONING`
  arms are unchanged. `AdmissionFailureReason.SYSTEM_ALREADY_DEFINED` becomes unreachable and goes
  with them.
- **Against ADR-0193.** Its Context lists `systems.define` among the UUID-minting idempotent
  creators and `systems.provision_defined` among the dedup-key enqueuers. Both entries are removed;
  the uniform contract itself is unchanged, and `systems.provision` already satisfies both roles.
### §3's delegated mechanics, as resolved by the implementation (#1600)

§3 left three choices to the implementation. Migration `0080_retire_defined_system_state.sql`
resolves them as follows.

- **Terminal state: `torn_down`.** `defined -> torn_down` was already a legal edge, described in
  the state machine as "an abandoned create-without-provision System torn down without first
  advancing to provisioning", and it is the honest description of what happened: no provider work
  ever started, so there is no host domain, no overlay, and nothing to reap — only a reservation to
  give back. `failed` was rejected because it asserts an error that never occurred and would enter
  failure reporting. `torn_down` sits outside `_NON_TERMINAL_SYSTEM`, so it genuinely releases the
  `max_concurrent_systems` slot rather than renaming the strand.
- **The Allocation is left to `reap_orphaned_active_allocations`.** The row's Allocation stays
  `active`; the migration does not touch it. That repair is precisely the "an `active` allocation
  whose only System is terminal or absent" case (ADR-0109), it re-checks the predicate under the
  `PROJECT -> ALLOCATION` lock, and it writes the release audit trail and ledger credit a bare SQL
  `UPDATE` could not. Reverting the allocation in SQL would duplicate that logic outside the lock.
  The observable cost is latency: `DEFAULT_ORPHANED_ACTIVE_GRACE` is two minutes measured from
  `allocations.updated_at`, so the slot returns one reconciler pass later, not instantly.
- **Audit: one `audit_log` row per resolved System.** Each row records
  `transition = 'defined->torn_down'` under principal `system:migration` and
  `tool = 'migration:0080_retire_defined_system_state'`, following the system-principal convention
  `audit.record_system` uses for reconciler-initiated transitions. `args_digest` is the digest of
  the empty argument map, since the migration took no arguments. Without these rows an object's
  trail would show `->defined` and then nothing, with the row silently terminal across a deploy.
  No Allocation audit row is written here — the reaper writes its own when it releases.

The migration also drops `'defined'` from the `systems_state_check` CHECK constraint after
resolving the rows, so the database rejects a value the code can no longer read. This mirrors
migration 0044's removal of `failed` from `component_uploads_state_check`. Sequencing within the
one migration transaction is data-resolution first, constraint tighten second; the constraint
would otherwise fail validation against the very rows being resolved.

- **Against ADR-0326.** Its contributor-tier lifecycle classified
  `systems.define`/`provision`/`provision_defined`/`reprovision` together. Two of the four
  disappear. Its Context list also still named `artifacts.create_system_upload`, which ADR-0441 §3
  already removed; #1600 corrected that stale reference in the same pass rather than leave the ADR
  citing two dead tools.
- **Against ADR-0339.** This is the most affected. `_insert_defined_system` and its
  arch-validation-before-`_new_system_allowed` ordering go away, and the deliberate
  non-re-validation of arch on the `provision_defined` lane becomes moot along with the lane. Its
  Consequences residual — "a host whose `guest_arches` changes between `define` and
  `provision_defined`" — ceases to exist, because there is no longer a window between the two.
  ADR-0339's arch validation on the one-shot `provision` path is unaffected and remains in force.
- The state machine loses a state and two edges, which simplifies every exhaustive-classification
  guard that enumerates `SystemState`.
- **A migration is required and is not optional** (§3). This is the one place retirement costs more
  than deleting code.
- The reservation/validation checkpoint is genuinely lost, with no replacement (§1). An agent that
  wants to know whether a profile is admissible must attempt a provision.
- Not an AI surface (no LLM, prompt-construction, retrieval, or classifier decision), so no eval
  plan is required. This ADR ships no code, no schema, no migration, and no configuration.

## Considered & rejected

- **Keep the lane as it is.** Rejected: its sole recorded justification was deleted by ADR-0441 §3,
  its docstrings advertise that deleted capability to agents, no live test covers it, and both
  onboarding surfaces that reach it dead-end. Keeping it means keeping two tools, a durable state,
  two transition edges, and three state classifications for a checkpoint no caller uses.
- **Keep `systems.define`, fold `provision_defined` into `systems.provision`.** This saves one tool
  instead of two and preserves the reservation checkpoint, which is the genuinely valuable half. It
  is the strongest alternative. Rejected because it does not resolve the ambiguity it leaves behind:
  `systems.provision` would need to keep a `defined`-admitting branch, so the state, its edges, and
  all three classifications survive, and the agent surface still presents two ways to start a
  System with no rule for choosing between them. Most of the cost is in the state, not the second
  tool.
- **Keep `SystemState.DEFINED` as a state, remove only the two tools.** Rejected: with no producer
  the state is unreachable, and an unreachable enum value is dead code that every exhaustive
  classification must keep handling. It also would not avoid the §3 migration, since historical rows
  would still hold a state no tool can advance — it only hides the problem.
- **Deprecate with aliases and a migration period.** Rejected: KDIVE is pre-release and follows
  replace-don't-deprecate, and epic #1576 explicitly excludes compatibility aliases. Discoverability
  is served by retained gateway search vocabulary (ADR-0456 §3), not by hidden wrappers.
- **Re-purpose `systems.define` as a dry-run profile validator.** It preserves the validation half
  of the checkpoint at no new tool cost. Rejected for this ADR: a validator that mints no row, flips
  no allocation, and claims no quota slot is a different tool with a different contract, and
  smuggling it in under an existing mutating name would leave the annotation, RBAC tier, and
  idempotency contract mismatched. It is a reasonable future proposal on its own merits, and §1
  records it as such.
- **Retire the lane but skip the migration on the pre-1.0 fresh-DB argument.** Rejected: ADR-0441
  used that argument for artifacts nothing would strand. A stranded `defined` row holds a
  `max_concurrent_systems` slot and an `active` Allocation and cannot be advanced or, once the enum
  value is gone, read. The blast radius is live capacity, not storage.
