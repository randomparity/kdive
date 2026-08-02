# Spec: Investigation-scoped kernel build reuse (#1519)

- Issue: [#1519](https://github.com/randomparity/kdive/issues/1519)
- ADR: [ADR-0531](../../adr/0531-investigation-scoped-kernel-builds.md)
- Status: Design proposed

## Frozen scope

The outcome is investigation-lifetime ownership and content-addressed resolution for reusable
externally built and uploaded kernel artifacts. One upload must install across multiple Systems in one
Investigation; ownership, garbage collection, concurrency, schema, and agent-facing MCP contracts
must be defined and tested. The user selected an optional investigation build reference on
`runs.create`. Restoring the server-side build lane removed by ADR-0316, remote-provider expansion,
and unrelated artifact classes are excluded. There are no remaining design-changing ambiguities.

Scope identity is `https://github.com/randomparity/kdive/issues/1519 +
work-1519-20260801-d`. Provenance is issue #1519, linked issue #1502, merged PR #1517, and the
user's explicit `runs.create` and external-build-only decisions in the active interactive run.

## Approaches

The selected approach adds an immutable-content investigation-build catalog and exposes its
content-derived `build_ref`. This preserves a single validated build set and makes ownership and
reclaim explicit.

A source-Run reference is smaller but makes reuse depend on a lifecycle object rather than the
content set. Individual object checksums are superficially content-addressed but permit invalid
kernel/initrd/debuginfo combinations. A separate reuse tool adds a state transition and a race that
create-time resolution avoids.

## Data model and identity

Migration 0095 adds `investigation_builds` with:

- `investigation_id` referencing `investigations(id)`;
- `content_digest`, a lowercase hexadecimal SHA-256 string, and opaque UUID `generation`;
- `build_ref`, the unique `<content_digest>.<generation>` public handle;
- `target_kind`, `build_profile`, and the complete serialized `BuildStepResult`;
- state (`active` or `reclaiming`);
- `created_at` and absolute `expires_at` timestamps, plus a primary key on
  `(investigation_id, build_ref)`.

It also adds nullable `runs.build_ref`. The link is an audit and reclaim reference, not a foreign
key to a composite owner whose deletion timing differs from Run retention.

The canonical build document is versioned and includes the target kind, normalized build profile,
kernel checksum, optional initrd and debuginfo checksums, build id, cmdline, and normalized
provenance. A chunked artifact uses its ordered validated chunk checksum-and-size vector plus total
size, excluding its advisory whole-object hash. Canonical JSON uses sorted keys and compact
separators; SHA-256 over its UTF-8 bytes is the `content_digest`. Object keys and etags are excluded because they describe storage placement
rather than content. Finalization already requires the object store's SHA-256 for every artifact.
The generation suffix prevents an expired or partially reclaimed physical set from being mistaken
for a fresh publication of the same content.

The record's content is immutable and stores the exact selected artifact keys and versions. Under
the Investigation lock, completion queries for an active, unexpired matching digest. A match must
have an equal canonical document and becomes the winner. With no match, completion mints a UUID
generation and inserts a new active record. An expired or reclaiming matching digest never wins.
Only the winner's artifact set gets new rows with `owner_kind='investigations'` and the
Investigation id. The existing owner-triple uniqueness constraint makes its registration
replay-safe.

The validated identity is immutable before publication. A single-upload validator binds every
ranged content and build-id read to the VersionId returned by its first HEAD. For chunked input,
each `UploadPartCopy` names the verified source VersionId, multipart completion captures the final
VersionId, and final-object validation names that version. Every later build consumer receives the
persisted `(key, VersionId)` pair; only legacy build steps without `artifact_versions` use a
key-only fallback.

An identical-content loser completes its source Run with the winner's build result and references,
not its own uploaded keys. Its uploaded versions remain unregistered, are deleted exactly after the
database commit, and fall to the existing Run-prefix orphan sweep if exact deletion fails. Loser
cleanup never names the winner's keys. Catalog selection and registration share the Investigation
lock, so garbage collection cannot observe or delete a half-published winner.

## Completion flow

`runs.complete_build` retains its upload-window and validation behavior. Its final transaction
acquires the Investigation lock before the Run lock (the repository's global order), then rechecks
the Run state and upload-window identity under both before it:

1. derives the canonical build document and `build_ref` from validated HEAD data and build result;
2. selects an active matching generation or publishes a new investigation-build record;
3. registers artifact rows against the Investigation only when this candidate won;
4. records the source Run's succeeded build step and `runs.build_ref`;
5. marks the source Run succeeded and records the existing audit event.

All five database effects commit together. Object uploads precede the transaction as today. After
commit, a convergence loser deletes only its own exact uploaded versions; the existing orphan sweep
remains the retry owner if that cleanup or the transaction fails. The successful tool response
includes `data.build_ref`, `data.expires_at`, and `data.server_time`; `runs.get`/`runs.list` expose
the reference and deadline.

No path may acquire Investigation while already holding Run. The existing bind, cancel, and upload
reaper paths remain Run-only and must not call the new publication/reclaim helpers. Completion,
install admission, and generation reclaim are the only dual-scope paths and all use Investigation
→ Run. A barrier-controlled complete-build-versus-install test proves neither waits cyclically and
that install observes either the pre-completion rejection or the completed generation.

## Reuse flow

`runs.create` adds optional `build_ref`. It remains compatible with bound and unbound creation.
After parsing the normal build profile and while holding the Investigation lock, create resolves:

```sql
SELECT ... FROM investigation_builds
WHERE investigation_id = $requested_investigation AND build_ref = $requested_ref
```

The reference must contain a 64-character lowercase hexadecimal digest, a dot, and a canonical
lowercase UUID generation. Missing and cross-Investigation values
share `configuration_error` with `reason: build_ref_not_found`, so another tenant's catalog is not
an oracle. The record's target kind and normalized build profile must equal the new Run's resolved
values; mismatch returns `reason: build_ref_incompatible` and safe expected/actual target-kind and
architecture fields.

The existing `KDIVE_BUILD_ARTIFACT_RETENTION_DAYS` applies in days per build. Completion stamps
`expires_at` from the Postgres clock as `server_time + retention`; it never refreshes on reuse.
Create at or after that instant returns `reason: build_ref_expired` with `expires_at`, a fresh
`server_time` from the same database clock, and `runs.create` as the literal recovery tool. The
caller repeats create with the same Investigation, System or target kind, and profile but omits
`build_ref`. That successful create returns a Run id and the normal
`artifacts.create_run_upload` → upload → `runs.complete_build` sequence. Uploading and completing
identical content then publishes a new generation with a new deadline; the expired generation and
any live Run references to it remain isolated. This preserves ADR-0234's storage backstop while
giving an agent a terminating recovery path and the full limit contract before it plans reuse.

Creation writes the Run with state `succeeded`, `kernel_ref`, `debuginfo_ref`, and `build_ref`, plus
a succeeded `build` run step copied from the immutable record. It does not create an upload
manifest. Admission, System holding, Investigation state transition, audit, and idempotency remain
in the same transaction. Idempotent replay returns the same `build_ref` and next action.

Without `build_ref`, behavior is unchanged: the Run starts `created` and the response points to
the external-build contract and upload tools. With it, the wrapper docstring states same-
Investigation scope, compatibility requirements, failure reasons, and that the next action is
`runs.install`. The optional field description names the digest-plus-UUID format and source
(`data.build_ref` from `runs.complete_build` or `runs.get`).

## Garbage collection and concurrency

Investigation ownership replaces Run ownership only for newly finalized builds. The existing
close-plus-grace and expired-build sweeps are extended to enumerate `investigation_builds` and their
artifact rows. The latter uses each record's stored absolute deadline. Legacy
`owner_kind='runs'` build artifacts keep their current close and age-based TTL paths.

Create and reclaim take the Investigation advisory lock. Reclaim rechecks for non-terminal Runs
whose `build_ref` selects the generation and for queued or running install jobs on any referencing
Run. Either condition defers deletion. Otherwise reclaim marks the generation `reclaiming` before
deleting its exact object versions. A partial object-store failure keeps that state for retry.
Failed deletes receive a database-clock retry delay. The global pass first keyset-scans at most 200
catalog primary-key rows from a durable cursor, then evaluates expiry/close eligibility and pin
joins only for that bounded set. Rank ordering within the set takes one generation per
Investigation before a second from any tenant; the cursor advances past scanned ineligible or
pinned rows so one tenant cannot make later catalog rows unreachable. Partial indexes support the
live Run build-reference and queued/running install-job pin lookups.
After deletion it removes only that generation's artifact rows and record, rechecking the state
under the lock. A fresh publication of identical content uses a new generation and cannot be
deleted or selected through the old record.
The final catalog-row delete writes a durable `(investigation_id, build_ref, expires_at)`
tombstone. Selection consults it only after an exact same-Investigation catalog miss, preserving
expired recovery after GC without turning unknown or cross-Investigation handles into an oracle.

`runs.install` acquires the Investigation lock before the existing Run lock. In that transaction it
checks the generation deadline and enqueues or recycles the install job. A first install or restage
at or after expiry returns `build_ref_expired` with `expires_at`, `server_time`, and `runs.create`
as the first recovery action. The caller recreates without the expired reference and follows the
upload flow. An unchanged already-succeeded install is an idempotent no-op and remains callable
after expiry because it performs no artifact read. Once admission enqueues work, the queued job
closes the gap until execution. Each executing install attempt records its own durable
generation-use row before resolving artifact references and removes only that row after provider
consumption. GC waits for every row independently of the shared job state, so cancellation or
lease overlap cannot expose an older still-running attempt. A cleanup fault leaks a safe pin rather
than permitting deletion. Job-heartbeat expiry never removes a use row: ADR-0018 permits the old
handler to continue after its claim is reclaimed. A genuinely dead attempt is recovered only by
an explicit operator/reconciler action naming the exact worker and recording independently obtained
worker-death evidence in the durable recovery ledger.

Caller-controlled build and install cmdline extras are trimmed and limited to 4096 printable
characters at MCP ingress and at the service/worker boundaries before hashing, persistence, or
provider use.

The Investigation lock makes create-versus-reclaim deterministic. Concurrent source completions
of identical content converge through the active-digest query under that lock and artifact
owner-triple uniqueness.
Different builds within one Investigation serialize only for their short database commit, not
during upload or validation.

## Threat model

### Boundaries and actors

The existing authenticated tenant controls `build_ref`, build profile, Investigation id, uploaded
bytes, and build metadata. The server controls tenancy lookup and canonicalization; the worker and
object store control validated checksums. The change widens `runs.create` so it can select existing
sensitive artifacts and widens artifact lifetime from one Run to its Investigation.

### Controls

- Existing project membership and contributor RBAC gate create and complete-build.
- Resolution includes the requested Investigation id in the SQL predicate; missing and cross-
  tenant references return the same error.
- The server derives `build_ref`; callers cannot register an arbitrary content set.
- Exact build-profile and target-kind equality prevents installing a validated build under an
  incompatible Run contract.
- The Investigation lock serializes create, completion, close, and reclaim; database uniqueness
  guards generation identity and artifact-row replay.
- Responses expose references and scalar reasons, never artifact bytes, object-store credentials,
  or another Investigation's existence.

### Out of scope

This design does not permit cross-Investigation sharing, add remote-provider behavior, deduplicate
separate uploads before validation, or change object-store encryption and transport. Authorized
members of one Investigation already share its Runs and Systems; reuse does not create a new trust
class within that boundary.

## Acceptance criteria

1. Completing one external build returns a stable `build_ref`, registers its artifact rows under
   the Investigation, and records the reference on the source Run.
2. Two bound Runs for distinct Systems in that Investigation can select the reference at create,
   start with a succeeded build step, and install without another upload manifest or object copy.
3. Unbound create can select the same build when target kind and build profile match, then bind and
   install normally.
4. Malformed, missing, cross-Investigation, target-kind-mismatched, and build-profile-mismatched
   references fail without a Run, System hold, audit transition, or tenancy disclosure.
5. Concurrent identical completions converge; create racing close or reclaim cannot produce a
   dangling build reference; a validated identical completion after expiry publishes a distinct
   generation that survives partial cleanup of the old one.
6. Close-plus-grace and absolute-deadline collection reclaim new investigation-owned build objects
   only after no live Run or queued/running install job references them; expiry is reported with
   database-clock timestamps and a recreate/re-upload recovery action, and legacy run-owned build
   collection remains green.
7. `runs.create`, `runs.complete_build`, `runs.get`, generated CLI/docs, schema migration tests,
   service tests, and adversarial concurrency tests describe and prove the contract. Every
   `suggested_next_actions` entry is directly callable with identifiers in that response; expired
   reuse points first to `runs.create`, never an upload tool that needs an absent Run id.

## Verification plan

Start with focused failing service tests for `runs.create` reuse and complete-build ownership.
Cover bound and unbound success, every rejection in criterion 4, idempotent replay, and a
barrier-controlled create/reclaim, install/reclaim, and complete-build/install races. A queued
install delayed past expiry
pins all objects through provider consumption; a first install after expiry fails before enqueue;
an unchanged installed variant remains an idempotent no-op. Fault injection pauses after each
old-generation artifact deletion and proves a fresh same-content generation stays usable. Add
migration shape and garbage-collection tests, then MCP
wrapper/schema tests proving the agent-visible contract and suggested next actions. Mutate the
Investigation ownership predicate and profile compatibility check to confirm those tests fail.

Run focused modules after each slice, then `just lint`, `just type`, schema and documentation
guards, `just test`, and `just ci`. Live VM testing is not required because provider installation
receives the same object refs and this change adds no provider-specific behavior.

## Resume facts

- Branch: `feat/investigation-kernel-artifacts-1519`
- Base branch: `main`
- Required aggregate guardrail: `just ci`
- Individually CI-gated recipes include lint, type, shell/workflow/docs/ADR/schema guards, tests,
  and image/chart smoke checks as defined in `.github/workflows/ci.yml`.
- ADR index coupling: not coupled; `docs/adr/README.md` declares the directory listing authoritative.
