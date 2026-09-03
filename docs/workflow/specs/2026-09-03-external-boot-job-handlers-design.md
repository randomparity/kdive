# External-boot job payloads and lifecycle handlers — design

Issue: [#2205](https://github.com/randomparity/kdive/issues/2205). Parent: #2118. Blocker:
#2201, merged as `951fbaea0`. Decision record:
[ADR-0591](../../adr/0591-external-boot-operations-ride-marked-boot-and-teardown-jobs.md).

## Goal

Give the external-boot lifecycle an executable job surface. A `boot` or `teardown` job whose
payload carries a validated `ExternalBootAuthorityMarkerV1` is claimed by a worker, routed to a
per-operation handler, allocates authority as `kdive_worker`, calls the injected
`ExternalBootPorts` method, and returns an `ExternalBootAuthorityResultV1` the worker commits
through `commit_external_boot_authority_result`.

## What already exists

| Piece | Where | State |
|---|---|---|
| Provider port, six methods | `src/kdive/providers/ports/external_boot.py:308-330` | done |
| Injection slot | `src/kdive/providers/core/runtime.py:180` | done |
| Fault-inject implementation | `src/kdive/providers/fault_inject/lifecycle/external_boot.py:19` | done |
| Marker and result envelopes | `src/kdive/jobs/models.py:188`, `:221` | done |
| Worker marker decode + fence | `src/kdive/jobs/worker.py:534-573`, `:614-646` | done |
| Commit wrapper | `src/kdive/jobs/queue.py:267-332` | done |
| Authority SQL (allocate / acknowledge / commit) | `0122_external_boot_authority.sql:322`, `:556`, `:731` | done |
| Claim lane reopened for marked payloads | `0127_reopen_external_boot_claim_lane.sql` | done (#2201) |
| **Payload carrying a marker** | — | **this change** |
| **Handler package and registrar** | — | **this change** |

## Constraints the schema imposes

1. **No new `JobKind`.** `allocate_external_boot_authority` refuses unless
   `v_job.kind = (CASE WHEN p_purpose = 'teardown' THEN 'teardown' ELSE 'boot' END)`
   (`0122…sql:465`).
2. **The marker key is a literal.** The claim fence, the finalizer fence, and both authority
   functions test `payload ? 'external_boot_authority_v1'` against the top-level JSONB key.
3. **One handler per `JobKind`.** `HandlerRegistry.register` raises `DuplicateHandler`
   (`src/kdive/jobs/models.py:393-394`); `boot` and `teardown` are already bound.
4. **The worker may not write the external-boot tables.** `0121…sql:280-285` leaves it
   `SELECT` only; `commit_external_boot_authority_result` is the sole write path and finalizes
   the `jobs` row itself (`0122…sql:1661`, `:1681`).
5. **A commit needs an acknowledgement.** `0122…sql:904` refuses without one, and
   `acknowledge_external_boot_authority` is granted to `kdive_provider_authority` alone
   (`:1741-1744`).
6. **Generic finalization stays fenced.** `0122…sql:304-315` excludes marked jobs from
   `complete_worker_job` and `fail_worker_job`; #2201 left that half installed deliberately.
   Nothing in this change reopens it, and nothing re-implements the
   `repair_abandoned_jobs` predicate #2201 installed.

## Design

### 1. Payload models

`src/kdive/jobs/payloads.py` gains two models and swaps two registry entries:

```python
class BootPayload(RunPayload):
    external_boot_authority_v1: ExternalBootAuthorityMarkerV1 | None = None

class TeardownPayload(SystemPayload):
    external_boot_authority_v1: ExternalBootAuthorityMarkerV1 | None = None
```

`_ACTIVE_PAYLOAD_MODELS[JobKind.BOOT] = BootPayload`,
`_ACTIVE_PAYLOAD_MODELS[JobKind.TEARDOWN] = TeardownPayload`, and
`_RUN_PAYLOAD_MODELS[JobKind.BOOT] = BootPayload`.

`TEARDOWN` stays out of `_RUN_PAYLOAD_MODELS`: `TeardownPayload` has no `run_id` field, and
adding one would make the worker's `_compensation_run_id` transition a Run to `failed` on every
ordinary System teardown failure — a behavior change nothing asked for. `run_id_from_payload`
therefore returns the Run for `boot` (marked or not) and `None` for `teardown`, and a test pins
both.

An unmarked payload is unchanged on the wire: `dump_payload` already serializes with
`exclude_none=True` (`payloads.py:459`), so `{"run_id": …}` round-trips byte-identically and
every persisted pre-change job still decodes.

**Cross-field validation on the model** (a `model_validator(mode="after")` on each), so a bad
marker is rejected at `dump_payload` rather than at `allocate_external_boot_authority`:

- `BootPayload`: `marker.run_id == UUID(self.run_id)`; `marker.purpose != "teardown"`.
- `TeardownPayload`: `marker.system_id == UUID(self.system_id)`; `marker.purpose == "teardown"`.
- Both: `marker.operation` is admitted for `marker.purpose`, checked with the existing
  `operation_is_permitted(purpose, operation)`
  (`src/kdive/providers/external_boot_authority/protocol.py:91`), which is the same
  `_PURPOSE_OPERATIONS` table `_AuthorityBinding` and the SQL both enforce. Nothing new is
  defined and `jobs/models.py` is not modified.
- Both: `marker.operation` is one of the six enqueueable operations (below).

The marker's `activation_id`/`plan_identity` cannot be checked against the activation row inside
a Pydantic validator — it has no database. They are checked by the enqueue-side helper
`build_external_boot_payload` (below), which reads the activation row, and again by
`allocate_external_boot_authority` at execution.

### 2. Enqueue-side helper

`src/kdive/jobs/handlers/external_boot/admission.py`:

```python
async def build_external_boot_payload(
    conn: AsyncConnection,
    *,
    activation_id: UUID,
    purpose: Purpose,   # the alias at providers/external_boot_authority/protocol.py:28
    operation: str,
    provider_kind: str,
    authority_instance: str,
    operation_identity: str,
    resolver: ProviderResolver,
) -> tuple[JobKind, BootPayload | TeardownPayload]
```

It reads the activation row, so `run_id`, `system_id`, and `plan_identity` are **taken from the
row** rather than accepted from the caller — a marker whose facts disagree with the activation
cannot be constructed at all. `provider_kind` and `authority_instance` are caller-supplied,
because neither `ExternalBootActivation` nor `ExternalBootReservation` carries them; the helper
rejects a `provider_kind` that disagrees with the `ResourceKind` the resolver binds for that
System, and rejects one whose bound runtime has `external_boot is None`. It returns the
`JobKind` the marker's purpose requires, so no caller picks the kind by hand.

This is the "sourced explicitly by the enqueueing caller" criterion's home. #2204 wires the MCP
tools to it; this change ships the helper and its tests.

### 3. Routing

`HandlerRegistry` still binds one handler per kind. `register_all_handlers` builds the
operations registry once and passes it as a **required** keyword to both registrars, each of
which wraps its ordinary handler in the one shared router:

```python
# src/kdive/jobs/handlers/external_boot/router.py
def route_marked(operations: ExternalBootOperations, ordinary: JobHandler) -> JobHandler:
    async def handler(conn: AsyncConnection, job: Job) -> JobHandlerResult:
        if EXTERNAL_BOOT_AUTHORITY_MARKER_KEY not in job.payload:
            return await ordinary(conn, job)
        return await operations.run(conn, job)
    return handler
```

The router branches on **presence of the key, not validity of the marker** — the same rule
`src/kdive/jobs/worker.py:614-624` already applies, whose comment states "Presence, rather than
validity, selects the fail-closed path". So the router needs no decoder of its own and
`worker.py` is not modified. Decoding happens inside `operations.run`, which loads the payload
(and with it the validated marker); a malformed marker fails there with a payload validation
error, and never reaches `boot_handler` or `teardown_handler`.

The keyword is required rather than defaulted: a registrar call site that omitted it would send
a marked job to `boot_handler` or `teardown_handler`, which boot a Run and tear a System down.
Making it required means that mistake does not compile.

`boot_handler` and `teardown_handler` each change one line — `load_payload(job, BootPayload)`
and `load_payload(job, TeardownPayload)` — because `load_payload` requires an exact model match
(`payloads.py:467-470`).

### 4. Operations registry

```python
# src/kdive/jobs/payloads.py — defined here because the payload validator is its first consumer
ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS: Final[frozenset[str]] = frozenset(
    {"activate", "recover", "resolve-conflict", "release", "cleanup", "teardown"}
)

# src/kdive/jobs/handlers/external_boot/operations.py
type ExternalBootOperationHandler = Callable[
    [AsyncConnection, Job, ExternalBootAuthorityMarkerV1], Awaitable[ExternalBootAuthorityResultV1]
]

class ExternalBootOperations:
    def register(self, operation: str, handler: ExternalBootOperationHandler) -> None: ...
    def get(self, operation: str) -> ExternalBootOperationHandler | None: ...
    async def run(self, conn, job, marker) -> ExternalBootAuthorityResultV1: ...
```

`register` raises `DuplicateExternalBootHandler` on a second registration for an operation, and
refuses an operation outside `ENQUEUEABLE_EXTERNAL_BOOT_OPERATIONS`. `run` refuses an unregistered
operation — which is exactly `deadline`, `recovery-attempt`, and `fail` — with a terminal
`CategorizedError(configuration_error)` naming the operation. `deadline` and `recovery-attempt`
are mid-operation commits that deliberately leave the `jobs` row `running`
(`0122…sql:1685-1687`); re-entry across them is #2202's. `fail` is a result carrier, never an
admission.

### 5. Handler ports

```python
# src/kdive/jobs/handlers/external_boot/ports.py
from kdive.providers.external_boot_authority.protocol import (
    AuthorityAcknowledgementV1,
    AuthorityTakeoverRequestV1,
)

class ExternalBootAuthorityAcknowledger(Protocol):
    async def acknowledge(
        self, request: AuthorityTakeoverRequestV1
    ) -> AuthorityAcknowledgementV1: ...

@dataclass(frozen=True, slots=True)
class ExternalBootHandlerPorts:
    resolver: ProviderResolver
    incarnation_credential: SecretStr
    acknowledger: ExternalBootAuthorityAcknowledger | None = None
```

**No new model.** `AuthorityTakeoverRequestV1`
(`src/kdive/providers/external_boot_authority/protocol.py:131`, an `_AuthorityBinding` at
`:102-128`) already carries `authority_id`, `generation`, `system_id`, `activation_id`,
`run_id`, `plan_identity`, `purpose`, `operation`, `provider_kind`, `authority_instance`,
`operation_identity`, and `operation_digest` — exactly what the handler holds after allocation,
with its own `_operation_matches_purpose` validator. `AuthorityAcknowledgementV1` (`:209`)
already returns `journal_sequence`, `journal_digest`, and `positive_quiescence_digest` — exactly
the three facts `commit_external_boot_authority_result` requires and a worker session cannot
produce, because `acknowledge_external_boot_authority` is `kdive_provider_authority`-only by
ADR-0584's design. The four further arguments that function takes — `allocation_id`, `job_id`,
`job_attempt`, `worker_incarnation` — are the host's to resolve from `external_boot_authorities`,
on which it holds `SELECT` (`0122…sql:1716-1719`).

`protocol.py` imports only stdlib and pydantic, so reusing it costs the handler package nothing
in its import closure.

`acknowledger is None` fails the operation closed with `configuration_error` **before** the
provider is touched, so an unwired deployment never leaves a half-applied mutation. Wiring it to
the authority host's transport is #2199/#2200; the seam is what makes an operation executable
and testable here.

### 6. Handler shape

Every operation handler is the same seven steps, in `operations/common.py`:

1. **Resolve.** `binding = await resolver.binding_for_system(conn, marker.system_id)`. Refuse
   when `binding.kind.value != marker.provider_kind` or `binding.runtime.external_boot is None`
   (`configuration_error`, terminal).
2. **Read the activation.** `SELECT`-only, permitted. Refuse when the row is absent, when its
   `run_id`/`system_id`/`plan_identity` disagree with the marker, or when the evidence the
   operation consumes is missing — for `activate` that is `materialization` **and**
   `recovery_point` (the `prepared-before-admission` disposition, ADR-0591 decision 4).
3. **Allocate.** `allocate_external_boot_authority` as `kdive_worker`. `superseded` raises a
   non-terminal `CategorizedError(stale_handle)`; the SQLSTATE `42501` from a non-worker session
   propagates unchanged, which is what criterion 8 asserts.
4. **Acknowledge.** Through the seam, binding every allocated fact. A `superseded`
   acknowledgement raises `stale_handle` before the provider is touched.
5. **Call the port**, on a thread (`asyncio.to_thread`) because `ExternalBootPorts` is sync,
   like every other provider call in `jobs/handlers/`.
6. **Build the result.** Evidence composed from the persisted activation evidence, the
   acknowledgement, and the port's own return; `composite_state` is the acknowledgement's
   `positive_quiescence_digest`.
7. **Return it.** The worker commits: `_finalize_handler` checks `_authority_binding_matches`
   (`worker.py:630-646`) and calls `queue.complete_external_boot` /
   `queue.fail_external_boot`. The handler never calls the commit function itself — that check
   is the one thing standing between a mismatched result and the authority tables.

A provider exception is wrapped in `ExternalBootAuthorityFailure` carrying an
`ExternalBootAuthorityFailureV1` bound to the same allocation, with
`failure_context.phase` set to the step that failed (`admission`, `preparation`,
`provider-call`, `observation`, `commit`) and `error_category` mapped from the raised
`CategorizedError` where there is one and `infrastructure_failure` otherwise. Richer category
mapping is #2202's; this change ships the honest default and one test pinning it.

### 7. Per-operation detail

| Operation | Kind / purpose | Port call | Result variant | Activation after an applied commit |
|---|---|---|---|---|
| `activate` | boot / activate | `activate` then `observe` | `_ActivateResult` | `active`, `terminal_evidence` set |
| `recover` | boot / recover | `recover` then `observe` | `_RecoverResult` | `recovered` |
| `resolve-conflict` | boot / resolve-conflict | `recover` then `observe` | `_RecoverResult` | `recovered` |
| `release` | boot / release | `observe` | `_ReleaseResult` | reservation released |
| `cleanup` | boot / release | `cleanup` | `_CleanupResult` | `cleanup_complete = true` |
| `teardown` | teardown / teardown | `cleanup` | `_TeardownResult` | `cleanup_complete`, System `torn_down` |

- **activate** requires activation state `activating` at commit (`0122…sql:1302-1306`), so the
  handler refuses a `prepared` activation that the server has not moved to `activating`;
  `allocate` admits both, the commit admits only `activating`, and failing early is cheaper than
  a superseded commit. `activation_readiness_deadline` comes from the acknowledgement's clock,
  not the handler's: the handler adds
  `ACTIVATION_READINESS_WINDOW` (a module constant, 15 minutes) to `now(UTC)` and states the
  full five-part limit contract in the docstring.
- **release** reads the `external_boot_reservations` row in state `ready` and copies
  `store_identity`, `owner_key`, and `reserved_bytes` from it verbatim — the commit re-checks
  all three against the same row (`0122…sql:1535-1541`), so any value the handler invented
  would be rejected there. `release_identity` is `sha256` over the canonical release evidence.

  **Settled reading of `objects`.** ADR-0583's prose orders the sequence "deletes and verifies
  the owned objects, releases the reservation exactly once, commits cleanup complete"
  ([ADR-0583](../../adr/0583-external-run-boot-uses-prepared-recovery-points.md), lines
  373-376), which would put the deletion before the release. The merged
  ADR-0584 adapter says otherwise and is what shipped: `LocalExternalBootAuthorityAdapter`
  lists `RELEASE` in neither `_MUTATING_OPERATIONS`
  (`src/kdive/providers/local_libvirt/external_boot_authority.py:53-60`) nor
  `_DELETING_OPERATIONS` (`:65`), and its comment states that release's "provider effect is
  exactly one observation" because "ADR-0584 makes conflict resolution, release, and teardown
  each allocate their own later generation before mutating" (`:49-52`). Deletion therefore
  belongs to `cleanup`, under a later generation, and `release` observes.

  So at release time no owned object is yet absent. `_ReleaseObject` can only represent an
  absent object (`absent: Literal[True]`, `src/kdive/jobs/models.py:65-67`), so the truthful
  enumeration is empty. The handler enumerates over exactly the activation's recorded object
  references — the `materialization.artifacts` refs and the `recovery_point.recovery_ref`,
  which are also the only refs the commit's `known_refs` recursion accepts
  (`0122…sql:1229-1263`) — includes only those `port.observe` shows gone, and sets
  `enumeration_complete` because that domain is the complete one. For every adapter that exists
  today the result is `objects: []` on a healthy activation, and a non-empty list on one whose
  objects a prior partial cleanup already removed. The handler never asserts `absent` for an
  object it did not check. A store-side enumeration richer than the activation's own recorded
  refs would need a port `ExternalBootPorts` does not have; adding one is #2199/#2200's.
- **cleanup** requires a recorded release (`0122…sql:1398`), reads its `release_identity`, and
  sets `mode` from the activation state — `ordinary` for `recovered`/`abandoned`,
  `system_teardown` for `recovery_conflict`/`recovery_failed`, matching `0122…sql:1400-1413`.
- **teardown** carries both evidences in one result and is the only operation on the
  `teardown` kind.

Every evidence `objects` entry must be a reference the commit's `known_refs` recursion already
knows (`0122…sql:1229-1263`): the handler draws them from the activation's persisted
`materialization` and `recovery_point` and the reservation row, never from a value it composed.

### 8. Failure and terminality

`commit_external_boot_authority_result` finalizes the `jobs` row itself. A success operation
sets `succeeded` (`0122…sql:1681-1684`); the `fail` operation sets `failed` when the result is
terminal or the attempt is the last, and `queued` otherwise (`:1660-1669`). The handler reads
the returned `(status, job_state)` through `queue.commit_external_boot_authority_result`, which
returns the post-commit `Job` on `applied` and `None` on `superseded`. Criterion 6's "an applied
and a not-applied commit each leave the `jobs` row in a terminal state, never `running`" is
asserted against those two: a success commit leaves `succeeded`, a terminal failure commit
leaves `failed`.

A commit that returns `superseded` writes no `jobs` row at all, so the job stays `running` until
its lease lapses, and both generic finalizers and `repair_abandoned_jobs` are fenced against it.
That window is not closed here: it is the availability-only leak #2203 owns, and closing it from
the worker would require a second terminalizer competing with the `SECURITY DEFINER` commit. A
test records the behavior so it cannot change silently.

### 9. Import closure

`src/kdive/jobs/handlers/external_boot/` imports only `kdive.jobs`, `kdive.db`,
`kdive.domain`, `kdive.providers.ports`, and `kdive.providers.core`. The `provider_kind`
literals are data. A closure-walking test mirrors the gate
`tests/services/external_boot/test_recovery_requests.py` already uses.

## Threat model

**Boundaries added.** (a) The job payload: a marker read back out of Postgres and used to select
an operation and address an activation. (b) The acknowledgement seam: facts from a
provider-authority process used to compose a result the database commits. (c) The provider port
call.

**Boundaries widened.** The `boot` and `teardown` dispatch paths now branch on payload content.
Before this change every `boot` job went to `boot_handler`.

**Actors.** An authenticated tenant who can enqueue jobs through the MCP surface (indirectly,
via #2204's tools); a compromised or buggy provider-authority process on the same host; a
worker that has lost its lease to a reclaim. Anonymous internet actors reach none of this. The
design trusts Postgres and the `SECURITY DEFINER` functions, and trusts nothing the payload or
the provider says about identity.

**Controls.**

| Boundary | Control |
|---|---|
| Payload marker → operation selection | Closed `Literal` on `operation`; registry refuses anything outside the six enqueueable names; `extra="forbid"` on every model |
| Payload marker → activation addressing | Marker/payload cross-validation in the model; `build_external_boot_payload` takes `run_id`/`system_id`/`plan_identity` from the activation row rather than the caller; `allocate_external_boot_authority` re-checks all nine marker fields (`0122…sql:467-474`) |
| Marker → wrong handler | Required-keyword routing; a marked job cannot reach `boot_handler`/`teardown_handler` |
| Handler → authority tables | No direct write is possible: `0121…sql:280-285` grants `SELECT` only. Every mutation goes through the `SECURITY DEFINER` commit, which re-checks the whole binding |
| Result → commit | `_authority_binding_matches` (`worker.py:630-646`) before the SQL call, then the commit's own re-check of every field |
| Acknowledgement facts | Bound to the exact allocated `authority_id`/`generation`; the commit re-checks `journal_sequence`, `journal_digest`, and the digest against the stored acknowledgement row (`0122…sql:937-942`), so a lying seam is refused by Postgres, not by the handler |
| Absent acknowledger | Fails closed before the port call — no partial provider mutation |
| Evidence content | The commit's forbidden-key scan (`0122…sql:978-979`) rejects anything resembling a credential, command, path, URL, or XML; handlers compose evidence only from persisted rows and closed models |
| Failure context | `_FailureContext` admits one field, `phase`, from a closed `Literal`; no message text crosses into the authority audit |

**Out of scope.** Authenticating the provider-authority peer (ADR-0584's mTLS transport, not
wired here); rate-limiting enqueue (the MCP admission surface, #2204); the availability leak
when a commit is superseded (#2203).

## Testing

Behavioral, against a real Postgres where the assertion is about SQL behavior. Every new test is
bite-proved: fix committed first, controlled fault injected, clean assertion failure observed,
fault reverted, file verified byte-identical.

1. **Payload contracts** (`tests/jobs/test_external_boot_payloads.py`) — round-trip through
   `dump_payload`/`load_payload`; `extra="forbid"`; unmarked `{run_id}` still decodes and
   re-dumps identically; `run_id_from_payload` returns the Run for `boot` and `None` for
   `teardown`; every marked payload's kind is `BOOT` or `TEARDOWN`; marker/payload disagreement
   on `run_id`, `system_id`, purpose/kind, and purpose/operation each rejected with a named
   message.
2. **Enqueue helper** (`tests/jobs/handlers/external_boot/test_admission.py`, Postgres) — the
   helper sources identity from the activation row; a `provider_kind` disagreeing with the
   resolved binding is rejected at validation, asserted by showing
   `allocate_external_boot_authority` is never reached; a runtime with `external_boot is None`
   is rejected.
3. **Registry** (`test_operations.py`) — the production registry from
   `build_production_handler_registry` resolves each of the six operations to exactly one
   handler; a second registration raises; `deadline`, `recovery-attempt`, and `fail` are refused
   by name.
4. **Routing** (`test_router.py`) — an unmarked `boot` job reaches the ordinary handler and a
   marked one does not; the same for `teardown`; a malformed marker reaches the operations
   registry, not the ordinary handler.
5. **Operation execution** (`test_lifecycle.py`, Postgres, fault-inject port) — per
   operation: the port method is called with exactly the persisted recovery point; the commit
   applies; the activation row holds the expected `ExternalBootActivationState`; the `jobs` row
   is `succeeded`. A failure result leaves the `jobs` row `failed`.
6. **Authority binding** (in `test_lifecycle.py`) — a handler result mutated in each of five fields
   is rejected by `_authority_binding_matches` and never reaches the commit.
7. **Role gate** (`test_role_gate.py`, Postgres) — the same handler under a `kdive_server`
   login raises `psycopg.errors.InsufficientPrivilege` with SQLSTATE `42501` and message
   `worker authority is required`.
8. **Disposition pin** (`test_prepared_before_admission.py`) — an activate job against an
   activation with `recovery_point` NULL fails without calling the port; the handler calls
   neither `materialize` nor `prepare` on any path.
9. **End to end** (`tests/integration/test_external_boot_job_lifecycle.py`, Postgres) — enqueue
   a marked job, claim it with a real `Worker` (which needs #2201's migration), run the
   registered handler against the fault-inject port, assert the activation reaches its terminal
   state and the job is terminal.
10. **Import closure** (`test_import_closure.py`) — no module under
    `kdive.providers.local_libvirt`, `kdive.providers.remote_libvirt`, or `libvirt`.
11. **Superseded-commit behavior** (in `test_lifecycle.py`) — records that a superseded
    commit leaves the `jobs` row `running`, naming #2203 as owner.

## Out of scope

Observe-driven re-entry, deadline reuse, provider-exception category mapping (#2202);
reconciler detection lanes and the superseded/abandoned leak (#2203); flipping the three agent
contracts live (#2204); the local and remote provider adapters and their authority composition
(#2199, #2200); the worker-side client of the authority host transport; any migration.

## Global constraints

- Python 3.14, `uv`. Ruff line length 100, lint set `E,F,I,UP,B,SIM`. `ty` strict, whole tree.
- Guardrails: `just ci` (bare, never piped); `just lint`, `just type`, `just test-changed` while
  iterating. Base branch `main`; branch `feat/external-boot-job-handlers-2205` off
  `54f346f553861f949c7df1957cae6f7915673231`.
- Prose rule: **Milestone** not "Sprint"; avoid critical, robust, comprehensive, elegant.
- No new dependency. No migration. No new `JobKind`. No edit to
  `0122_external_boot_authority.sql`, `0127_reopen_external_boot_claim_lane.sql`, or
  `src/kdive/reconciler/repairs/jobs.py`.
