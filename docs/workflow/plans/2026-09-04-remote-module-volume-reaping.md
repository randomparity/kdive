# Remote module volume reaping implementation plan

Goal: safely reclaim discharged remote module volumes through the reconciler. The provider owns
single-host libvirt classification/deletion and an async fleet adapter; the reconciler owns the
Postgres retention callback and repair reporting. Python 3.14, `uv`, psycopg, and libvirt are the
existing stack.

Expected implementation size: 700–1,050 changed lines (L) — derived from three source modules,
composition/report wiring, and provider, fleet, lane, and loop tests.

## Global Constraints

- Python 3.14; no new dependency; 100-character lines; Ruff `E,F,I,UP,B,SIM`; whole-tree `ty`.
- The host is x86_64. Project targets are x86_64 and ppc64le; native ppc64le live testing is
  excluded from this campaign run.
- ADR-0588 governs whole-name ownership, enumerate-before-retained-read ordering, distinct mutation
  and reap obligations, immediate domain-reference preflight, and idempotent deletion. ADR-0603 and
  ADR-0604 govern typed remote identity and its Resource-bound authority transport. #2170 supplies
  the completion-owned preparation executor used for blocking provider work.
- Public error details contain only `pool` and `volume`. Never include host identity, URI,
  credentials, domain XML, or backing paths.
- A single host sweep admits at most 4,096 distinct normalized paths across candidates, direct
  disk-graph references, and named-volume canonical paths. Deduplicate before identity lookup;
  discovery of path 4,097 fails with redacted `INFRASTRUCTURE_FAILURE` before any deletion.
- Guardrails: focused pytest while iterating; `just lint`, `just type`, and `just test-changed`
  before review; `just ci > .agent/q2168-ci.log 2>&1 < /dev/null` before push.
- Branch: `feat/reap-remote-module-volumes-2168`; base: `main`.

## File map

- `src/kdive/providers/remote_libvirt/lifecycle/rootfs/remote_module_attachments.py`: expose the
  landed complete disk-graph reference traversal without changing its behavior.
- `src/kdive/providers/remote_libvirt/reaping/module_volumes.py`: single-host algorithm and async
  multi-host provider adapter consuming that traversal.
- `src/kdive/providers/infra/reaping.py`: provider-neutral module-volume reaper port and null port.
- `src/kdive/reconciler/cleanup/provider_resources/module_volume_reaping.py`: durable-obligation
  expansion and worker handler; the reconciler-facing lane only enqueues the durable job.
- `src/kdive/domain/operations/jobs.py`, `src/kdive/jobs/payloads.py`, `src/kdive/jobs/assembly.py`,
  and `src/kdive/db/schema/0131_remote_module_volume_reap_job_kind.sql`: closed job contract,
  persisted enum, and worker registration.
- `src/kdive/providers/remote_libvirt/composition.py` and
  `src/kdive/providers/assembly/composition.py`: worker-only concrete port construction through
  `build_worker_module_volume_reaper`; remove the reconciler-named builder.
- `src/kdive/jobs/handlers/module_volume_reaping.py` and `src/kdive/jobs/assembly.py`: worker handler
  and `WorkerHandlerAssembly.module_volume_reaper` registration.
- `src/kdive/reconciler/loop.py`: enqueue-lane registration and enqueue-count reporting; reconciler
  process/configuration receives no provider reaper or authority sender.
- Mirrored files under `tests/providers/`, `tests/reconciler/`, and `tests/processes/`: behavior,
  composition, and report coverage.

## Task 1 — Single-host ownership and attachment-safe deletion

### Interfaces

Consumes `parse_module_volume_name(name: str) -> ModuleVolumeOwner | None`, `CategorizedError`,
`ErrorCategory`, libvirt connection/pool/domain duck types, and the landed #2167 traversal promoted
to these provider-package interfaces:

```python
def volume_references(root: Element) -> list[tuple[str, str]]: ...
def path_references(root: Element) -> set[str]: ...
```

Both walk every `source` descendant of each disk; `path_references` additionally reads active and
legacy mirror `file`/`dev` attributes. The existing attachment inspector remains their first
consumer. This task produces:

