# Provider-host external-boot authority and journal design

## Scope

Issue #2126 implements the provider-host slice of accepted
[ADR-0584](../../adr/0584-provider-host-authority-fences-external-boot-mutations.md).
It adds the provider-neutral `external-boot-authority-v1` value contract, a narrow authority
service, a crash-recoverable append-only journal, migration 0123's independently anchored journal
head, and the shared bounded `AuthorityMutationAdapter` contract. Migration 0122 remains the
owner of authority allocation, acknowledgement, and core-result fencing. PostgreSQL remains
lifecycle truth; the journal records provider mutation admission and observation only.

Scheduling, reconciliation, lifecycle transitions, deployment provisioning and ACL rollout, and
non-external provider behavior are excluded. This design supports Python 3.14 and the repository's
x86_64 and ppc64le targets without adding a dependency.

## Architecture

`kdive.providers.external_boot_authority` is a provider-neutral package with four boundaries:

- `protocol.py` defines closed, bounded request, acknowledgement, observation, conflict, and
  recovery values. The wire schema is `external-boot-authority-v1`; UUIDs, positive 64-bit
  generations and sequences, lowercase SHA-256 digests, UTF-8 identifiers, observations, and
  recovery-object lists have explicit byte or cardinality limits. Requests carry identities and
  digests, never provider definitions, paths, commands, credentials, or provider output.
- `journal.py` canonicalizes each record, hashes the previous digest into the next record, appends
  one newline-delimited record, flushes and fsyncs the file, and exposes exact-prefix recovery.
  The journal record phases are `watermark-installed`, `takeover-superseded`,
  `takeover-acknowledged`, `admitted`, `mutation-started`, `provider-returned`, `observed`, and
  `terminal`. Every record repeats the authority instance, System, activation, generation,
  operation and attempt identities, purpose, the exact adapter operation/commit point, request
  digest, and phase-specific bounded evidence.
  Mutation records additionally require source and target identities plus stable recovery-object
  ownership. Takeover records forbid those mutation-only fields and instead bind their predecessor
  takeover generation where applicable.
- `service.py` owns one `asyncio.Lock` mutation lane per System. Under that lane it restores and
  verifies the exact journal head, installs a newer generation watermark, resolves every older
  admitted operation, appends and anchors each phase, rechecks the generation immediately before
  every provider commit, and returns only evidence already fsynced and anchored.
- `repository.py` is the only database-facing port used by the service. It resolves an opaque 0122
  authority binding and performs migration 0123's monotonic head compare-and-set. The authority
  role cannot allocate a generation or change lifecycle, activation, Run, job, or accounting
  state.

The authority service is an in-process application boundary in this issue. Its callable interface
accepts an already authenticated peer identity supplied by the hosting transport and a provider
adapter selected at composition. Mutual-TLS listener and credential provisioning are deployment
work, not silently approximated here. Tests use an authenticated peer fixture; production assembly
must not advertise the service without such a peer-authentication boundary.

## Protocol and authentication

An `AuthorityTakeoverRequestV1` contains the opaque authority UUID, positive generation, System,
activation, Run, plan digest, purpose, provider kind, authority instance, operation identity, and
operation digest. An `AuthorityMutationRequestV1` repeats that immutable binding and adds the
admitted operation, expected source identity, intended target identity, and at most 1,024 stable
recovery-object bindings. Takeover resolves an `allocating` 0122 binding and performs no provider
mutation. After core records the acknowledgement and promotes the binding, mutation resolves the
same binding in `current` state before provider access. Every identifier is nonblank and at most 255 UTF-8
bytes; provider identities are opaque nonblank strings of at most 1,024 UTF-8 bytes; digests use
`sha256:` plus 64 lowercase hexadecimal digits. Serialized input is closed and capped at 1 MiB.

The service asks migration 0122 to resolve the opaque reference for the authenticated active worker
incarnation and requires every immutable field to match before journal admission. Takeover accepts
only the exact newest `allocating` binding; mutation accepts only the exact `current` binding with
its recorded acknowledgement. Possession of the reference is insufficient. The provider adapter receives the validated typed request, never the
peer credential. Failures return a bounded category and identity references; no raw provider
definition, output, path, command, or secret crosses the shared response boundary.

## Serialized mutation and takeover

The per-System lane makes admission, watermark changes, provider commit points, and observations a
single ordered history. A takeover request at generation `G` is admitted only when `G` equals the
newest allocating binding resolved from Postgres and is not below the installed lane watermark. Before acknowledging `G`, the
service persists and anchors its watermark and classifies every lower-generation operation as one
of: provider mutation never began, returned and observed, lost response resolved to the exact
source or target identity, or conflict. Timeout, cancellation, disconnect, task termination, or an
unobserved provider return is unresolved and therefore withholds acknowledgement.

