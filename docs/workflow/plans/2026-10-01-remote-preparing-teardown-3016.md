# Remote-libvirt preparing teardown without PREP evidence (#3016) — plan

Goal: `systems.teardown` admits a remote-libvirt `preparing` activation with no retained PREP
receipt. Architecture: one guard in `build_external_boot_payload`; no worker, authority-host or
schema change; tests prove the admission, the handler path and the MCP tool. Spec:
[design](../specs/2026-10-01-remote-preparing-teardown-3016-design.md).

Expected implementation size: 150–230 changed lines (M) — a 6-line guard, about 70 lines of
admission tests, about 60 lines of handler test and helper parameters, and about 50 lines of
MCP test.

## Global Constraints

- Python 3.14, `uv`; psycopg 3. No new dependency. No migration. No new ADR number.
- ADR-0620 is append-only. Its 2026-10-01 #3016 amendment is already on the branch.
- File scope: `src/kdive/jobs/handlers/external_boot/admission.py`, `tests/jobs/handlers/`,
  `tests/mcp/`, and this design set.
- Gates: `just lint`, `just type`, `just test-verbose <paths>`, `just records` (after
  `git fetch origin main`), and pre-push `just ci > <file> 2>&1 < /dev/null`.

## Task 1 — admit the receipt-less preparing teardown

Files: modify `src/kdive/jobs/handlers/external_boot/admission.py`; test
`tests/jobs/handlers/external_boot/test_admission.py`.

Interfaces: consumes `RemoteModuleAttemptObligationRepository.read_reap_preparation(conn,
system_id, run_id) -> ModuleAttemptPreparationRequestV1 | None`, which raises
`ModuleAttemptObligationError` for two or more rows (`db/remote_module_attempt_obligations.py`).
Produces no new name.

Verification:

- Contract: a remote-libvirt `preparing` teardown with no receipt is admitted, and the payload
  carries no receipt. Mode: focused-test. Test
  `test_remote_preparing_teardown_prep_receipt[none]`. Red before step 3: `CategorizedError`
  "no retained PREP evidence". Green command:
  `just test-verbose tests/jobs/handlers/external_boot/test_admission.py`.
- Contract: one receipt is carried, and two refuse as ambiguous. Mode: focused-test. Cases
  `[one]` and `[two]`. Both pass before and after step 3, which shows the read is unchanged.
  Same command.
- Contract: a non-`preparing` teardown with no receipt still refuses. Mode: focused-test. Case
  `[recovery-failed]`. Same command. Release without a receipt keeps its existing test,
  `test_remote_lifecycle_payload_carries_exact_retained_prep_receipt[missing]`.

Steps:

1. Append this test to `test_admission.py`:

```python
@pytest.mark.parametrize(
    ("activation_state", "receipts", "refusal"),
    [
        ("preparing", 0, None),
        ("preparing", 1, None),
        ("preparing", 2, "PREP evidence is ambiguous"),
        ("recovery_failed", 0, "no retained PREP evidence"),
    ],
    ids=["none", "one", "two", "recovery-failed"],
)
def test_remote_preparing_teardown_prep_receipt(
    migrated_url: str, activation_state: str, receipts: int, refusal: str | None
) -> None:
    """#3016: only a preparing activation's teardown may lack the PREP receipt."""

    async def body(conn: AsyncConnection, vehicle: Vehicle) -> None:
        preparing = activation_state == "preparing"
        await seed_case(
            conn,
            vehicle,
            purpose="teardown",
            activation_state=activation_state,
            attempt_state="recovering" if preparing else "failed",
            with_materialization=not preparing,
            with_recovery_point=not preparing,
        )
        await conn.execute(
            "UPDATE resources SET kind='remote-libvirt' WHERE id=("
            "SELECT a.resource_id FROM systems s JOIN allocations a ON a.id=s.allocation_id "
            "WHERE s.id=%s)",
            (vehicle.system_id,),
        )
        await conn.execute(
            "UPDATE runs SET target_kind='remote-libvirt' WHERE id=%s", (vehicle.run_id,)
        )
        repository = RemoteModuleAttemptObligationRepository()
        for nonce in ("1" * 32, "2" * 32)[:receipts]:
            attempt = ModuleAttempt(vehicle.system_id, vehicle.run_id, nonce)
            await repository.open_mutation_obligation(conn, attempt)
            await repository.record_terminal_evidence(conn, attempt, _evidence(attempt))
            await repository.open_reap_obligation(conn, attempt)
        local = resolver_for(vehicle).resolve(ResourceKind.LOCAL_LIBVIRT)

        async def build() -> tuple[JobKind, BootPayload | TeardownPayload]:
            return await build_external_boot_payload(
                conn,
                activation_id=vehicle.activation_id,
                purpose="teardown",
                operation="teardown",
                provider_kind="remote-libvirt",
                authority_instance=AUTHORITY_INSTANCE,
                operation_identity="teardown-remote",
                resolver=ProviderResolver({ResourceKind.REMOTE_LIBVIRT: local}),
            )

        if refusal is not None:
            with pytest.raises(CategorizedError, match=refusal):
                await build()
            return
        kind, payload = await build()
        assert kind is JobKind.TEARDOWN
        assert isinstance(payload, TeardownPayload)
        if receipts == 0:
            assert payload.remote_module_attempt_v1 is None
        else:
            assert payload.remote_module_attempt_v1 is not None
            assert payload.remote_module_attempt_v1.module_attempt_obligation.operation_nonce == (
                "1" * 32
            )

    _drive(migrated_url, body)
```