```python
class ModuleVolumeReaperConn(Protocol):
    def storagePoolLookupByName(self, name: str) -> _Pool: ...
    def listAllDomains(self, flags: int = 0) -> list[_Domain]: ...


def list_owned_module_volumes(
    conn: ModuleVolumeReaperConn, pool_name: str
) -> list[tuple[str, ModuleVolumeOwner]]: ...


def referenced_volume_paths(conn: ModuleVolumeReaperConn) -> frozenset[str]: ...


def reap_orphaned_module_volumes(
    conn: ModuleVolumeReaperConn,
    pool_name: str,
    identity_port: RemoteDeviceIdentityPort,
    *,
    retained_owners: Callable[[], Collection[ModuleVolumeOwner]],
) -> int: ...
```

### Verification

- Mode: focused-test. Contract: full-name ownership, retention ordering, domain-reference
  protection, fail-closed resolution, and idempotent deletion. Tests: the eleven cases named by
  issue #2168 in `tests/providers/remote_libvirt/reaping/test_module_volumes.py`. Red: import fails
  before the module exists. Green:
  `uv run python -m pytest tests/providers/remote_libvirt/reaping/test_module_volumes.py -q`.

### Steps

1. Rename #2167's landed `_volume_references` and `_path_references` helpers as public
   provider-package interfaces, update its internal calls, and retain its complete disk-graph tests.
2. Add the eleven named reaper tests plus mixed-pool, active/inactive lexical-alias, nested backing
   store, data store, and active/legacy mirror regressions, with fakes that record refresh,
   enumeration, retention-read, path lookup, and delete order; run the focused command and observe
   the missing-module failure.
3. Implement protocols and parse each active and inactive definition with #2167's bounded parser;
   feed every document through the shared reference helpers. Resolve direct file/device paths,
   candidate paths, and named-volume canonical paths through the supplied ADR-0603 identity port.
   Missing or malformed identity fails closed; timeout and operational resolution errors raise
   infrastructure failure. Normalize and deduplicate candidate, direct-reference, and named-volume
   canonical paths before identity lookup. Admit exactly 4,096 distinct paths across the complete
   host preflight; on discovering a 4,097th, raise redacted `INFRASTRUCTURE_FAILURE` before the
   first delete and without exposing a path.
   The destructive function receives the port as an explicit positional dependency; no connection
   object, helper, or closure constructs or captures authority implicitly.
4. Add boundary regressions proving 4,096 distinct normalized paths are admitted, a 4,097th fails
   before identity lookup or deletion with no path in the error, and normalized lexical aliases
   consume one identity lookup. Use a delete spy so every over-limit path proves no delete begins.
5. Implement one complete enumeration, retention filtering, a complete reference/conflict
   preflight, then deletion. Add a fake-port regression that records candidate and domain-reference
   lookups, plus absent and malformed identity cases proving no delete begins. Translate libvirt
   errors with bounded details and count `VIR_ERR_NO_STORAGE_VOL` as removed.
6. Run the focused command plus #2167's attachment tests and require every named and added
   regression to pass. Commit the task.

Acceptance: only whole-name matches can reach `delete`; `retained_owners` is first invoked after
`listAllVolumes` returns; the complete protected set exists before the first delete; attached
candidates are warned and skipped without starving independent candidates; direct-path lexical and
managed aliases cannot bypass protection; no volume content API is called.
At most 4,096 distinct normalized paths reach the identity port in one host sweep, aliases are
deduplicated before lookup, and discovering one additional distinct path leaves every candidate
undeleted.

## Task 2 — Async fleet port and provider composition

### Interfaces

Produces in `providers.infra.reaping`:

```python
class ModuleVolumeKey(NamedTuple):
    system_id: str
    run_id: str
    operation_nonce: str
    kind: str


class ModuleVolumeReaper(Protocol):
    async def reap_module_volumes(
        self,
        retained_owners: Callable[[], Awaitable[Collection[ModuleVolumeKey]]],
    ) -> int: ...
```

`NullModuleVolumeReaper` returns zero without calling the callback.
`RemoteLibvirtModuleVolumeReaper.from_env(secret_registry=..., authority_sender_factory=...)`
consumes the existing `remote_libvirt_reaper_connections`, #2170's
`RemoteModulePreparationExecutor`, and each configuration's immutable authority binding. It builds
the same typed Resource-bound sender and ADR-0603 identity adapter used by preparation, calls Task 1
on each reachable host's `storage_pool`, and bridges each synchronous retention read to the owning
asyncio loop. One bounded deadline covers each host sweep.

