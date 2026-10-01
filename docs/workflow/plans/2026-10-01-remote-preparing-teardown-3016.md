# Remote-libvirt preparing teardown without PREP evidence (#3016) — plan

Goal: `systems.teardown` admits a remote-libvirt `preparing` activation with no retained PREP
receipt. Architecture: one guard in `build_external_boot_payload`; no worker, authority-host or
schema change; tests prove the admission, the handler path and the MCP tool. Spec:
[design](../specs/2026-10-01-remote-preparing-teardown-3016-design.md).

Expected implementation size: 120–190 changed lines (M) — a 6-line guard, about 70 lines of
admission tests, about 50 lines of handler helper and parametrization, and about 30 lines of
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
   cannot open a second attempt, discharge the first one's mutation obligation
   (`discharge_mutation_obligation(conn, attempt, reason=...)`) before opening the second.
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

Interfaces: `_job(case)` and `_dispatch(dsns, conn, case, operation, vehicle)` in that file gain
keywords `marker: dict[str, Any] | None = None` (default `case.marker`) and
`extra: dict[str, Any] | None = None` (merged into the job payload); `_dispatch` also gains
`resolver: ProviderResolver | None = None` (default `resolver_for(vehicle)`). Uses
`RemoteModuleAttemptObligationRepository` and `ModuleAttempt` as in Task 1, and
`AuthorityCapability(authority_instance, geometry=None, sender=None, modules=None)` from
`kdive.providers.ports.authority`.

Verification:

- Contract: a remote-libvirt teardown of a `preparing` activation ends `torn_down` and ends the
  reservation exactly once, both without a PREP receipt and with a retained one carried.
  Mode: focused-test. Test
  `test_teardown_of_a_preparing_activation_skips_preparation[remote*-*]`. It passes on
  main because no source change is needed. Controlled fault (the regression the
  spec rules out): insert `await _execute(context)` as the first statement of `complete` in
  `teardown_handler`; expect red (refused, activation not `torn_down`).
  Revert it with
  `git checkout -- src/kdive/jobs/handlers/external_boot/lifecycle.py` only after Task 1 is
  committed. Green command:
  `just test-verbose tests/jobs/handlers/external_boot/test_prepared_before_admission.py`.

Steps:

1. Let `_job` and `_dispatch` take the override:

```python
def _job(
    case: SeededCase, marker: dict[str, Any] | None = None, extra: dict[str, Any] | None = None
) -> Job:
    kind = JobKind.TEARDOWN if case.purpose == "teardown" else JobKind.BOOT
    key = "system_id" if kind is JobKind.TEARDOWN else "run_id"
    value = case.vehicle.system_id if kind is JobKind.TEARDOWN else case.vehicle.run_id
    payload = {key: str(value), "external_boot_authority_v1": marker or case.marker}
    payload |= extra or {}
    return build_job(kind, payload).model_copy(update={"id": case.job_id, "attempt": case.attempt})
```

   In `_dispatch`, add `*, resolver: ProviderResolver | None = None, marker: dict[str, Any] |
   None = None, extra: dict[str, Any] | None = None` after `vehicle`; pass
   `resolver=resolver or resolver_for(vehicle)` to `ExternalBootHandlerPorts`; call
   `handler(worker, _job(case, marker, extra), ExternalBootAuthorityMarkerV1.model_validate(marker or case.marker))`.
2. Add a `provider` parameter, `["local", "remote", "remote-receipt"]`, to
   `test_teardown_of_a_preparing_activation_skips_preparation`. Set
   `provider_kind = "local-libvirt" if provider == "local" else "remote-libvirt"`, pass
   `marker_overrides={"provider_kind": provider_kind}` to `seed_case`, and replace the
   `_dispatch` call with:

