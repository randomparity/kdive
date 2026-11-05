# Stranded activation observation plan

Goal: produce both settlement records for a stranded worker-owned activation (#3574).

Architecture: one unmarked platform job on the fixed worker records its own evidence, then asks the
authority for an unjournaled observation. The authority derives its facts from the database,
proves its lane quiescent, observes its daemon and store, and records absence. Spec:
[stranded activation observation](../specs/2026-11-05-stranded-activation-observation-design.md).

Tech stack: Python 3.14, psycopg, Postgres SQL migrations, pytest.

Expected implementation size: 900–1300 changed lines (L) — the file map below: about 350 lines of source across 21 files plus 500–900 lines of tests.

## Global Constraints

- Migration file `src/kdive/db/schema/0195_stranded_activation_observation.sql`, number 0195, forward-only (ADR-0015).
- No new ADR. No new dependency. `0194` stays untouched.
- `kdive_provider_authority` alone gets the read function; `REVOKE ALL` first, `SET search_path = ''`.
- New shell variables: none.

## File map

| File | Change |
|---|---|
| `src/kdive/db/schema/0195_stranded_activation_observation.sql` | new: job kind, read function |
| `src/kdive/domain/operations/jobs.py` | job kind, platform-internal set |
| `src/kdive/jobs/payloads.py` | payload model and registration |
| `src/kdive/services/external_boot/admission.py` | operation, `preparing` row |
| `src/kdive/services/external_boot/settlement.py` | `request_stranded_observation` |
| `src/kdive/db/external_boot_activations.py` | `record_stranded_worker_ownership` |
| `src/kdive/providers/core/runtime.py` | `DomainPresence`, `domain_presence` field |
| `src/kdive/providers/local_libvirt/composition.py` | set `domain_presence` |
| `src/kdive/providers/local_libvirt/lifecycle/connect.py` | `LibvirtDomainPresence` |
| `src/kdive/jobs/handlers/external_boot/stranded.py` | new handler |
| `src/kdive/jobs/assembly.py` | register handler |
| `src/kdive/providers/external_boot_authority/protocol.py` | closed request and response |
| `src/kdive/providers/external_boot_authority/transport.py` | operation, decode, dispatch |
| `src/kdive/jobs/authority_sender.py`, `src/kdive/providers/ports/authority.py` | sender method |
| `src/kdive/providers/external_boot_authority/stranded.py` | new: repository, facts, digest |
| `src/kdive/providers/external_boot_authority/service.py` | `observe_stranded_activation` |
| `src/kdive/providers/external_boot_authority/host.py` | accepted function, wire repository |
| `src/kdive/providers/local_libvirt/external_boot_authority.py`, `.../lifecycle/boot/external_boot.py`, `.../lifecycle/boot/session.py` | adapter observation |

No ownership move. No obsolete path. Tests live beside their subjects; the four `migrations[-K:]`
window lists take the new row and K+1.

## Task 1: migration

**Interfaces.** Produces SQL `read_stranded_activation_target(uuid, text)`, consumed by Task 3.

**Verification.**

- Contract: read function shape. Mode: focused-test. `tests/db/test_migration_0195_stranded_activation_observation.py`; red: `UndefinedFunction`; green: `uv run python -m pytest tests/db/test_migration_0195_stranded_activation_observation.py -q`.
- Contract: role denial for server, worker, reconciler. Mode: focused-test. Same file; red: no `InsufficientPrivilege` raised; same green command.
- Contract: `jobs_kind_check` accepts the new kind. Mode: focused-test. Same file; red: `CheckViolation`; same green command.

Steps:

1. Write the tests using `_strand` from `tests/db/test_migration_0194_stranded_activation_settlement.py`, moved to a support module when both files need it (`tests/test_test_module_dependencies.py` forbids importing a collected test module). Cases: stranded row returned with NULL journal columns when no head exists; head columns filled when one does; `object_refs` lists a materialized activation's refs; no row for a missing activation, a `prepared` activation, an ownership row, a NULL `domain_name`, a foreign instance.
2. Run them; expect `UndefinedFunction`.
3. Write the migration:

```sql
-- 0195_stranded_activation_observation.sql — #3574, ADR-0816 (2026-11-05).
-- Forward-only (ADR-0015). The observation job and the authority's own read of its target.
ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check;
ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check
    CHECK (kind IN ('provision', 'reprovision', 'teardown', 'build', 'install',
                    'boot', 'force_crash', 'power', 'capture_vmcore', 'image_build',
                    'diagnostics_worker_check', 'build_install_boot', 'authorize_ssh_key',
                    'console_rotate', 'diagnostic_sysrq', 'check_ssh_reachable',
                    'watch_for_crash', 'snapshot', 'restore', 'delete_snapshot',
                    'capture_traffic', 'reclaim_investigation_rootfs',
                    'remote_module_volume_reap', 'resolve_recovery_orphan',
                    'observe_stranded_activation'));

CREATE FUNCTION public.read_stranded_activation_target(
    p_activation_id uuid, p_authority_instance text
) RETURNS TABLE (
    system_id uuid, run_id uuid, domain_name text, object_refs text[],
    journal_sequence bigint, journal_digest text
) LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_activation public.external_boot_activations%ROWTYPE;
    v_domain text;
BEGIN
    IF NOT pg_has_role(session_user, 'kdive_provider_authority', 'member') THEN
        RAISE EXCEPTION 'provider authority is required' USING ERRCODE = '42501';
    END IF;
    SELECT * INTO v_activation FROM public.external_boot_activations WHERE id = p_activation_id;
    IF NOT FOUND OR v_activation.state <> 'preparing' THEN RETURN; END IF;
    IF NOT EXISTS (SELECT 1 FROM public.external_boot_authorities AS authority
                   WHERE authority.activation_id = p_activation_id
                     AND authority.authority_instance = p_authority_instance)
       AND NOT EXISTS (SELECT 1 FROM public.jobs AS job
                       WHERE job.kind = 'boot'
                         AND job.payload #>> '{external_boot_authority_v1,activation_id}'
                             = p_activation_id::text
                         AND job.payload #>> '{external_boot_authority_v1,authority_instance}'
                             = p_authority_instance) THEN
        RETURN;
    END IF;
    IF EXISTS (SELECT 1 FROM public.authority_system_ownership AS owner
               WHERE owner.system_id = v_activation.system_id) THEN
        RETURN;
    END IF;
    SELECT system.domain_name INTO v_domain FROM public.systems AS system
    WHERE system.id = v_activation.system_id;
    IF v_domain IS NULL THEN RETURN; END IF;
    RETURN QUERY
    SELECT v_activation.system_id, v_activation.run_id, v_domain,
           ARRAY(SELECT ref FROM unnest(ARRAY[
               v_activation.materialization #>> '{artifacts,kernel,ref}',
               v_activation.materialization #>> '{artifacts,modules,ref}',
               v_activation.materialization #>> '{artifacts,initrd,ref}',
               v_activation.materialization #>> '{artifacts,debuginfo,ref}',
               v_activation.recovery_point #>> '{recovery_ref,ref}',
               v_activation.pre_recovery_evidence #>> '{recovery_object,ref}'
           ]) AS ref WHERE ref IS NOT NULL),
           head.sequence, head.digest
    FROM (SELECT 1) AS one
    LEFT JOIN public.external_boot_authority_journal_heads AS head
      ON head.authority_instance = p_authority_instance
     AND head.system_id = v_activation.system_id;
END $$;

REVOKE ALL ON FUNCTION public.read_stranded_activation_target(uuid, text)
FROM PUBLIC, kdive_server, kdive_worker, kdive_reconciler, kdive_lifecycle_witness,
     kdive_provider_authority;
GRANT EXECUTE ON FUNCTION public.read_stranded_activation_target(uuid, text)
    TO kdive_provider_authority;
```

4. Add the row and K+1 to the four window lists.
5. Run the focused commands; commit `feat(db): add stranded observation job and target read`.

## Task 2: wire contract

**Interfaces.** Produces `AuthorityStrandedObservationRequestV1(activation_id: UUID)`,
`AuthorityStrandedObservationResponseV1(disposition, objects: tuple[str, ...])`,
transport operation `observe-stranded-activation`,
`sender.observe_stranded_activation(request, *, deadline: float) -> AuthorityStrandedObservationResponseV1`.

**Verification.**

- Contract: closed models. Mode: focused-test. `tests/providers/external_boot_authority/test_protocol.py`; red: `ImportError`; green: `uv run python -m pytest tests/providers/external_boot_authority/test_protocol.py -q`. Cases: extra field refused; `objects` with `recorded` refused; `objects_present` without objects refused.
- Contract: dispatch. Mode: focused-test. `tests/providers/external_boot_authority/test_transport.py`; red: `invalid-request`; green: `uv run python -m pytest tests/providers/external_boot_authority/test_transport.py -q`. Cases: routed; a service without the method answers `provider-not-configured`; a mismatched request model is refused at encode.

Steps:

1. Write tests; run them; expect the red above.
2. Add to `protocol.py`:

```python
class AuthorityStrandedObservationRequestV1(_ClosedValue):
    """The activation to observe; the authority derives every other fact itself (ADR-0816)."""

    schema_: Literal["external-boot-authority-stranded-observation-v1"] = Field(
        "external-boot-authority-stranded-observation-v1", alias="schema"
    )
    activation_id: UUID


class AuthorityStrandedObservationResponseV1(_ClosedValue):
    """A recorded absence, or the closed reason none was recorded."""

    schema_: Literal["external-boot-authority-stranded-observation-response-v1"] = Field(
        "external-boot-authority-stranded-observation-response-v1", alias="schema"
    )
    disposition: Literal["recorded", "domain_present", "objects_present"]
    objects: Annotated[tuple[str, ...], Field(max_length=32)] = ()

    @model_validator(mode="after")
    def _objects_name_the_refusal(self) -> Self:
        if bool(self.objects) != (self.disposition == "objects_present"):
            raise ValueError("objects must be named exactly when the authority holds them")
        return self
```

3. In `transport.py`: add the literal, a `_decode_operation_request` branch using `model_validate_json`, the type checks in `encode_request_envelope` and `_decode_envelope`, the allowed-operations set, an `AuthorityStrandedObservationService` runtime-checkable protocol, and a dispatch branch answering `provider-not-configured` for a service without it.
4. Add the sender method to `authority_sender.py` in the shape of `resolve_recovery_orphan` and to the port in `providers/ports/authority.py`.
5. Run the focused commands; commit `feat(authority): add the stranded observation wire contract`.

## Task 3: authority observation

**Interfaces.** Consumes Tasks 1 and 2. Produces in `stranded.py`:
`StrandedTarget(system_id, run_id, domain_name, object_refs, journal_sequence, journal_digest)`,
`StrandedAuthorityFacts(domain_absent, store_absent, recovery_ref)`,
`StrandedObservationRepository.read_target(activation_id) -> StrandedTarget | None`,
`StrandedObservationRepository.record_absence(activation_id, target, composite_state) -> bool`,
`stranded_composite_state(activation_id, target) -> str`. Produces
`LocalExternalBootAuthorityAdapter.observe_stranded_absence(activation_id, target) -> StrandedAuthorityFacts`.

**Verification.**

- Contract: quiescence and dispositions. Mode: focused-test. `tests/providers/external_boot_authority/test_stranded_service.py`; red: `AttributeError`; green: `uv run python -m pytest tests/providers/external_boot_authority/test_stranded_service.py -q`. Cases: no repository; no peer; missing target; failed flag while another caller holds the lane; active operation; file tail differs from head; head with no file; no head with no file records; a non-terminal head records; domain present and objects present record nothing; a recorder refusal raises `superseded`; the lane lock is held across the provider read and the record.
- Contract: digest. Mode: focused-test. Same file; two targets differing in one object differ in digest, and the value matches `^sha256:[0-9a-f]{64}$`.
- Contract: local adapter. Mode: focused-test. `tests/providers/local_libvirt/test_external_boot_authority.py`; red: `AttributeError`; green: `uv run python -m pytest tests/providers/local_libvirt/test_external_boot_authority.py -q`. Cases: domain missing and store empty; domain defined; store holding a partial directory; a libvirt fault raises rather than reading as absent.
- Contract: readiness. Mode: focused-test. The existing `check_database_role` real-role test; red: `unexpected` grant failure; green: `uv run python -m pytest tests/providers/external_boot_authority/test_host.py -q -k database_role`.

Steps:

1. Write the tests; run them; expect the red above.
2. Write `stranded.py` with frozen dataclasses, the repository calling `SELECT * FROM public.read_stranded_activation_target(%s,%s)` and `SELECT public.record_stranded_activation_authority_absence(%s,%s,%s,%s,%s,%s,%s)`, and:

```python
def stranded_composite_state(activation_id: UUID, target: StrandedTarget) -> str:
    """Name what the authority observed as absent; settlement copies it to terminal evidence."""
    document = json.dumps(
        {
            "activation_id": str(activation_id),
            "domain_name": target.domain_name,
            "journal_digest": target.journal_digest,
            "journal_sequence": target.journal_sequence,
            "objects": sorted(target.object_refs),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(b"kdive-stranded-absence-v1\0" + document).hexdigest()
```

3. Add to `ExternalBootAuthorityService`: constructor keyword `stranded_observations: StrandedObservationRepository | None = None`, and:

```python
    async def observe_stranded_activation(
        self, peer: AuthenticatedPeer | None, request: AuthorityStrandedObservationRequestV1
    ) -> AuthorityStrandedObservationResponseV1:
        """Prove this lane quiescent, read its own daemon and store, and record absence."""
        if not self._accepting:
            raise AuthorityServiceError("superseded")
        repository = self._stranded_observations
        if (
            peer is None
            or repository is None
            or not isinstance(self._adapter, AuthorityStrandedObserver)
        ):
            raise AuthorityServiceError("provider_conflict")
        probe = await repository.read_target(request.activation_id)
        if probe is None:
            raise AuthorityServiceError("superseded")
        lane = self._lane(probe.system_id)

        async def run() -> AuthorityStrandedObservationResponseV1:
            try:
                async with lane.lock:
                    return await self._observe_stranded_locked(request, repository, lane, probe)
            finally:
                self._release_lane(probe.system_id, lane)

        task = asyncio.create_task(run())
        self._track_completion(task)
        return await asyncio.shield(task)
```

`_observe_stranded_locked` follows the spec's steps 2 to 6. Add `stranded_lane_failed` and `stranded_head_mismatch` to `JournalConflictReason`, and log each refusal as `_resolve_recovery_orphan` does. For `objects_present` return `objects=(facts.recovery_ref, *target.object_refs)`. Its tail check compares `records[-1].sequence` and `record_digest(records[-1])` with the target's journal columns, and treats no records with both columns NULL as equal.

4. Local adapter: make `_offload`'s request parameter `... | None` and skip the lease scope for `None`; add `observe_stranded_absence`, calling `LocalLibvirtExternalBoot.stranded_facts(binding, domain_name)`, which calls `RealLocalExternalBootIO.stranded_facts`:

```python
    def stranded_facts(
        self, binding: ExternalBootActivationBinding, domain_name: str
    ) -> StrandedAuthorityFacts:
        with RecoveryMetadataStore(self._recovery_root) as store:
            store_absent = store.exact_recovery_absence(binding)
        return StrandedAuthorityFacts(
            domain_absent=not self._session_factory.domain_is_defined(domain_name),
            store_absent=store_absent,
            recovery_ref=_recovery_ref(binding).ref,
        )
```

Add `LocalExternalBootSessionFactory.domain_is_defined(name)`, using `self._connect()` and treating `VIR_ERR_NO_DOMAIN` as false.

5. `host.py`: add `'public.read_stranded_activation_target(uuid,text)'::regprocedure` to `accepted_function`, and pass `stranded_observations=StrandedObservationRepository(connections, config.authority_instance)` in the local branch of `_build_mutation_service` only.
6. Run the focused commands; commit `feat(authority): observe a stranded activation`.

## Task 4: job kind, worker evidence and handler

**Interfaces.** Consumes Task 2's `sender.observe_stranded_activation`. Produces
`JobKind.OBSERVE_STRANDED_ACTIVATION`,
`ObserveStrandedActivationPayload(system_id: str, activation_id: str)` (schema `observe-stranded-activation-v1`),
`ExternalBootActivationRepository.record_stranded_worker_ownership(conn, *, credential_hash: bytes, job_id: UUID, job_attempt: int, activation_id: UUID, domain_name: str) -> CasStatus`,
`class DomainPresence(Protocol): def is_defined(self, domain_name: str) -> bool`,
`ProviderRuntime.domain_presence: DomainPresence | None = None`.

**Verification.**

- Contract: local presence. Mode: focused-test. `tests/providers/local_libvirt/test_domain_presence.py`; red: `ImportError`; green: `uv run python -m pytest tests/providers/local_libvirt/test_domain_presence.py -q`. Cases: found, `VIR_ERR_NO_DOMAIN` false, other libvirt fault raises, connection closed each time.
- Contract: handler branches. Mode: focused-test. `tests/jobs/handlers/external_boot/test_stranded.py`; red: `ImportError`; green: `uv run python -m pytest tests/jobs/handlers/external_boot/test_stranded.py -q`. Cases: each refusal reason in the spec's handler list, success.

Steps:

1. Write tests; run; expect `ImportError`.
   Add `OBSERVE_STRANDED_ACTIVATION = "observe_stranded_activation"` to `JobKind` and to `PLATFORM_INTERNAL_JOB_KINDS`, add the kind to `tests/domain/test_models.py`, and add this payload beside `RemoteModuleVolumeReapPayload`, registering it in `ActivePayloadModel`, the `_ActivePayloadModel` union and `_ACTIVE_PAYLOAD_MODELS`:

```python
class ObserveStrandedActivationPayload(_PayloadBase):
    """One stranded activation to observe (ADR-0816). It carries no authority marker."""

    schema_: Literal["observe-stranded-activation-v1"] = Field(alias="schema")
    system_id: str
    activation_id: str
```

2. Add the repository method beside `is_settled`:

```python
    async def record_stranded_worker_ownership(
        self,
        conn: AsyncConnection,
        *,
        credential_hash: bytes,
        job_id: UUID,
        job_attempt: int,
        activation_id: UUID,
        domain_name: str,
    ) -> CasStatus:
        """State that this worker's own daemon holds the System's domain (ADR-0816)."""
        row = await (
            await conn.execute(
                "SELECT public.record_stranded_activation_worker_ownership(%s,%s,%s,%s,%s)",
                (credential_hash, job_id, job_attempt, activation_id, domain_name),
            )
        ).fetchone()
        if row is None:
            raise RuntimeError("worker ownership record returned no status")
        return CasStatus(str(row[0]))
```

3. Add `DomainPresence` to `providers/core/runtime.py` and the field after `authority`. Add `LibvirtDomainPresence(connect)` to `lifecycle/connect.py`, opening and closing one connection per call, and set `domain_presence=LibvirtDomainPresence(lambda: libvirt.open(config.require(LIBVIRT_URI)))` in the local `ProviderRuntime`.
4. Write `src/kdive/jobs/handlers/external_boot/stranded.py`:

```python
"""Produce both owners' evidence for a stranded activation (ADR-0816, #3574)."""

_AUTHORITY_DEADLINE_SECONDS = 60.0
_ACTIVATIONS = ExternalBootActivationRepository()


def _refuse(reason: str, message: str) -> CategorizedError:
    return CategorizedError(
        message, category=ErrorCategory.CONFLICT, details={"reason": reason}, terminal=True
    )


async def observe_stranded_activation_handler(
    conn: AsyncConnection,
    job: Job,
    *,
    resolver: ProviderResolver | None,
    incarnation_credential: Callable[[], SecretStr],
) -> str:
    """Record this worker's evidence, then ask the authority to record its own."""
    payload = load_payload(job, ObserveStrandedActivationPayload)
    system_id, activation_id = UUID(payload.system_id), UUID(payload.activation_id)
    if resolver is None:
        raise _unsupported()
    binding = await resolver.binding_for_system(conn, system_id)
    presence = binding.runtime.domain_presence
    if presence is None:
        raise _unsupported()
    capability = binding.runtime.authority
    if capability is None or capability.sender is None:
        raise CategorizedError(
            "stranded observation authority route is not configured",
            category=ErrorCategory.CONFIGURATION_ERROR,
            details={
                "reason": "authority_route_missing"
                if capability is None
                else capability.missing_route_reason
            },
            terminal=True,
        )
    system = await SYSTEMS.get(conn, system_id)
    domain_name = None if system is None else system.domain_name
    if domain_name is None or not await asyncio.to_thread(presence.is_defined, domain_name):
        raise _refuse(
            "worker_domain_absent",
            f"this worker's daemon holds no domain for System {system_id}; "
            "the activation is not the worker-owned stranded shape",
        )
    credential = incarnation_credential().get_secret_value().encode()
    async with conn.transaction():
        status = await _ACTIVATIONS.record_stranded_worker_ownership(
            conn,
            credential_hash=hashlib.sha256(credential).digest(),
            job_id=job.id,
            job_attempt=job.attempt,
            activation_id=activation_id,
            domain_name=domain_name,
        )
    if status is not CasStatus.APPLIED:
        raise _refuse(
            "worker_evidence_superseded",
            f"activation {activation_id} no longer accepts worker evidence: {status.value}",
        )
    response = await capability.sender.observe_stranded_activation(
        AuthorityStrandedObservationRequestV1(activation_id=activation_id),
        deadline=asyncio.get_running_loop().time() + _AUTHORITY_DEADLINE_SECONDS,
    )
    if response.disposition == "domain_present":
        raise _refuse(
            "authority_domain_present",
            f"the authority's daemon holds a domain for System {system_id}, so the activation "
            "is not settled; end that domain on the authority before observing again",
        )
    if response.disposition == "objects_present":
        raise _refuse(
            "authority_objects_present",
            f"the authority's store is not proven absent for activation {activation_id}: "
            f"{', '.join(response.objects)}; inspect these references on the authority host, "
            "settlement stays refused until they are gone",
        )
    return json.dumps({"activation_id": str(activation_id), "disposition": "recorded"})
```

with `_unsupported()` returning a terminal `CONFIGURATION_ERROR` naming `stranded_observation_unsupported`, and `register_handlers(registry, *, resolver, incarnation_credential)`. Register it in `assembly.py` beside `orphan.register_handlers`, passing `lambda: assembly.incarnation_credential`.

5. Add a security-definer entry for `external_boot_activation_settlements` to `tests/jobs/worker_write_baseline.json` in the shape of `teardown.remote-module-attempt-obligations.update`, list `tests/jobs/handlers/external_boot/test_stranded.py` in `tests/jobs/handlers/worker_role_inventory.json`, and approve `public.record_stranded_activation_worker_ownership(bytea,uuid,integer,uuid,text)` in `test_worker_role_inventory.py`.
6. Run the focused commands plus `uv run python -m pytest tests/jobs/test_assembly.py tests/domain/test_models.py tests/jobs/handlers/test_worker_role_inventory.py -q`; commit `feat(jobs): add the stranded observation handler`.

## Task 5: admission, enqueue and read

**Interfaces.** Consumes Task 4. Produces `ExternalBootOperation.OBSERVE_STRANDED_ACTIVATION` and
`async def request_stranded_observation(conn, ctx, *, activation_id: UUID) -> Job`, and `async def read_stranded_observation(conn, ctx, *, activation_id: UUID) -> StrandedObservationOutcome | None`, which #3551 calls.

**Verification.**

- Contract: matrix row. Mode: focused-test. `tests/services/external_boot/test_admission.py`; red: `preparing` denies; green: `uv run python -m pytest tests/services/external_boot/test_admission.py -q`.
- Contract: service refusals and enqueue. Mode: focused-test. `tests/services/external_boot/test_stranded_observation.py`; red: `ImportError`; green: `uv run python -m pytest tests/services/external_boot/test_stranded_observation.py -q`.

Steps:

1. Write tests: role denial before any database read; each of `not_found`, `authority_owned`, `not_preparing`, with no job row; a second request returns the same job while it is live; a request after a failed job requeues it; the read returns `None` before a request, is role-gated, and returns a refused job's `reason` and `message`.
2. Exempt the new operation from `set(GUARDED_TOOLS.values()) == set(ExternalBootOperation)` in `tests/services/external_boot/test_admission.py` with the reason that it has no tool until #3551, add its `_ADMITTING_STATES` row, and add both new functions to `_UNENVELOPED_PLATFORM_ROLE` in `tests/guards/test_require_role_membership_guard.py`. Add the operation and `ExternalBootActivationState.PREPARING: _ALWAYS_ADMITTED | {ExternalBootOperation.OBSERVE_STRANDED_ACTIVATION}`.
3. Implement in `settlement.py`:

```python
async def request_stranded_observation(
    conn: AsyncConnection, ctx: RequestContext, *, activation_id: UUID
) -> Job:
    """Enqueue the observation that produces both owners' settlement evidence (ADR-0816)."""
    require_platform_role(ctx, PlatformRole.PLATFORM_ADMIN)
    activation = await _REPOSITORY.get(conn, activation_id)
    if activation is None:
        raise _observation_refused(activation_id, "not_found", ErrorCategory.NOT_FOUND)
    system_id = activation.system_id
    async with conn.transaction(), advisory_xact_lock(conn, LockScope.SYSTEM, system_id):
        system = await SYSTEMS.get(conn, system_id)
        if system is None:
            raise _observation_refused(activation_id, "not_found", ErrorCategory.NOT_FOUND)
        if await authority_system_binding(conn, system_id) is not None:
            raise _observation_refused(activation_id, "authority_owned")
        activation = await _REPOSITORY.get(conn, activation_id)
        if activation is None or activation.state is not ExternalBootActivationState.PREPARING:
            raise _observation_refused(activation_id, "not_preparing")
        await check_external_boot_admission(
            conn,
            system_id,
            ExternalBootOperation.OBSERVE_STRANDED_ACTIVATION,
            project=system.project,
        )
        return await queue.enqueue(
            conn,
            JobKind.OBSERVE_STRANDED_ACTIVATION,
            ObserveStrandedActivationPayload(
                schema="observe-stranded-activation-v1",
                system_id=str(system_id),
                activation_id=str(activation_id),
            ),
            Authorizing(
                principal=ctx.principal, agent_session=ctx.agent_session, project=system.project
            ),
            f"stranded-observation:{activation_id}",
            recycle=JobRecyclePolicy.TERMINAL,
        )


def _observation_refused(
    activation_id: UUID, reason: str, category: ErrorCategory = ErrorCategory.CONFLICT
) -> CategorizedError:
    return CategorizedError(
        f"stranded activation {activation_id} cannot be observed: {reason}",
        category=category,
        details={"reason": reason},
        terminal=True,
    )
```

4. Add `StrandedObservationOutcome` (frozen dataclass: `job_id`, `state`, `reason`, `message`) and `read_stranded_observation`, requiring `platform_admin` and selecting the job by `dedup_key = f"stranded-observation:{activation_id}"`; `reason` and `message` come from `failure_context` keys `failure_detail_reason` and `failure_message`.
5. Run the focused commands; commit `feat(external-boot): admit the stranded observation`.

## Task 6: end to end

**Interfaces.** Consumes every task above.

**Verification.**

- Contract: both producers reach settlement. Mode: focused-test. `tests/services/external_boot/test_stranded_observation.py`; red: the test fails until its arm's expectation is written against the wired producers; it is coverage over completed work; green: `uv run python -m pytest tests/services/external_boot/test_stranded_observation.py -q`.
- Contract: generated job-kind surfaces. Mode: focused-test. `just docs-check` and `just cli-verbs-check`; red: drift reported; green: both exit 0.

Steps:

1. Extend the test file. Drive `request_stranded_observation` as `kdive_server`, claim the job as `kdive_worker` with a real incarnation, run the handler with a fake `DomainPresence`, and connect its sender to a real `ExternalBootAuthorityService` holding a `kdive_provider_authority` connection, a temporary journal and a fake adapter. Then call `settle_stranded_activation` and assert `applied`. Add the arms: owned System refused at each layer; authority domain present leaves `authority_evidence_missing`; worker domain absent leaves an empty settlement row and `authority_evidence_missing`; an activation filling all six object paths refuses while held, reads back through `read_stranded_observation`, and settles once the fake reports the store absent.
2. Regenerate with `just docs` and `just cli-verbs`.
3. Commit `test(external-boot): drive stranded observation to settlement`.

Rollback: revert the branch before the migration deploys. After 0195 deploys, roll forward: the grant fails a reverted authority host's readiness, and a row of the new kind breaks a reverted reader.

## Deferrals

- Removing held objects: no owner yet; reported to the campaign with a 400–600 line estimate.
- Remote-libvirt producers: no owner yet; reported to the campaign.