### Verification

- Mode: focused-test. Contract: worker-thread execution, per-host pool binding, post-enumeration
  async callback, cancellation draining, count aggregation, and disabled null behavior. Tests:
  `tests/providers/remote_libvirt/reaping/test_module_volume_fleet.py` and provider-composition
  cases. Red: imports/descriptor field fail. Green:
  `uv run python -m pytest tests/providers/remote_libvirt/reaping/test_module_volume_fleet.py tests/providers/test_composition.py -q`.

### Steps

1. Add protocol/null tests and a fake two-host connection bundle that asserts the callback runs on
   the event-loop thread only after the worker has enumerated each host; observe missing symbols.
2. Implement the provider-neutral key/port and null port.
3. Implement the remote fleet adapter with `asyncio.run_coroutine_threadsafe` for the retained-owner
   callback and #2170's completion-owned executor for each blocking host operation; convert
   immutable keys to `ModuleVolumeOwner` inside the provider boundary and aggregate per-host removal
   counts. Build the host's typed sender and ADR-0603 identity adapter from its fixed authority
   binding. Add a controlled test that cancels twice during the drain and proves the adapter remains
   pending until both callback and worker finish, after which it raises cancellation with the
   cancellation count preserved. Reuse the executor's existing shutdown and capacity regressions;
   do not duplicate its cancellation loop.
4. Pass the per-host identity port explicitly into `reap_orphaned_module_volumes`; add a composition
   assertion that the port built from that host's fixed binding is the one observed by candidate and
   reference lookups.
5. Add the remote factory to the provider descriptor and expose the worker-owned
   `ProviderComposition.build_worker_module_volume_reaper`, returning the null port when remote
   libvirt is disabled. Task 3 binds it only from worker assembly.
6. Run the focused command and expect all selected tests to pass. Commit the task.

Acceptance: provider construction opens no connection or authority credential; one unreachable host
does not block later hosts; a reachable-host operation or missing authority fails closed; no
callback is invoked when no host is reached; cancellation does not outlive or abandon the worker
thread or retained-owner future; caller-selected destinations and local identity fallback remain
impossible.

## Task 3 — Durable worker job and reconciler registration

### Interfaces

Consumes `RemoteModuleAttemptObligationRepository.retained_owners`, whose entries contain a
`ModuleAttempt` and independent `mutation_retained`/`reap_retained` flags. Migration `0131` adds
`remote_module_volume_reap` to the persisted job enum. The closed payload is:

```python
class RemoteModuleVolumeReapPayload(_PayloadBase):
    schema: Literal["remote-module-volume-reap-v1"]
```

The reconciler enqueues it under stable key `remote-module-volume-reap:v1`, with synthetic
principal/project `remote-libvirt`, terminal recycling, and the ordinary bounded worker-attempt
contract. Because tenant project names are not reserved, classify the job kind in a shared
platform-internal set rather than relying on that synthetic project for isolation. Every tenant
`jobs.list`, point read/wait, and cancel path denies the kind before project-role evaluation,
including a caller with a colliding `remote-libvirt` project; the existing platform-operator
`ops.jobs_list` path remains unchanged and can observe it. No Resource, endpoint, credential,
path, or owner enters the payload. The worker handler
constructs the concrete reaper from `WorkerHandlerAssembly`, whose active incarnation credential
is borrowed only by the typed sender, and calls it with a retained-owner callback over the claimed
job's database connection. The callback expands mutation retention to `source.ext4` and
`scratch.ext4`, and reap retention to `reaping.journal` and `reaped.journal`, rendering UUIDs
canonically.

Exact ownership surfaces:

```python
async def enqueue_remote_module_volume_reap(conn: AsyncConnection) -> bool: ...

async def remote_module_volume_reap_handler(
    conn: AsyncConnection, job: Job, *, reaper: ModuleVolumeReaper
) -> None: ...

class WorkerHandlerAssembly:
    module_volume_reaper: ModuleVolumeReaper

def ProviderComposition.build_worker_module_volume_reaper(
    self,
    *,
    enable_remote_libvirt: bool | None = None,
    authority_sender_factory: Callable[[RemoteAuthorityBinding], AuthorityRequestSender]
    | None = None,
) -> ModuleVolumeReaper: ...
```