2. Run `just test-verbose tests/jobs/handlers/external_boot/test_admission.py`. Expect
   `[none]` to fail with "no retained PREP evidence" and every other case to pass. If `[two]`
   fails because `open_mutation_obligation` refuses a second live attempt for the same System,
   open, record and reap the first attempt's obligation and then discharge its mutation
   obligation with `discharge_mutation_obligation(conn, attempt, reason=...)`, using a reason
   from `_DISCHARGE_REASONS`, before opening the second. Record the change in the commit
   message.
3. In `admission.py`, replace

```python
        if remote_module_attempt is None:
            raise _refuse("remote module lifecycle has no retained PREP evidence")
```

with

```python
        # A preparing activation's teardown may predate its module attempt. Nothing on the
        # teardown path reads the receipt; the authority host reaps or quarantines module
        # volumes from its own records (ADR-0620, #3016 amendment).
        if remote_module_attempt is None and not (
            purpose == "teardown" and activation.state.value == "preparing"
        ):
            raise _refuse("remote module lifecycle has no retained PREP evidence")
```

4. Re-run the step 2 command. Expect all cases to pass.
5. Run `just lint` and `just type` (expect exit 0), then commit
   `fix(external-boot): admit a remote preparing teardown without PREP (#3016)`.

## Task 2 — the handler tears down without a receipt or module lifecycle

Files: test `tests/jobs/handlers/external_boot/test_prepared_before_admission.py`. No source
change.

Interfaces: consumes `_dispatch(dsns, conn, case, operation, vehicle)` from the same file. It
gains two keyword-only parameters, `resolver: ProviderResolver | None = None` (default
`resolver_for(vehicle)`) and `marker: dict[str, Any] | None = None` (default `case.marker`).
`_job(case)` gains `marker: dict[str, Any] | None = None` with the same default. It also uses
`AuthorityCapability(authority_instance=..., modules=...)` from
`kdive.providers.ports.authority`, and the module attribute
`kdive.jobs.handlers.external_boot.lifecycle.execute_remote_module_lifecycle_on_authority_host`.

Verification:

- Contract: a remote-libvirt teardown of a `preparing` activation whose payload has no
  `remote_module_attempt_v1` ends `torn_down` and ends the reservation exactly once, without
  the worker-side module lifecycle. Mode: focused-test. Test
  `test_remote_teardown_of_a_preparing_activation_needs_no_prep_receipt[pending|ready]`. It
  passes on main because no source change is needed. Controlled fault: insert
  `await execute_remote_module_lifecycle_on_authority_host()` as the first statement of
  `complete` in `teardown_handler`. Expect red with "must not run the worker module lifecycle",
  which also proves the patched attribute is the one the handler module calls. Revert it with `git checkout -- src/kdive/jobs/handlers/external_boot/lifecycle.py`
  only after Task 1 is committed. Green command:
  `just test-verbose tests/jobs/handlers/external_boot/test_prepared_before_admission.py`.

Steps:

1. Change `_job` and `_dispatch` so that the marker and resolver can be overridden:

```python
def _job(case: SeededCase, marker: dict[str, Any] | None = None) -> Job:
    kind = JobKind.TEARDOWN if case.purpose == "teardown" else JobKind.BOOT
    key = "system_id" if kind is JobKind.TEARDOWN else "run_id"
    value = case.vehicle.system_id if kind is JobKind.TEARDOWN else case.vehicle.run_id
    payload = {key: str(value), "external_boot_authority_v1": marker or case.marker}
    return build_job(kind, payload).model_copy(update={"id": case.job_id, "attempt": case.attempt})
```

   In `_dispatch`, add `*, resolver: ProviderResolver | None = None, marker: dict[str, Any] |
   None = None` after `vehicle`. Pass `resolver=resolver or resolver_for(vehicle)` to
   `ExternalBootHandlerPorts`. Replace the handler call with
   `await handler(worker, _job(case, marker), ExternalBootAuthorityMarkerV1.model_validate(marker or case.marker))`.
