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
  and reap obligations, immediate domain-reference preflight, and idempotent deletion.
- Public error details contain only `pool` and `volume`. Never include host identity, URI,
  credentials, domain XML, or backing paths.
- Guardrails: focused pytest while iterating; `just lint`, `just type`, and `just test-changed`
  before review; `just ci > .agent/q2168-ci.log 2>&1 < /dev/null` before push.
- Branch: `feat/reap-remote-module-volumes-2168`; base: `main`.

## File map

- `src/kdive/providers/remote_libvirt/reaping/module_volumes.py`: single-host algorithm and async
  multi-host provider adapter.
- `src/kdive/providers/infra/reaping.py`: provider-neutral module-volume reaper port and null port.
- `src/kdive/reconciler/cleanup/provider_resources/module_volume_reaping.py`: durable-obligation
  expansion and lane call.
- `src/kdive/providers/remote_libvirt/composition.py` and
  `src/kdive/providers/assembly/composition.py`: concrete port construction and enablement.
- `src/kdive/reconciler/loop.py` and `src/kdive/processes/reconciler.py`: repair registration,
  configuration, result count, and production binding.
- Mirrored files under `tests/providers/`, `tests/reconciler/`, and `tests/processes/`: behavior,
  composition, and report coverage.

## Task 1 — Single-host ownership and attachment-safe deletion

### Interfaces

Consumes `parse_module_volume_name(name: str) -> ModuleVolumeOwner | None`, `CategorizedError`,
`ErrorCategory`, and libvirt connection/pool/domain duck types. Produces:

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

1. Add the eleven tests with fakes that record refresh, enumeration, retention-read, path lookup,
   and delete order; run the focused command and observe the missing-module failure.
2. Implement protocols and safe domain XML parsing with `defusedxml`; direct file/device paths are
   collected, volume references resolve through their named pool and volume, and any missing or
   malformed required attribute raises infrastructure failure.
3. Implement one complete enumeration, retention filtering, a complete reference/conflict
   preflight, then deletion. Translate libvirt errors with bounded details and count
   `VIR_ERR_NO_STORAGE_VOL` as removed.
4. Run the focused command and expect eleven passing tests. Commit the task.

Acceptance: only whole-name matches can reach `delete`; `retained_owners` is first invoked after
`listAllVolumes` returns; all conflicts are found before the first delete; no volume content API is
called.

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
`RemoteLibvirtModuleVolumeReaper.from_env(secret_registry=...)` consumes the existing
`remote_libvirt_reaper_connections`, calls Task 1 on each reachable host's `storage_pool`, and
bridges each synchronous retention read to the owning asyncio loop.

### Verification

- Mode: focused-test. Contract: worker-thread execution, per-host pool binding, post-enumeration
  async callback, count aggregation, and disabled null behavior. Tests:
  `tests/providers/remote_libvirt/reaping/test_module_volume_fleet.py` and provider-composition
  cases. Red: imports/descriptor field fail. Green:
  `uv run python -m pytest tests/providers/remote_libvirt/reaping/test_module_volume_fleet.py tests/providers/test_composition.py -q`.

### Steps

1. Add protocol/null tests and a fake two-host connection bundle that asserts the callback runs on
   the event-loop thread only after the worker has enumerated each host; observe missing symbols.
2. Implement the provider-neutral key/port and null port.
3. Implement the remote fleet adapter using `asyncio.to_thread` and
   `asyncio.run_coroutine_threadsafe`; convert immutable keys to `ModuleVolumeOwner` inside the
   provider boundary and aggregate per-host removal counts.
4. Add the remote factory to the provider descriptor and expose
   `ProviderComposition.build_reconciler_module_volume_reaper`, returning the null port when remote
   libvirt is disabled.
5. Run the focused command and expect all selected tests to pass. Commit the task.

Acceptance: provider construction opens no connection; one unreachable host does not block later
hosts; a reachable-host operation error propagates; no callback is invoked when no host is reached.

## Task 3 — Durable-obligation lane and reconciler registration

### Interfaces

Consumes `RemoteModuleAttemptObligationRepository.retained_owners`, whose entries contain a
`ModuleAttempt` and independent `mutation_retained`/`reap_retained` flags. Produces:

```python
async def reap_orphaned_module_volumes(
    conn: AsyncConnection,
    reaper: ModuleVolumeReaper,
) -> int: ...
```

The callback expands a retained mutation owner to `source.ext4` and `scratch.ext4`, and a retained
reap owner to `reaping.journal` and `reaped.journal`, rendering UUIDs canonically.

### Verification

- Mode: focused-test. Contract: kind-specific expansion happens only when the provider invokes the
  callback and the returned provider count is preserved. Tests:
  `tests/reconciler/test_module_volume_reaping.py`. Red: lane import fails. Green:
  `uv run python -m pytest tests/reconciler/test_module_volume_reaping.py -q`.
- Mode: focused-test. Contract: repair ordering, failure isolation, report count, production
  binding, and disabled composition. Tests in `tests/reconciler/test_loop.py`,
  `tests/reconciler/test_catalog.py`, and `tests/processes/test_reconciler.py`. Red: expected repair
  kind/count/config field is absent. Green:
  `uv run python -m pytest tests/reconciler/test_loop.py tests/reconciler/test_catalog.py tests/processes/test_reconciler.py -q`.

### Steps

1. Add lane tests for mutation-only, reap-only, both, discharged rows, deferred callback timing,
   count propagation, and repository failure; observe the missing module.
2. Implement the lane with a repository injection seam used only by tests and the concrete
   repository default in production.
3. Add the port to `ReconcileConfig`, register `reaped_module_volumes` beside provider-volume
   lanes, extend `ReconcileReport` and repair counters, and bind the composition factory in the
   reconciler process.
4. Add catalog, report, failure-isolation, and process-construction assertions; run both focused
   commands and expect all selected tests to pass.
5. Run `just lint`, `just type`, and `just test-changed`; expect clean. Commit the task.

Acceptance: the retention query occurs only when the provider calls it after host enumeration;
disabled remote composition is a no-op; an error records the repair name and does not starve later
repairs.

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