```python
resolver = resolver_for(vehicle)
if provider_kind == "remote-libvirt":
    await seed.execute(
        "UPDATE resources SET kind='remote-libvirt' WHERE id=("
        "SELECT a.resource_id FROM systems s JOIN allocations a ON a.id=s.allocation_id "
        "WHERE s.id=%s)",
        (vehicle.system_id,),
    )
    await seed.execute(
        "UPDATE runs SET target_kind='remote-libvirt' WHERE id=%s", (vehicle.run_id,)
    )
    local = resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    # A module capability makes the worker module lifecycle reachable if teardown ran it.
    modules = AuthorityCapability(
        authority_instance=case.marker["authority_instance"], modules=cast(Any, object())
    )
    resolver = ProviderResolver({ResourceKind.REMOTE_LIBVIRT: replace(local, authority=modules)})
extra = None
if provider == "remote-receipt":
    attempt = ModuleAttempt(vehicle.system_id, vehicle.run_id, "1" * 32)
    repository = RemoteModuleAttemptObligationRepository()
    await repository.open_mutation_obligation(seed, attempt)
    await repository.record_terminal_evidence(seed, attempt, _evidence(attempt))
    await repository.open_reap_obligation(seed, attempt)
    receipt = await repository.read_reap_preparation(seed, vehicle.system_id, vehicle.run_id)
    assert receipt is not None
    extra = {"remote_module_attempt_v1": receipt.model_dump(mode="json", by_alias=True)}
marker = case.marker | {"provider_kind": provider_kind}
await _dispatch(
    authority_role_dsns,
    seed,
    case,
    "teardown",
    vehicle,
    resolver=resolver,
    marker=marker,
    extra=extra,
)
```

   Add the imports `from dataclasses import replace`, `from typing import cast`,
   `from kdive.domain.catalog.resources import ResourceKind`,
   `from kdive.providers.core.resolver import ProviderResolver`,
   `from kdive.providers.ports.authority import AuthorityCapability`,
   `from kdive.db.remote_module_attempt_obligations import ModuleAttempt,
   RemoteModuleAttemptObligationRepository`, and
   `from tests.db.remote_module_attempt_obligations_support import _evidence`. Add "#3016: remote-libvirt
   reads no PREP receipt" to the docstring. The existing row assertion (`torn_down`, mode,
   reservations 0, releases 0 or 1) then covers both providers unchanged.
3. Run the green command; expect six cases to pass. A runner refusal of the remote binding is
   fixed in the test binding only.
4. Apply the controlled fault, expect red, revert, expect green.
5. `just lint`, `just type` (exit 0); commit
   `test(external-boot): prove remote preparing teardown needs no PREP (#3016)`.

## Task 3 — `systems.teardown` queues the remote teardown

Files: test `tests/mcp/lifecycle/test_systems_tools.py`.

Interfaces: `_seed_retired_teardown_authority(conn, seeded, *, purpose="recover",
current=False)` gains `provider_kind: str = "local-libvirt"`, bound in place of the literal
`'local-libvirt'` in its `INSERT`.

Verification:

- Contract: `systems.teardown` on a remote-libvirt System whose newest activation is
  `preparing` with no receipt returns `queued`; the teardown job carries the remote marker and
  no receipt. Mode: focused-test. Test
  `test_teardown_of_preparing_activation_enqueues_authority_marker[remote-libvirt]`. Red with
  `git show <task-1-sha>~1:<admission.py> > <admission.py>` (restore with
  `git checkout -- <admission.py>`): status `failure`, reason
  `external_boot_teardown_authority_unresolved`. Green command:
  `just test-verbose tests/mcp/lifecycle/test_systems_tools.py -k preparing`.

Steps:

1. Parameterize `_seed_retired_teardown_authority` with `provider_kind`.
2. Parametrize `test_teardown_of_preparing_activation_enqueues_authority_marker` with
   `provider_kind` in `["local-libvirt", "remote-libvirt"]`. After the reservation insert:

```python
resolver = provider_resolver(external_boot=ExternalBootOperations())
if provider_kind == "remote-libvirt":
    await conn.execute(
        "UPDATE resources SET kind = 'remote-libvirt' "
        "WHERE id = (SELECT resource_id FROM allocations WHERE id = %s)",
        (alloc_id,),
    )
    await conn.execute("UPDATE runs SET target_kind = 'remote-libvirt' WHERE id = %s", (run_id,))
    local = resolver.resolve(ResourceKind.LOCAL_LIBVIRT)
    resolver = ProviderResolver({ResourceKind.REMOTE_LIBVIRT: local})
```

   Pass `provider_kind=provider_kind` to `_seed_retired_teardown_authority` and
   `resolver=resolver` to `_teardown`. Append the assertions:

```python
        assert marker["provider_kind"] == provider_kind
        # #3016: a preparing activation that never opened its module reap carries no receipt.
        assert job["payload"].get("remote_module_attempt_v1") is None
```

3. Run the green command; expect both cases to pass. A non-PREP refusal of the remote case is
   fixed in the seed rows only, never in production code.
4. Show red against the pre-Task-1 `admission.py`, then restore it.
5. `just lint`, `just type` (exit 0); commit
   `test(mcp): systems.teardown queues a remote preparing teardown (#3016)`.

## Task 4 — records and live proof

No code. `git fetch origin main && just records` (exit 0). Then the remote-libvirt live tier if
a lab host can run it: tear down a System whose `runs.boot` activate job was canceled before
preparation. Otherwise the PR states that only the DB-backed and handler arms ran. Mode:
task-test-not-applicable for the live arm: it needs a provisioned remote-libvirt authority
host, and Tasks 1–3 cover the code contracts.

## Requirement map

| Spec success line | Task |
|---|---|
| receipt-less preparing teardown admitted; one carried; two ambiguous | 1 |
| non-preparing teardown and release still refuse | 1 (and the existing release test) |
| handler ends `torn_down`, reservation once, with and without a receipt | 2 |
| `systems.teardown` returns `queued` with no receipt | 3 |
| ADR amendment, records gate | design set, 4 |