Takeover first appends and anchors `watermark-installed` for `G`, immediately preventing any new
lower-generation admission or provider commit. It then positively resolves every operation already
admitted below `G`. Those operations may append only `provider-returned`, `observed`, and `terminal`
records for their pre-watermark operation identities; they may not append `admitted` or
`mutation-started` or reach another provider commit. After every such operation is terminal,
takeover appends and anchors `takeover-acknowledged`. These takeover records use the request's
immutable operation identity and digest and perform no provider access.

An operation paused after anchored `admitted` but before `mutation-started` may append one terminal
`never-began` record directly from that admitted record. It performs no provider access and carries
no provider observation. An operation with anchored `mutation-started` must instead append the
bounded returned/observed/terminal sequence needed to classify its provider outcome.

The acknowledgement returned to migration 0122 carries three distinct values: the exact sequence
and digest of the anchored `takeover-acknowledged` record, plus `positive_quiescence_digest`. The
quiescence digest is SHA-256 over canonical JSON containing the authority instance, System,
generation, watermark sequence and digest, and the journal-ordered list of every lower operation's
terminal sequence, digest, and closed outcome category. An empty lower-operation list is explicit.
Migration 0122 persists this value separately from the acknowledgement journal digest.

Allocation can supersede `G` while it is quiescing. If the exact newer allocating generation wins
the System lock before `G` anchors acknowledgement, `G` may not acknowledge. The newer takeover
must append and anchor `takeover-superseded`, binding `G`'s watermark record and the newer
generation, before installing its own watermark. This canonical transition closes the incomplete
takeover without claiming its lower operations quiescent; the newer takeover inherits and resolves
all still-nonterminal lower operations. Thus concurrent allocation cannot strand the lane, and a
superseded takeover can never produce an acknowledgement.

Client cancellation never cancels an admitted provider call. The lane-owned task retains the
adapter call and its recovery-object ownership until it reaches a positive observation. On process
restart the journal supplies the admitted work; service readiness remains false until recovery
restores exact continuity and resolves all nonterminal records. This implementation does not claim
survival if an operating-system process can die while its provider call continues independently;
the hosting supervisor must preserve the execution owner as ADR-0584 requires before the provider
advertises v1.

Core records that acknowledgement through migration 0122 and promotes `G` to `current`; only then
may a separate mutation request enter the lane. Immediately before each adapter commit, the service
re-resolves the authority binding and requires `current`, the same generation and immutable fields,
and the acknowledgement's exact journal sequence and digest. A stale generation stops without provider access. A
multi-commit operation rechecks separately for every commit; loss of authority leaves later commits
unattempted and records the last observed partial state for successor classification.

## Journal and database checkpoint

Migration 0123 adds one `external_boot_authority_journal_heads` row per `(authority_instance,
system_id)`. It stores a positive sequence, exact record digest, phase, authority UUID, generation,
operation identity, and update time. Because the lane admits at most one mutation at a time, the
same row also has two nullable, bounded continuations: `pending_takeover` retains the takeover
authority, generation, operation, attempt, request digest, watermark sequence, and watermark digest;
`suspended_operation` retains the lower authority, generation, System, activation, Run, plan,
provider, authority instance, operation identity, attempt, purpose, exact adapter operation/commit
point, request digest, last phase, and source/target/ownership digests. These are verification
state, not another journal: they contain no provider definitions, output, or record history and are
cleared when the corresponding operation or takeover becomes terminal.

A security-definer compare-and-set function authenticates the provider-authority role, locks the
System and head row, resolves the exact 0122 binding, and accepts
only `(expected_sequence, expected_digest) -> (expected_sequence + 1, new_digest)`. The initial
expected head is sequence zero and the fixed genesis digest. Sequence overflow, a non-current or
mismatched binding, invalid phase transition, duplicate replay with different facts, or unexpected
prior head changes zero rows and returns `superseded` or `conflict` without moving the checkpoint.

