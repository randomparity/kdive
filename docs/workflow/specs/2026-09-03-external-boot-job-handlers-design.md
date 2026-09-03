# External-boot job payloads and lifecycle handlers — design

Issue: [#2205](https://github.com/randomparity/kdive/issues/2205). Parent: #2118. Blocker:
#2201, merged as `951fbaea0`. Decision record:
[ADR-0593](../../adr/0593-external-boot-operations-ride-marked-boot-and-teardown-jobs.md).

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
  `operation_is_permitted(marker.purpose, AuthorityOperation(marker.operation))`
  (`src/kdive/providers/external_boot_authority/protocol.py:91`), which is the same
  `_PURPOSE_OPERATIONS` table `_AuthorityBinding` and the SQL both enforce. Nothing new is
  defined and `jobs/models.py` is not modified. The `AuthorityOperation(...)` coercion is
  required rather than cosmetic: the function is annotated
  `(purpose: str, operation: AuthorityOperation)` and `ExternalBootAuthorityMarkerV1.operation`
  is a bare `Literal[str]`, so passing it through unconverted is a `ty` error even though the
  `StrEnum` membership test would succeed at runtime. The coercion doubles as the closed-set
  check, raising `ValueError` on anything outside the nine names.
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
   `recovery_point` (the `prepared-before-admission` disposition, ADR-0593 decision 4).
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

| Operation | Kind / purpose | Required activation state | Port call | Result variant | Activation after an applied commit |
|---|---|---|---|---|---|
| `activate` | boot / activate | `activating` | `activate` then `observe` | `_ActivateResult` | `active`, `terminal_evidence` set |
| `recover` | boot / recover | `recovering` † | `recover` then `observe` | `_RecoverResult` | `recovered` |
| `resolve-conflict` | boot / resolve-conflict | `recovery_conflict` † | `recover` then `observe` | `_RecoverResult` | `recovered` |
| `release` | boot / release | `active`, `recovered`, `abandoned`, `recovery_conflict`, `recovery_failed` ‡ | `observe` | `_ReleaseResult` | reservation released |
| `cleanup` | boot / release | `recovered`, `abandoned`, `recovery_conflict`, `recovery_failed` ‡ | `cleanup` | `_CleanupResult` | `cleanup_complete = true` |
| `teardown` | teardown / teardown | `recovery_conflict`, `recovery_failed` ‡ § | `cleanup` | `_TeardownResult` | `cleanup_complete`, System `torn_down` |

The **required activation state** column is the set `run_operation` passes as
`require_activation_state`, and it is taken from the **commit** preconditions
(`0122…sql:1302-1335`), not from `allocate`'s (`:482-500`). The two differ, and the commit's is
the tighter one: `allocate` admits `activate` from `prepared` *or* `activating` and `recover`
from `active` *or* `recovering`, while the commit admits only the second of each pair. Passing
`allocate`'s looser set would let a handler allocate, acknowledge, and call the provider, and only
then be refused at commit — landing in the re-execution window §8 describes with the System
already mutated. Failing before allocation is the cheaper and safer end.

Three preconditions in that table are **not** activation state, and each has to be checked
separately:

- **†** `recover` additionally requires the current recovery-attempt row to be in state
  `recovering`, and `resolve-conflict` requires it in state `conflict` (`0122…sql:1307-1318`).
  **Nothing in this change creates or advances that row.** It is written by the
  `recovery-attempt` operation, which this design excludes as #2202's (ADR-0593 decision 4). So
  in production the `recover` and `resolve-conflict` handlers are registered and reachable but
  cannot reach an applied commit until #2202 supplies the attempt row; their tests seed it
  directly, exactly as `tests/db/external_boot_authority_support.py:214-229` already does for the
  teardown case. This is stated rather than worked around, and a test pins it.
- **‡** `cleanup` and `teardown` additionally require `NOT cleanup_complete`, and `release`
  requires that no `external_boot_reservation_releases` row exists yet
  (`0122…sql:1319-1335`).
- **§** `teardown` additionally requires `systems.state = 'failed'` (`0122…sql:1331-1335`).

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

  **`objects` is always empty under every adapter that exists today, and that is the truthful
  value.** Release performs no deletion: ADR-0584's merged adapter lists `RELEASE` in neither
  `_MUTATING_OPERATIONS` (`src/kdive/providers/local_libvirt/external_boot_authority.py:53-61`)
  nor `_DELETING_OPERATIONS` (`:65`), because deletion belongs to `cleanup` under a later
  generation. So at release time no owned object is absent, and `_ReleaseObject` can represent
  only an absent object (`absent: Literal[True]`, `src/kdive/jobs/models.py:65-67`).

  `ExternalBootPorts` has no method that reports per-object absence — `observe` returns a
  `RunningKernelObservation` of `architecture`, `release`, and `gnu_build_id`
  (`src/kdive/providers/ports/external_boot.py:242-245`, `:324-326`), one call per recovery
  point, carrying no object identity. So the handler emits `objects: []` unconditionally, and
  `enumeration_complete` is truthful because the domain it can check is empty rather than because
  it checked and found nothing. The handler never asserts `absent` for an object it did not
  check, and this design deliberately does not give it a way to. A store-side enumeration needs a
  port that does not exist; adding one is #2199/#2200's.

  That ordering departs from ADR-0583's stated invariant, and the departure has a capacity
  consequence this specification does not have the authority to settle: the release commit
  `DELETE`s the reservation row and credits `reserved_bytes` back while the objects still exist.
  It is recorded as
  [deferral record 0010](../../debt/0010-external-boot-release-credits-capacity-before-cleanup.md).
- **What the `observe` calls are for.** `observe` returns a `RunningKernelObservation` and
  nothing in `ExternalBootTerminalEvidenceV1` consumes it — `composite_state` is the
  acknowledgement's `positive_quiescence_digest`, not a digest derived from the observation. The
  call is a **post-mutation liveness precondition**: `activate`, `recover`, and
  `resolve-conflict` require that the observed running kernel equals the one the activation's
  persisted `materialization.kernel_observation` records, and refuse to emit terminal evidence
  when it does not. That is the whole of its contribution, and the handler discards the value
  after the comparison. `release` calls it for the same reason — to establish the recovery point
  is still the one being released — and likewise reads nothing out of it.
- **cleanup** requires a recorded release (`0122…sql:1398`), reads its `release_identity`, and
  sets `mode` from the activation state — `ordinary` for `recovered`/`abandoned`,
  `system_teardown` for `recovery_conflict`/`recovery_failed`, matching `0122…sql:1400-1413`.
- **teardown** carries both evidences in one result and is the only operation on the
  `teardown` kind.

Every evidence `objects` entry must be a reference the commit's `known_refs` recursion already
knows: the handler draws them from the activation's persisted `materialization` and
`recovery_point` and the reservation row, never from a value it composed. That is a **subset** of
what `known_refs` accepts — the recursion seeds from four activation columns
(`materialization`, `recovery_point`, `pre_recovery_evidence`, `terminal_evidence`) and unions
`external_boot_reservations` and `external_boot_reservation_releases`
`store_identity`/`owner_key` (`0122…sql:1204-1249`). Restricting to the narrower set is
deliberate: it is the set whose provenance the handler can establish from rows it read.

### 8. Failure and terminality

`commit_external_boot_authority_result` finalizes the `jobs` row itself. A success operation
sets `succeeded` (`0122…sql:1681-1684`); the `fail` operation sets `failed` when the result is
terminal or the attempt is the last, and `queued` otherwise (`:1660-1669`).

**The handler does not call the commit; the worker does.** `_finalize_handler`
(`src/kdive/jobs/worker.py:534-542`) checks `_authority_binding_matches` and then calls
`queue.complete_external_boot` or `queue.fail_external_boot`, and that check is the one thing
standing between a mismatched result and the authority tables — which is why ADR-0593 rejects a
self-committing handler. The Python wrapper collapses the SQL's `(status, job_state)` to
`Job | None`: `queue.commit_external_boot_authority_result`
(`src/kdive/jobs/queue.py:267-306`) selects both columns, branches on
`status != "applied"`, and discards `job_state`.

So charter criterion 6's "the handler reads the `(status, job_state)` … and finalizes the job on
it" is met by a different component than the criterion names, and deliberately: the
`SECURITY DEFINER` commit is the sole terminalizer of an authority-marked job, the worker reads
`status` through the wrapper, and no code reads `job_state`. Recording the divergence here makes
it a stated decision rather than an apparent miss. The criterion's assertion still holds and is
tested: a success commit leaves the `jobs` row `succeeded`, and a terminal failure commit leaves
it `failed` — never `running`.

**The window in which a marked job is left `running` is a re-execution window, not an
availability window.** A commit that returns `superseded` writes no `jobs` row at all, and neither
does a handler exception that is not a binding-matching `ExternalBootAuthorityFailure` — the
worker logs and returns (`worker.py:505-517`). Either way the job keeps its lease until it lapses,
and then `claim_worker_job` re-claims it and increments `attempt`, on the lane #2201's `0127`
migration reopened. The handler restarts at step 1, **including the provider call at step 5**, on
a System the first attempt may already have mutated — and the allocate preconditions still hold
precisely because the commit that would have moved the activation state never applied. For
`cleanup` and `teardown` the re-run is a re-run deletion. Once `attempt >= max_attempts` the row
is permanently `running`, because `repair_abandoned_jobs` is fenced against marked payloads
(`src/kdive/reconciler/repairs/jobs.py:42-49`) and both generic finalizers are fenced
(`0122…sql:304-315`).

Nothing here makes the port calls idempotent or gates re-entry on an observation. Idempotency
under a later authority generation is the adapter's obligation under ADR-0584 — the merged local
adapter keeps per-activation admission watermarks for exactly that
(`src/kdive/providers/local_libvirt/external_boot_authority.py:111`, `:151-154`) — and this change
neither verifies nor relies on it. Observe-driven re-entry is #2202's; the reaping half is
#2203's. This design closes neither, and a test records the behavior so it cannot change
silently.

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

### The execution vehicle, stated before the tests that rest on it

"Against the fault-inject port" is not constructible by simply handing a handler a fresh
`FaultInjectExternalBoot`, and two independent mechanisms make it so. Both are the fixture's job
to solve, and neither weakens ADR-0593 decision 4.

1. **`observe` answers only for a recovery point `prepare` produced.**
   `FaultInjectExternalBoot.observe` is `return self._observations[recovery.recovery_ref.ref]`
   (`src/kdive/providers/fault_inject/lifecycle/external_boot.py:90-94`), and `_observations` is
   written in exactly one place — `prepare` (`:84`). Four of the six operations route through
   `observe`, so a port whose `_observations` is empty raises `KeyError` on all four. Seeding an
   activation row straight into Postgres does not populate it, and neither does a recovery-point
   JSONB shaped like `tests/db/external_boot_authority_support.py:122-131`, whose
   `{schema, binding, plan_identity}` has no `recovery_ref` at all and does not validate as a
   `RecoveryPoint`.
2. **No composed runtime binds that port under an admissible `provider_kind`.** The only runtime
   carrying `FaultInjectExternalBoot` is the fault-inject composition
   (`src/kdive/providers/fault_inject/composition.py:124`), whose kind is
   `ResourceKind.FAULT_INJECT = "fault-inject"` — a value
   `ExternalBootAuthorityMarkerV1.provider_kind` cannot hold and `allocate_external_boot_authority`
   rejects (`0122…sql:369`).

**The fixture therefore does this, and the tests assert against it:**

- Build one `FaultInjectExternalBoot` and drive it through `materialize` then `prepare`
  **out of band**, in the fixture, from a synthetic `ExternalBootPlan`. That is what populates
  `_observations` and yields a real `RecoveryPoint`.
- Write that `RecoveryPoint`'s canonical JSON verbatim into the seeded
  `external_boot_activations.recovery_point`, and the `ExternalBootMaterialization`'s into
  `materialization`, so the row the handler reads back is the object the port already knows.
- Hand the handler that **same instance**, wrapped in a delegating double that raises
  `AssertionError` from `materialize` and `prepare` and forwards the other four. The pin
  ADR-0593 decision 4 asks for is about the **handler**, so the fixture performing those two
  calls before the handler exists is exactly the disposition, not a hole in it.
- Register that wrapped port on a `ProviderRuntime` bound under `ResourceKind.LOCAL_LIBVIRT` in
  a test resolver, and seed the resource and Run with `kind`/`target_kind` `local-libvirt`. This
  is the fault-inject **port** without the fault-inject **kind**, which is what every criterion
  asking for the fault-inject port actually needs.

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
   a marked job, assert `count_claimable_worker_jobs` counts it (which is what #2201's migration
   bought, so it is asserted rather than assumed), claim it with a real `Worker`, run the
   registered handler on the vehicle above, assert the activation reaches its terminal state and
   the job is terminal.
10. **Import closure** (`test_import_closure.py`) — no module under
    `kdive.providers.local_libvirt`, `kdive.providers.remote_libvirt`, or `libvirt`.
11. **Re-execution window** (in `test_lifecycle.py`) — records that a superseded commit leaves
    the `jobs` row `running`, and that a pre-allocation refusal (absent acknowledger) writes no
    `jobs` row at all and leaves it `running` with its lease. Both name #2203 as the owner of the
    reaping half and #2202 as the owner of re-entry, in the test docstrings, so neither reads as
    intended coverage.
12. **Recovery-attempt prerequisite** (in `test_lifecycle.py`) — pins that `recover` and
    `resolve-conflict` reach an applied commit only when the current recovery-attempt row is in
    state `recovering`/`conflict`, which the tests seed directly because nothing in this change
    writes it (#2202 owns the `recovery-attempt` operation).

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