2. Append this test:

```python
@pytest.mark.parametrize(("reservation", "releases"), [("pending", 0), ("ready", 1)])
def test_remote_teardown_of_a_preparing_activation_needs_no_prep_receipt(
    migrated_url: str,
    authority_role_dsns: Callable[[str], str],
    monkeypatch: pytest.MonkeyPatch,
    reservation: str,
    releases: int,
) -> None:
    """#3016: a remote teardown reads no PREP receipt and runs no worker module lifecycle."""

    async def forbidden(**_values: object) -> None:
        raise AssertionError("System teardown must not run the worker module lifecycle")

    monkeypatch.setattr(
        "kdive.jobs.handlers.external_boot.lifecycle."
        "execute_remote_module_lifecycle_on_authority_host",
        forbidden,
    )

    async def body(seed: AsyncConnection) -> None:
        vehicle = build_vehicle()
        marker_overrides = {"provider_kind": "remote-libvirt"}
        case = await seed_case(
            seed,
            vehicle,
            purpose="teardown",
            operation="teardown",
            activation_state="preparing",
            with_materialization=False,
            with_recovery_point=False,
            with_reservation=reservation == "ready",
            marker_overrides=marker_overrides,
        )
        await seed.execute(
            "UPDATE resources SET kind='remote-libvirt' WHERE id=("
            "SELECT a.resource_id FROM systems s JOIN allocations a ON a.id=s.allocation_id "
            "WHERE s.id=%s)",
            (vehicle.system_id,),
        )
        await seed.execute(
            "UPDATE runs SET target_kind='remote-libvirt' WHERE id=%s", (vehicle.run_id,)
        )
        local = resolver_for(vehicle).resolve(ResourceKind.LOCAL_LIBVIRT)
        remote = replace(
            local,
            authority=AuthorityCapability(
                authority_instance=case.marker["authority_instance"], modules=cast(Any, object())
            ),
        )

        await _dispatch(
            authority_role_dsns,
            seed,
            case,
            "teardown",
            vehicle,
            resolver=ProviderResolver({ResourceKind.REMOTE_LIBVIRT: remote}),
            marker=case.marker | marker_overrides,
        )

        async with seed.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT a.state, s.state AS system_state, "
                "(SELECT count(*) FROM external_boot_reservations r "
                " WHERE r.activation_id = a.id) AS reservations, "
                "(SELECT count(*) FROM external_boot_reservation_releases r "
                " WHERE r.activation_id = a.id) AS releases "
                "FROM external_boot_activations a JOIN systems s ON s.id = a.system_id "
                "WHERE a.id = %s",
                (vehicle.activation_id,),
            )
            row = await cur.fetchone()
        assert row == {
            "state": "torn_down",
            "system_state": "torn_down",
            "reservations": 0,
            "releases": releases,
        }

    _drive(migrated_url, body)
```

   Add the imports `from dataclasses import replace`, `from typing import cast`,
   `from kdive.domain.catalog.resources import ResourceKind`,
   `from kdive.providers.core.resolver import ProviderResolver`, and
   `from kdive.providers.ports.authority import AuthorityCapability`.
3. Run the green command. Expect both cases to pass. If the runner refuses the binding because
   its `authority_instance` differs from the marker's, record the exact refusal and set the
   capability's `authority_instance` to the value the runner compares against.
4. Apply the controlled fault, run the green command, and expect red. Revert the fault and
   expect green again.
5. Run `just lint` and `just type` (expect exit 0), then commit
   `test(external-boot): prove remote preparing teardown needs no PREP (#3016)`.

## Task 3 — `systems.teardown` queues the remote teardown

Files: test `tests/mcp/lifecycle/test_systems_tools.py`.

Interfaces: `_seed_retired_teardown_authority(conn, seeded, *, purpose="recover",
current=False)` gains `provider_kind: str = "local-libvirt"`, which replaces the literal
`'local-libvirt'` in its `INSERT` with a bound parameter. It also uses the existing helpers
`granted_allocation`, `_seed_teardown_system`, `_seed_run`, `seed_activation`, `_teardown`,
`provider_resolver`, `ExternalBootOperations` and `ProviderResolver`, all already imported or
defined in the file.