Operation identity normally cannot switch before terminal. The only exceptions are checked against
the row's retained continuations. Anchoring a watermark atomically copies the exact current head's
nonterminal operation into `suspended_operation` and installs `pending_takeover`. A watermark may
switch only to completion records matching every suspended field; terminal completion clears the
suspended continuation and may switch only to the retained pending takeover. Watermark `G` may
switch to `takeover-superseded` only for the exact newer allocating generation, which atomically
replaces `pending_takeover` while retaining the suspended operation. Each exception revalidates the
immutable System, activation, authority instance, operation, attempt, request digest, generation,
phase, and ownership digests. No exception admits work or permits provider access; every other
nonterminal operation switch conflicts without moving the head.

The continuation columns are updated in the same transaction as the exact head CAS. Constraints
require the complete field group or all null, cap every text/digest field, and prohibit a suspended
terminal phase. Fabricated operation identities, wrong prior phases, attempts, digests, ownership,
or unrelated takeover continuations therefore change zero rows without trusting caller-supplied
journal history. A bounded chain proof is deliberately not accepted: SQL cannot authenticate local
journal bytes beyond the one exact head it already trusts, while the two fixed continuations are
sufficient because the lane has at most one admitted mutation and one takeover in progress.

The binding-state rule is phase-specific. `watermark-installed`, `takeover-superseded`, and
`takeover-acknowledged` accept only the exact newest `allocating` binding. New mutation admission
and commit phases accept only the exact `current` binding carrying the recorded takeover
acknowledgement. After a watermark, only the authenticated pending successor binding may append
completion records
for an operation whose `admitted` record is already anchored in that lane. Without
`mutation-started`, only terminal `never-began` is legal; with it, only
returned/observed/terminal completion is legal. The function verifies that immutable operation
identity against `suspended_operation`; it never impersonates the lower binding. Provider access
follows this trusted-head authorization. Every other
cross-state or cross-phase attempt changes zero rows. `takeover-acknowledged` follows its matching
watermark and all inherited lower terminal records. A mutation may start only after that exact
acknowledgement is recorded by 0122 and the binding is current.

For every phase the service appends and fsyncs locally, then advances the database head. On first
creation it opens the lane journal without following symlinks, creates it exclusively with mode
`0600`, fsyncs the file, and fsyncs the parent directory before the first database compare-and-set;
existing journals must be regular, privately owned files with no writable group/other bits.
`admitted`
and `mutation-started` are both anchored before provider access. If the database update fails after
local fsync, the file has a longer suffix and service enters failed-closed recovery. It never trims
or adopts that suffix automatically. If local append fails, the database head cannot advance.

At startup, recovery parses the entire bounded journal and verifies canonical encoding, sequence,
digest chain, authority instance, System lane, phase ordering, and stable ownership. The local last
record must equal every field of the trusted database head. Empty-with-head, valid-prefix
truncation, an unanchored longer suffix, corruption, reorder, duplicate sequence, foreign identity,
or phase divergence refuses the affected lane and cannot acknowledge takeover. Repair is an
operator restoration of the exact retained journal bytes; neither API moves the head backward nor
declares a record absent.

The journal limit is exact on-disk bytes, including newlines. Its constructor-configurable
maximum defaults to 64 MiB per authority journal. Startup streams and validates every retained
record with bounded per-record input; the cached trusted tail then makes appends independent of
retained-history length. A prospective append beyond the maximum fails before file bytes,
database CAS, or provider progress and leaves readiness false. Recovery is audited restoration or
retention of exact bytes, never truncation or compaction. Production hosting configuration and
advertisement remain deferred to #2127.

## Stable recovery-object ownership

A recovery object is keyed by `(System, activation, recovery reference)` and repeats that owner in
every record that mentions it. Generation, peer, operation attempt, and takeover may change; the
owner tuple may not. The journal rejects duplicate references, reordered noncanonical lists,
cross-System or cross-activation ownership, and any later record that changes an existing binding.
Adapters receive only validated ownership records. A successor can resume or delete an object only
after an observation proves the same owner; otherwise it reports conflict. Teardown may destroy the
owned System under its distinct authority, but an unproven recovery object remains quarantined.

## Provider adapter contract

`AuthorityMutationAdapter` exposes provider-neutral `observe(request)` and
`commit(request, commit_point)` operations with closed result values. The service, not the adapter,
owns generation checks, serialization, journal phases, retries, and takeover. An adapter maps only
the named external-boot commit points to provider primitives and returns a bounded identity
observation.

Concrete local-libvirt and remote-libvirt adapters and their composition are owned by #2140 after
the provider primitives in #2108, #2110, and #2120 land. This issue proves the shared adapter
contract with a controllable test adapter. Production assembly must not advertise v1 before #2140
and the authenticated hosting boundary are installed. Existing `ProviderRuntime.external_boot` and
all non-external install, boot, control, capture, and reaping paths remain unchanged.