The worker builder replaces `build_reconciler_module_volume_reaper`; delete the old method and its
reconciler-oriented tests. `build_worker_handler_assembly` constructs the reaper with its existing
active-incarnation sender factory. Neither `ReconcileConfig` nor `build_reconcile_config` gains a
reaper or sender parameter.

### Verification

- Mode: focused-test. Contract: kind-specific expansion happens only when the provider invokes the
  callback; the handler preserves fixed Resource authority construction and exposes no destination
  input. Tests:
  `tests/reconciler/test_module_volume_reaping.py`. Red: lane import fails. Green:
  `uv run python -m pytest tests/reconciler/test_module_volume_reaping.py -q`.
- Mode: focused-test. Contract: repair ordering, failure isolation, report count, production
  binding, bounded payload, in-flight deduplication, terminal recycling, retry/restart behavior, and
  disabled composition. Tests span `tests/jobs/`, `tests/reconciler/`, and worker assembly. Green:
  run those changed test paths with `just test-verbose`.

### Steps

1. Add migration `0131`, `JobKind.REMOTE_MODULE_VOLUME_REAP`, and the exact closed payload model;
   prove missing/extra/wrong-schema fields fail before handler dispatch.
2. Add queue admission tests for one stable key: queued/running rows deduplicate, terminal rows
   recycle, concurrent admissions yield one active row, failed work retries within the bounded
   worker attempt contract, and an expired lease is reclaimed after worker restart.
3. Classify `REMOTE_MODULE_VOLUME_REAP` as platform-internal in the job domain and consume that
   classification in all tenant list/read/wait/cancel paths before project authorization. Prove a
   tenant whose project is literally `remote-libvirt` cannot list, read, wait for, or cancel the
   maintenance job; prove an ordinary job in that same project remains accessible; and prove the
   existing platform-admin queue view still lists the internal job.
4. Convert the reconciler lane to
   `enqueue_remote_module_volume_reap(conn: AsyncConnection) -> bool`, enqueueing the constant
   payload. Register
   `module_volume_reap_jobs_enqueued` in the catalog/report and return one only for insertion or
   terminal recycling; queue failure is isolated like other repairs. Assert the report never
   claims a removed count or a later provider failure.
5. Implement `remote_module_volume_reap_handler(conn, job, *, reaper) -> None`. Validate the
   payload, use the reaper supplied by worker assembly, and
   give it the deferred repository callback. Prove mutation/reap expansion, post-enumeration read,
   aggregate fleet isolation, null composition, and that no payload field can select Resource,
   endpoint, or credential. A provider error remains a worker-job retry or terminal failure; a
   success emits the aggregate removed count only through bounded worker telemetry.
6. Add `module_volume_reaper` to `WorkerHandlerAssembly`, register the handler, rename composition
   to `build_worker_module_volume_reaper`, and delete `build_reconciler_module_volume_reaper`.
   Construct the remote adapter only in `build_worker_handler_assembly`, borrowing the active
   incarnation credential through the landed typed sender factory. Assert worker assembly receives
   the reaper and reconciler assembly exposes no reaper or sender construction.
7. Run focused job, handler, reconciler, payload, migration, and assembly tests, then `just lint`,
   `just type`, and `just test-changed`; expect clean. Commit the task.

Acceptance: the retention query occurs only when the worker's provider calls it after host
enumeration; the queue carries no authority selector; one stable row bounds in-flight work;
terminal, retry, and restart paths converge; disabled remote composition is a no-op; and an enqueue
error records the repair name without starving later repairs.

## Task 4 — Final verification and rollback check

### Verification

- Mode: focused-test. Contract: the combined provider and reconciler paths satisfy all issue
  criteria. Green: rerun every Task 1–3 focused command.
- Mode: focused-test. Contract: repository-wide behavior and generated records remain valid.
  Green: `just ci > .agent/q2168-ci.log 2>&1 < /dev/null` with exit 0.

### Steps

1. Re-read `git diff main...HEAD` for ownership widening, error-detail leaks, duplicate name
   parsing, thread/event-loop deadlock risk, and unrelated changes; remove any such change.
2. Run the focused commands, `just lint`, `just type`, and `just test-changed`; expect exit 0.
3. Run the full CI recipe with blocking stream redirection; expect exit 0 and retain its log under
   ignored `.agent/`.
4. Rollback is `git revert` of this PR: the default null port and catalog removal leave existing
   volumes untouched. Do not manually delete test or operator storage during rollback.