Verification:

- Contract: `systems.teardown` on a remote-libvirt System whose newest activation is
  `preparing` with no receipt returns `queued`, and the teardown job carries the remote marker
  and no receipt. Mode: focused-test. Test
  `test_remote_teardown_of_preparing_activation_needs_no_prep_receipt`. Red before Task 1:
  status `failure` with reason `external_boot_teardown_authority_unresolved`. Show it by
  running the test with `git stash` of Task 1's `admission.py` hunk, or with Task 1 temporarily
  reverted through `git revert --no-commit <task-1-sha>` followed by `git revert --abort`.
  Green command: `just test-verbose tests/mcp/lifecycle/test_systems_tools.py -k preparing`.

Steps:

1. Parameterize `_seed_retired_teardown_authority` with `provider_kind`, as described above.
2. Append:

```python
def test_remote_teardown_of_preparing_activation_needs_no_prep_receipt(
    migrated_url: str,
) -> None:
    """#3016: a remote-libvirt preparing activation with no PREP receipt is torn down."""

    async def _run() -> None:
        async with systems_support.pool(migrated_url) as pool:
            alloc_id = await granted_allocation(pool)
            system_id = await _seed_teardown_system(pool, alloc_id, SystemState.READY)
            run_id = await _seed_run(pool, system_id, RunState.RUNNING)
            async with pool.connection() as conn:
                seeded = await seed_activation(
                    conn,
                    state=ExternalBootActivationState.PREPARING,
                    system_id=UUID(system_id),
                    run_id=UUID(run_id),
                )
                await conn.execute(
                    "INSERT INTO external_boot_reservations "
                    "(activation_id, store_identity, owner_key, reserved_bytes, state) "
                    "VALUES (%s, 'stores/main', %s, 4096, 'pending')",
                    (seeded.activation.id, f"owners/{seeded.activation.id}"),
                )
                await conn.execute(
                    "UPDATE resources SET kind = 'remote-libvirt' "
                    "WHERE id = (SELECT resource_id FROM allocations WHERE id = %s)",
                    (alloc_id,),
                )
                await conn.execute(
                    "UPDATE runs SET target_kind = 'remote-libvirt' WHERE id = %s", (run_id,)
                )
                await _seed_retired_teardown_authority(
                    conn, seeded, purpose="activate", current=True, provider_kind="remote-libvirt"
                )
            local = provider_resolver(external_boot=ExternalBootOperations()).resolve(
                ResourceKind.LOCAL_LIBVIRT
            )
            response = await _teardown(
                pool,
                ctx(Role.ADMIN),
                system_id,
                resolver=ProviderResolver({ResourceKind.REMOTE_LIBVIRT: local}),
            )
            assert response.status == "queued", response.model_dump()
            async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT kind, payload FROM jobs WHERE id = %s", (response.object_id,)
                )
                job = await cur.fetchone()

        assert job is not None and job["kind"] == "teardown"
        marker = job["payload"]["external_boot_authority_v1"]
        assert (marker["provider_kind"], marker["purpose"]) == ("remote-libvirt", "teardown")
        assert job["payload"].get("remote_module_attempt_v1") is None

    asyncio.run(_run())
```

3. Run the green command. Expect the new test and the existing #2961 test to pass. If
   `_teardown` refuses for a reason other than the PREP check (for example a profile keyed to
   `local-libvirt`), record the exact refusal and adjust only the seed rows to match a
   remote-libvirt System. Never adjust production code in this task.
4. Show red against the pre-Task-1 `admission.py`, as described above, then restore it.
5. Run `just lint` and `just type` (expect exit 0), then commit
   `test(mcp): systems.teardown queues a remote preparing teardown (#3016)`.

## Task 4 — records and live proof

No code. Run `git fetch origin main && just records` (expect exit 0). Then run the remote-libvirt
live tier, if a lab host can run it. Read the lab-host notes first; it is a teardown of a System
whose `runs.boot` activate job was canceled before preparation. Otherwise state in the PR that
only the DB-backed and handler arms ran. Mode: task-test-not-applicable for the live arm: it
needs a provisioned remote-libvirt authority host, and Tasks 1–3 already cover the code
contracts.

## Requirement map

| Spec success line | Task |
|---|---|
| receipt-less preparing teardown admitted; one carried; two ambiguous | 1 |
| non-preparing teardown and release still refuse | 1 (and existing release test) |
| handler ends `torn_down`, reservation once, no module lifecycle | 2 |
| `systems.teardown` returns `queued` with no receipt | 3 |
| ADR amendment, records gate | design set, 4 |