## Failure behavior and observability

Malformed or unauthenticated input is rejected before journal or provider access. A binding or
generation mismatch is `superseded`. Journal/database disagreement, invalid phase continuity, or
stable-owner disagreement is `journal_conflict`. An unreadable or mixed provider state is
`provider_conflict`. Provider unavailability leaves the operation unresolved and takeover blocked;
it is never converted to source or target by elapsed time.

Logs and exceptions contain authority, System, generation, sequence, phase, operation, and bounded
categories. They exclude peer credentials, provider credentials, definitions, commands, paths,
and raw output. Metrics count requests, rejection categories, unresolved older calls, recovery
failures, and checkpoint latency by provider kind and authority instance without tenant-controlled
labels.

## Threat model

### Boundary inventory and actors

- **Added: authenticated worker to authority service.** An authenticated but stale or compromised
  worker controls request bytes and may replay a valid reference.
- **Added: authority service to provider adapter.** The service controls typed commit intent; a
  provider or host administrator may return misleading, mixed, or unreadable state.
- **Added: authority journal to restarted process.** Local storage may be truncated, extended,
  reordered, corrupted, or replaced by bytes from another lane.
- **Added: authority service to Postgres.** A compromised authority process has its narrowly granted
  database role. Database availability can fail between local fsync and checkpoint.
- **Added: shared provider adapter contract.** Concrete local and remote mutation seams remain
  unchanged here and are owned by #2140.

The design trusts Postgres, the authority host's kernel and supervisor, the configured
provider-authority identity, and platform operators. Tenant agents never call this protocol
directly. A privileged host or database administrator is outside the fence.

### Controls

- Closed Pydantic models, byte/cardinality caps, canonical serialization, digest validation, and a
  1 MiB message cap reject malicious input before allocation or provider access.
- Peer identity plus migration 0122's exact active-incarnation and immutable-binding lookup defeats
  possession, replay, cross-System, cross-Run, cross-activation, cross-attempt, wrong-purpose,
  wrong-provider, and wrong-instance requests.
- Per-System serialization, pre-commit binding checks, and positive observation prevent a newer
  generation from acknowledging while an older call may still commit.
- File and parent-directory fsync before the first CAS, file fsync before every later CAS, exact
  monotonic checkpoints, and full-chain recovery expose truncation, extra suffixes, corruption,
  reorder, substitution, and partial database failure.
- Stable ownership validation prevents takeover from claiming another activation's recovery object.
- The provider-authority database role has execute-only access to the narrow functions and no
  lifecycle writes. Bounded structured diagnostics prevent secret and provider-output disclosure.

### Explicit exclusions

Deployment enforcement that workers and reconcilers lack mutation-capable sockets, SSH accounts,
filesystem permissions, and service credentials belongs to #2127. The authenticated transport's
listener and credential provisioning also belong to deployment assembly; this issue defines the
service's required authenticated-peer input and fails closed without it. Privileged host/database
administrators are trusted operators. Denial of service by withholding a provider response is
accepted in favor of false takeover. Scheduling, reconciliation, and lifecycle policy remain in
their current owners.

## Verification

Unit tests prove closed bounded takeover and mutation values, allocating-to-acknowledged-to-current
ordering, canonical journal hashing, every takeover and mutation phase transition,
stable ownership, cancellation retention, and adapter-contract behavior. Migration
tests prove role grants, exact binding resolution, monotonic compare-and-set, concurrency, zero-row
mismatch behavior, and immutable heads. Adversarial service tests pause each older operation before
and after each commit, lose responses, restart from every journal phase, and prove takeover waits
until positive observation. Recovery tests reject shorter, longer, divergent, reordered, corrupt,
duplicate, foreign, and valid-prefix-truncated journals, including a trusted `mutation-started`
head whose provider call remains unresolved.
Crash tests cover first-file exclusive creation, parent-directory fsync, and the initial checkpoint;
migration tests cover every allocating/current versus takeover/mutation phase combination and
concurrent takeover attempts. Restart tests stop after each takeover record and require the exact
anchored `takeover-acknowledged` sequence and digest before core promotion.

The focused gates are `just test-verbose tests/providers/external_boot_authority`,
`just test-verbose tests/db/test_external_boot_authority_journal_migration.py`. The branch gates
are `just lint`, `just type`, and `just ci`.

## Durable continuation facts

The branch is `feat/provider-host-authority-journal-2126`, the comparison base is `main`, and the
repository guardrails are the `just` recipes named above. There are no design deferrals.
