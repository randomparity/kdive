"""Connected acceptance proofs for the local external-boot authority path."""

from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import psycopg
import pytest
from pydantic import SecretStr

import kdive.config as runtime_config
from kdive.jobs.authority_sender import AuthorityRequestSender
from kdive.providers.assembly import composition as provider_composition
from kdive.providers.external_boot_authority import host, transport
from kdive.providers.external_boot_authority.host import AuthorityHostConfig
from kdive.providers.external_boot_authority.local_client import (
    LocalAuthorityBinding,
    _AuthorityUnixTransport,
)
from kdive.providers.external_boot_authority.network_client import _resolve_tls_material
from kdive.providers.external_boot_authority.protocol import (
    GENESIS_DIGEST,
    AuthorityMutationRequestV1,
    AuthorityTakeoverRequestV1,
    JournalPhase,
    JournalRecordV1,
    canonical_record_bytes,
)
from kdive.providers.external_boot_authority.service import AuthenticatedPeer, AuthorityServiceError
from kdive.providers.local_libvirt import composition as local_composition
from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    LocalObservedState,
    LocalRecoveryMetadataV1,
    RealLocalExternalBootIO,
    RecoveryPhase,
)
from kdive.providers.local_libvirt.settings import (
    LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES,
    LIBVIRT_RECOVERY_ROOT,
)
from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    ExternalBootPreparationRequest,
    KernelIdentity,
    MaterializedArtifacts,
    OpaqueProviderRef,
    PresentComponentState,
    ProviderStateIdentity,
    RecoveryPoint,
    RunningKernelObservation,
)
from kdive.security.secrets.secret_registry import SecretRegistry
from kdive.security.secrets.secrets import FileRefBackend
from tests.db.external_boot_authority_support import _allocate, _RoleDsns, _seed_case
from tests.providers.contract.plans import ACTIVATION_ID, sample_plan_data
from tests.providers.external_boot_authority.tls_support import _tls_material

_SOURCE = "sha256:" + "1" * 64
_TARGET = "sha256:" + "2" * 64
_MODULES = "sha256:" + "3" * 64
_SOURCE_XML = "<domain><name>kdive</name><os><kernel>old</kernel></os></domain>"


class _HostBoundary:
    """One controllable host-I/O boundary beneath the real coordinator."""

    def __init__(self) -> None:
        self.metadata: LocalRecoveryMetadataV1 | None = None
        self.actions: list[str] = []

    def open(self) -> AbstractContextManager[_HostBoundary]:
        return _HostContext(self)

    def materialize(self, plan: ExternalBootPlan) -> ExternalBootMaterialization:
        self.actions.append("materialize")
        identity = KernelIdentity(
            architecture=plan.architecture,
            release=plan.module_obligation.release,
            gnu_build_id="01020304",
        )
        return ExternalBootMaterialization(
            architecture=plan.architecture,
            provider_kind="local-libvirt",
            ownership={"system_id": plan.ownership.system_id, "run_id": plan.ownership.run_id},
            plan_identity=plan.identity,
            extracted_vmlinuz_sha256=plan.bundle.vmlinuz_sha256,
            source_module_manifest=plan.module_obligation.source_manifest,
            installed_module_tree=_MODULES,
            verified_bundle_sha256=plan.bundle.sha256,
            verified_initrd_sha256=None,
            kernel_observation=identity,
            artifacts=MaterializedArtifacts(
                kernel=OpaqueProviderRef(ref="artifacts/kernel"),
                modules=OpaqueProviderRef(ref="artifacts/modules"),
                initrd=None,
            ),
        )

    def prepare(
        self, materialization: ExternalBootMaterialization, binding: ExternalBootActivationBinding
    ) -> LocalRecoveryMetadataV1:
        self.actions.append("prepare")
        target_xml = _SOURCE_XML.replace("old", "new")
        self.metadata = LocalRecoveryMetadataV1(
            binding=binding,
            plan_identity=materialization.plan_identity,
            materialization_identity=materialization.identity,
            release=materialization.kernel_observation.release,
            materialized_modules=materialization.artifacts.modules,
            materialized_modules_sha256=_MODULES,
            materialized_modules_bytes=1,
            source_xml_sha256="sha256:" + hashlib.sha256(_SOURCE_XML.encode()).hexdigest(),
            source_xml=_SOURCE_XML,
            source_definition=_SOURCE,
            source_boot=_SOURCE,
            target_boot=_TARGET,
            target_projection_sha256=_TARGET,
            target_xml_sha256="sha256:" + hashlib.sha256(target_xml.encode()).hexdigest(),
            target_xml=target_xml,
            expected_running=materialization.kernel_observation,
            source_state=ProviderStateIdentity(definition=_SOURCE, modules=AbsentComponentState()),
            target_state=ProviderStateIdentity(
                definition=_TARGET, modules=PresentComponentState(manifest=_MODULES)
            ),
            prior_power="running",
            capture={"state": "absent"},
            phase="pre-stop-intent",
        )
        return self.metadata

    def recovery_ref(self, binding: ExternalBootActivationBinding) -> OpaqueProviderRef:
        return OpaqueProviderRef(
            ref=f"local-recovery-v1/{binding.system_id}/{binding.activation_id}"
        )

    def reopen(self, _recovery: RecoveryPoint) -> LocalRecoveryMetadataV1:
        return self._metadata()

    def reopen_binding(self, binding: ExternalBootActivationBinding) -> LocalRecoveryMetadataV1:
        metadata = self._metadata()
        if metadata.binding != binding:
            raise ValueError("unexpected activation binding")
        return metadata

    def observe_state(self, metadata: LocalRecoveryMetadataV1) -> LocalObservedState:
        if metadata.phase == "target-defined":
            return LocalObservedState(_TARGET, metadata.target_state.modules, True)
        return LocalObservedState(_SOURCE, metadata.source_state.modules, False)

    def activate_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("activate-modules")
        self.record_phase(metadata, "module-restored")

    def define_target(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("define-target")
        self.record_phase(metadata, "target-defined")

    def observe_running(self, metadata: LocalRecoveryMetadataV1) -> RunningKernelObservation:
        self.actions.append("observe")
        return RunningKernelObservation(
            identity=metadata.expected_running,
            cmdline=b"root=UUID=x",
            expected_cmdline=b"root=UUID=x",
        )

    def recover_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("recover-modules")
        self.record_phase(metadata, "module-restored")

    def define_source(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("define-source")
        self.record_phase(metadata, "source-restored")

    def restore_power(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("restore-power")
        self.record_phase(metadata, "recovered")

    def record_phase(
        self, metadata: LocalRecoveryMetadataV1, phase: RecoveryPhase
    ) -> LocalRecoveryMetadataV1:
        self.metadata = metadata.model_copy(update={"phase": phase})
        return self.metadata

    def cleanup_complete(self, _recovery: RecoveryPoint) -> bool:
        return self._metadata().phase == "cleaned"

    def cleanup(self, metadata: LocalRecoveryMetadataV1, _point_digest: str) -> None:
        self.actions.append("cleanup")
        self.record_phase(metadata, "cleaned")

    def prime(self, binding: ExternalBootActivationBinding, plan_identity: str) -> None:
        materialization = self.materialize(_plan()).model_copy(
            update={"plan_identity": plan_identity}
        )
        self.prepare(materialization, binding)
        self.actions.clear()

    def _metadata(self) -> LocalRecoveryMetadataV1:
        if self.metadata is None:
            raise LookupError("no prepared recovery")
        return self.metadata


class _HostContext(AbstractContextManager[_HostBoundary]):
    def __init__(self, boundary: _HostBoundary) -> None:
        self._boundary = boundary

    def __exit__(self, *_args: object) -> None:
        return None

    def __enter__(self) -> _HostBoundary:
        return self._boundary


def _configure_local_composition(
    monkeypatch: pytest.MonkeyPatch, root: Path, boundary: _HostBoundary
) -> None:
    def require(setting: object) -> object:
        if setting is LIBVIRT_RECOVERY_ROOT:
            return root
        if setting is LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES:
            return 1024 * 1024
        raise AssertionError(f"unexpected setting: {setting}")

    monkeypatch.setattr(local_composition.config, "require", require)
    monkeypatch.setattr(RealLocalExternalBootIO, "open", lambda *_args: boundary.open())


def _host_config(secure: Path, instance: str, database_dsn: str) -> AuthorityHostConfig:
    material = _tls_material(secure, instance)
    dsn_path = secure / "database-dsn"
    dsn_path.write_text(database_dsn, encoding="utf-8")
    dsn_path.chmod(0o400)
    return AuthorityHostConfig(
        authority_instance=instance,
        authority_uid=os.geteuid(),
        authority_gid=os.getegid(),
        authority_client_gid=os.getegid(),
        journal_dir=secure / "journal",
        request_socket=secure / "request" / "authority.sock",
        provider_socket=secure / "provider.sock",
        database_dsn=dsn_path,
        server_private_key=material["server_key"],
        server_certificate=material["server_certificate"],
        server_ca=material["server_ca"],
        worker_client_ca=material["server_ca"],
        health_client_certificate=material["client_certificate"],
        health_client_key=material["client_key"],
    )


def _plan() -> ExternalBootPlan:
    return ExternalBootPlan.model_validate(sample_plan_data())


def test_composition_built_provider_drives_six_ports_and_replays_exact_preparation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "recovery"
    root.mkdir(mode=0o700)
    boundary = _HostBoundary()
    _configure_local_composition(monkeypatch, root, boundary)
    binding = local_composition.build_local_external_boot_authority(
        cast(Any, object()), tmp_path / "provider.sock"
    )
    plan = _plan()
    activation = ExternalBootActivationBinding(
        system_id=plan.ownership.system_id,
        run_id=plan.ownership.run_id,
        activation_id=ACTIVATION_ID,
    )
    authority = OpaqueProviderRef(ref="authority/current")
    materialize = ExternalBootPreparationRequest(
        phase="materialize",
        plan=plan,
        binding=activation,
        authority=authority,
        operation_identity="materialize",
    )
    prepared = ExternalBootPreparationRequest(
        phase="prepare",
        plan=plan,
        binding=activation,
        authority=authority,
        operation_identity="prepare",
    )

    first_materialization = binding.provider.execute_preparation(materialize)
    assert binding.provider.execute_preparation(materialize) == first_materialization
    first_preparation = binding.provider.execute_preparation(prepared)
    assert binding.provider.execute_preparation(prepared) == first_preparation
    point = first_preparation.recovery_point
    assert point is not None

    binding.provider.activate(point, authority)
    observation = binding.provider.observe(point, authority)
    assert observation.identity.release == plan.module_obligation.release
    binding.provider.recover(point, authority)
    binding.provider.cleanup(point, authority)

    assert boundary.actions == [
        "materialize",
        "prepare",
        "activate-modules",
        "define-target",
        "observe",
        "recover-modules",
        "define-source",
        "restore-power",
        "cleanup",
    ]


@pytest.mark.anyio
async def test_typed_sender_reaches_constructed_sql_backed_authority_service(
    monkeypatch: pytest.MonkeyPatch,
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
) -> None:
    with psycopg.connect(migrated_url) as connection:
        case = _seed_case(connection, worker_suffix="c")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        allocated = _allocate(worker, case)
    with psycopg.connect(migrated_url) as connection:
        connection.execute(
            "UPDATE worker_incarnations SET credential_hash = %s WHERE incarnation = %s",
            (hashlib.sha256(case.credential).digest(), case.worker_id),
        )

    with tempfile.TemporaryDirectory(prefix="kdive-authority-", dir=Path.home()) as temporary:
        secure = Path(temporary)
        secure.chmod(0o700)
        root = secure / "recovery"
        journal = secure / "journal"
        root.mkdir(mode=0o700)
        journal.mkdir(mode=0o700)
        runtime_config.load(
            {
                "KDIVE_LIBVIRT_RECOVERY_ROOT": str(root),
                "KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES": "1048576",
            }
        )
        config = _host_config(
            secure, case.authority_instance, authority_role_dsns("kdive_provider_authority")
        )
        boundary = _HostBoundary()
        _configure_local_composition(monkeypatch, root, boundary)
        monkeypatch.setattr(
            provider_composition, "object_store_from_env", lambda: cast(Any, object())
        )
        service = host._build_mutation_service(config)  # noqa: SLF001
        assert service is not None
        config.request_socket.parent.mkdir(mode=0o2750)
        config.request_socket.parent.chmod(0o2750)
        worker_credential = SecretStr(case.credential.decode())
        received_credentials: list[str] = []

        async def authenticate(credential: SecretStr) -> Any:
            received_credentials.append(credential.get_secret_value())
            return await host._authenticate(config, credential)  # noqa: SLF001

        def deadline() -> float:
            return asyncio.get_running_loop().time() + 10

        listener = None
        try:
            listener = await transport.serve_authority_transport(
                config, authenticate, service=service
            )
            await listener.start_serving()
            client_binding = LocalAuthorityBinding(
                authority_instance=case.authority_instance,
                request_socket=config.request_socket,
                server_ca_ref="server-ca",
                client_cert_ref="client-certificate",
                client_key_ref="client-key",  # pragma: allowlist secret - fixture reference
            )
            sender = AuthorityRequestSender(
                lambda: _AuthorityUnixTransport(
                    client_binding,
                    _resolve_tls_material(client_binding, FileRefBackend(secure, SecretRegistry())),
                ),
                lambda: worker_credential,
            )
            takeover = AuthorityTakeoverRequestV1(
                authority_id=allocated.authority_id,
                generation=allocated.generation,
                system_id=case.system_id,
                activation_id=case.activation_id,
                run_id=case.run_id,
                plan_identity="sha256:" + "a" * 64,
                purpose=cast(Any, case.purpose),
                operation=case.operation,
                provider_kind=case.provider_kind,
                authority_instance=case.authority_instance,
                operation_identity=case.operation_identity,
                operation_digest=allocated.operation_digest,
            )
            boundary.prime(
                ExternalBootActivationBinding(
                    system_id=str(case.system_id),
                    run_id=str(case.run_id),
                    activation_id=str(case.activation_id),
                ),
                takeover.plan_identity,
            )
            acknowledgement = await sender.acknowledge_takeover(takeover, deadline=deadline())
            await listener.close()
            listener = None
            await service.close()
            service = host._build_mutation_service(config)  # noqa: SLF001
            assert service is not None
            listener = await transport.serve_authority_transport(
                config, authenticate, service=service
            )
            await listener.start_serving()
            assert (
                await sender.acknowledge_takeover(takeover, deadline=deadline()) == acknowledgement
            )
            mutation = AuthorityMutationRequestV1.model_validate(
                takeover.model_dump(mode="json", by_alias=True)
                | {
                    "attempt_id": str(uuid4()),
                    "expected_source_identity": _SOURCE,
                    "intended_target_identity": _TARGET,
                    "recovery_objects": [],
                }
            )
            observation = await sender.execute_mutation(mutation, deadline=deadline())

            assert acknowledgement.authority_id == allocated.authority_id
            assert observation.category == "target"
            assert boundary.actions == ["activate-modules", "define-target"]
            assert received_credentials == [worker_credential.get_secret_value()] * 3

            async def reject_role(_connection: object) -> None:
                raise host.HostReadinessError("database", "role-check")

            with monkeypatch.context() as guarded:
                guarded.setattr(host, "check_database_role", reject_role)
                rejected = host._build_mutation_service(config)  # noqa: SLF001
                assert rejected is not None
                with pytest.raises(host.HostReadinessError, match="database: role-check"):
                    await rejected.acknowledge_takeover(AuthenticatedPeer(case.worker_id), takeover)
                await rejected.close()
        finally:
            if listener is not None:
                await listener.close()
            await service.close()


class _Ready(Exception):
    """Raised at READY=1 to stop the host after its startup readiness passed."""


@pytest.mark.anyio
@pytest.mark.parametrize("torn", [False, True], ids=["record", "torn"])
async def test_host_startup_retracts_a_refused_anchor_left_by_a_failed_retraction(
    monkeypatch: pytest.MonkeyPatch,
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
    torn: bool,
) -> None:
    """#2793: one refused anchor no longer restart-loops the host on journal: head-mismatch.

    #2983: nor does that anchor torn mid-append, which fails startup's check as invalid-lane.
    """
    with psycopg.connect(migrated_url) as connection:
        case = _seed_case(connection, worker_suffix="r")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        allocated = _allocate(worker, case)
    with psycopg.connect(migrated_url) as connection:
        connection.execute(
            "UPDATE worker_incarnations SET credential_hash = %s WHERE incarnation = %s",
            (hashlib.sha256(case.credential).digest(), case.worker_id),
        )

    with tempfile.TemporaryDirectory(prefix="kdive-authority-", dir=Path.home()) as temporary:
        secure = Path(temporary)
        secure.chmod(0o700)
        root = secure / "recovery"
        journal = secure / "journal"
        state = secure / "state"
        for directory in (root, journal, state):
            directory.mkdir(mode=0o700)
        runtime_config.load(
            {
                "KDIVE_LIBVIRT_RECOVERY_ROOT": str(root),
                "KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES": "1048576",
            }
        )
        config = replace(
            _host_config(
                secure, case.authority_instance, authority_role_dsns("kdive_provider_authority")
            ),
            state_dir=state,
        )
        config.request_socket.parent.mkdir(mode=0o2750)
        config.request_socket.parent.chmod(0o2750)
        boundary = _HostBoundary()
        _configure_local_composition(monkeypatch, root, boundary)
        monkeypatch.setattr(
            provider_composition, "object_store_from_env", lambda: cast(Any, object())
        )
        takeover = AuthorityTakeoverRequestV1(
            authority_id=allocated.authority_id,
            generation=allocated.generation,
            system_id=case.system_id,
            activation_id=case.activation_id,
            run_id=case.run_id,
            plan_identity="sha256:" + "a" * 64,
            purpose=cast(Any, case.purpose),
            operation=case.operation,
            provider_kind=case.provider_kind,
            authority_instance=case.authority_instance,
            operation_identity=case.operation_identity,
            operation_digest=allocated.operation_digest,
        )
        boundary.prime(
            ExternalBootActivationBinding(
                system_id=str(case.system_id),
                run_id=str(case.run_id),
                activation_id=str(case.activation_id),
            ),
            takeover.plan_identity,
        )
        peer = AuthenticatedPeer(case.worker_id)
        service = host._build_mutation_service(config)  # noqa: SLF001
        assert service is not None
        try:
            await service.acknowledge_takeover(peer, takeover)
            lane = journal / f"{case.system_id}.jsonl"
            anchored = lane.read_bytes()
            repository = cast(Any, service)._repository  # noqa: SLF001
            advance = repository.advance

            async def fenced_advance(*args: Any) -> str:
                # A concurrent fence lands after the service's checks: the real head-advance
                # function then refuses the record the service has already fsynced.
                with psycopg.connect(migrated_url) as admin:
                    admin.execute(
                        "UPDATE worker_incarnations SET state = 'terminated', "
                        "terminated_at = clock_timestamp(), outcome = 'killed' "
                        "WHERE incarnation = %s",
                        (case.worker_id,),
                    )
                return await advance(*args)

            def failed_retraction(*_args: object) -> None:
                raise OSError("injected retraction failure")

            monkeypatch.setattr(repository, "advance", fenced_advance)
            # raising=False keeps this test runnable on a build without retract, where it must
            # fail at startup with journal: head-mismatch rather than at this patch.
            monkeypatch.setattr(
                host.FileAuthorityJournal, "retract", failed_retraction, raising=False
            )
            mutation = AuthorityMutationRequestV1.model_validate(
                takeover.model_dump(mode="json", by_alias=True)
                | {
                    "attempt_id": str(uuid4()),
                    "expected_source_identity": _SOURCE,
                    "intended_target_identity": _TARGET,
                    "recovery_objects": [],
                }
            )
            with pytest.raises(AuthorityServiceError, match="superseded"):
                await service.execute_mutation(peer, mutation)
        finally:
            await service.close()
        unanchored = lane.read_bytes()
        assert unanchored.startswith(anchored)
        assert len(unanchored.splitlines()) == len(anchored.splitlines()) + 1
        if torn:
            unanchored = unanchored[:-10]
            lane.write_bytes(unanchored)
        monkeypatch.undo()
        _configure_local_composition(monkeypatch, root, boundary)
        monkeypatch.setattr(
            provider_composition, "object_store_from_env", lambda: cast(Any, object())
        )

        def ready(message: str) -> None:
            if message == "READY=1":
                raise _Ready

        async def installed_host_fact(_config: AuthorityHostConfig) -> None:
            return None

        # The installed access boundary (fixed system paths and accounts) and the provider
        # socket are host facts this test cannot own; everything else is the real startup.
        monkeypatch.setattr(host, "_validate_access_boundary", lambda _config: None)
        monkeypatch.setattr(host, "_check_provider_socket", installed_host_fact)
        monkeypatch.setattr(host, "_notify_systemd", ready)

        with pytest.raises(_Ready):
            await host.run_authority_host(config)

        assert lane.read_bytes() == anchored
        evidence = list((journal / "retracted").iterdir())
        assert [path.read_bytes() for path in evidence] == [unanchored[len(anchored) :]]


@pytest.mark.anyio
async def test_startup_reconcile_waits_on_the_lane_advisory_lock(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    """#2793: no retraction while another session holds the key the head advance takes."""
    key = "hashtextextended('kdive:system:' || v_authority.system_id::text, 2126)"
    lock = "SELECT pg_advisory_xact_lock(hashtextextended('kdive:system:' || %s::text, 2126))"
    with psycopg.connect(migrated_url) as admin:
        definition = admin.execute(
            "SELECT pg_get_functiondef('public.advance_external_boot_authority_journal_head"
            "(text,uuid,bigint,bigint,text,jsonb)'::regprocedure)"
        ).fetchone()
    assert definition is not None and key in definition[0]
    with tempfile.TemporaryDirectory(prefix="kdive-authority-", dir=Path.home()) as temporary:
        secure = Path(temporary)
        secure.chmod(0o700)
        (secure / "journal").mkdir(mode=0o700)
        (secure / "request").mkdir(mode=0o700)
        config = _host_config(
            secure, "authority-lock", authority_role_dsns("kdive_provider_authority")
        )
        system_id = uuid4()
        record = JournalRecordV1(
            authority_id=uuid4(),
            generation=1,
            system_id=system_id,
            activation_id=uuid4(),
            run_id=uuid4(),
            plan_identity="sha256:" + "a" * 64,
            purpose="activate",
            operation="activate",
            provider_kind="local-libvirt",
            authority_instance=config.authority_instance,
            operation_identity="operation-a",
            operation_digest="sha256:" + "b" * 64,
            sequence=1,
            previous_digest=GENESIS_DIGEST,
            phase=JournalPhase.WATERMARK_INSTALLED,
            attempt_id=uuid4(),
        )
        lane = config.journal_dir / f"{system_id}.jsonl"
        lane.write_bytes(canonical_record_bytes(record) + b"\n")
        lane.chmod(0o600)

        with psycopg.connect(migrated_url) as holder:
            holder.execute(lock, (str(system_id),))
            with pytest.raises(host.HostReadinessError, match="journal: reconcile-failed"):
                await host._reconcile_journal_tails(config)  # noqa: SLF001
        assert lane.read_bytes() == canonical_record_bytes(record) + b"\n"

        await host._reconcile_journal_tails(config)  # noqa: SLF001
        assert not lane.exists()


@pytest.mark.anyio
async def test_periodic_check_racing_an_anchor_waits_for_the_head_advance(
    monkeypatch: pytest.MonkeyPatch,
    migrated_url: str,
    authority_role_dsns: _RoleDsns,
) -> None:
    """#2899: a periodic check between an anchor's fsync and its head advance does not fail."""
    with psycopg.connect(migrated_url) as connection:
        case = _seed_case(connection, worker_suffix="p")
    with psycopg.connect(authority_role_dsns("kdive_worker"), autocommit=True) as worker:
        allocated = _allocate(worker, case)

    with tempfile.TemporaryDirectory(prefix="kdive-authority-", dir=Path.home()) as temporary:
        secure = Path(temporary)
        secure.chmod(0o700)
        root = secure / "recovery"
        journal = secure / "journal"
        for directory in (root, journal):
            directory.mkdir(mode=0o700)
        runtime_config.load(
            {
                "KDIVE_LIBVIRT_RECOVERY_ROOT": str(root),
                "KDIVE_LIBVIRT_EXTERNAL_BOOT_CAPACITY_BYTES": "1048576",
            }
        )
        config = _host_config(
            secure, case.authority_instance, authority_role_dsns("kdive_provider_authority")
        )
        boundary = _HostBoundary()
        _configure_local_composition(monkeypatch, root, boundary)
        monkeypatch.setattr(
            provider_composition, "object_store_from_env", lambda: cast(Any, object())
        )

        async def installed_host_fact(_config: AuthorityHostConfig) -> None:
            return None

        # The installed access boundary and the provider socket are host facts this test cannot
        # own; the heads, the lanes and the anchor are real.
        monkeypatch.setattr(host, "_validate_access_boundary", lambda _config: None)
        monkeypatch.setattr(host, "_check_provider_socket", installed_host_fact)
        takeover = AuthorityTakeoverRequestV1(
            authority_id=allocated.authority_id,
            generation=allocated.generation,
            system_id=case.system_id,
            activation_id=case.activation_id,
            run_id=case.run_id,
            plan_identity="sha256:" + "a" * 64,
            purpose=cast(Any, case.purpose),
            operation=case.operation,
            provider_kind=case.provider_kind,
            authority_instance=case.authority_instance,
            operation_identity=case.operation_identity,
            operation_digest=allocated.operation_digest,
        )
        boundary.prime(
            ExternalBootActivationBinding(
                system_id=str(case.system_id),
                run_id=str(case.run_id),
                activation_id=str(case.activation_id),
            ),
            takeover.plan_identity,
        )
        peer = AuthenticatedPeer(case.worker_id)
        service = host._build_mutation_service(config)  # noqa: SLF001
        assert service is not None
        appended = asyncio.Event()
        check_failed = asyncio.Event()
        try:
            await service.acknowledge_takeover(peer, takeover)
            lane = journal / f"{case.system_id}.jsonl"
            repository = cast(Any, service)._repository  # noqa: SLF001
            advance = repository.advance

            async def paused_advance(*args: Any) -> str:
                if not appended.is_set():
                    appended.set()
                    await check_failed.wait()
                return await advance(*args)

            validate = host.JournalInventoryValidator.validate

            async def observed(validator: Any, *args: Any) -> None:
                try:
                    await validate(validator, *args)
                except host.HostReadinessError:
                    check_failed.set()
                    raise

            monkeypatch.setattr(repository, "advance", paused_advance)
            monkeypatch.setattr(host.JournalInventoryValidator, "validate", observed)
            before = len(lane.read_bytes().splitlines())
            mutation = asyncio.create_task(
                service.execute_mutation(
                    peer,
                    AuthorityMutationRequestV1.model_validate(
                        takeover.model_dump(mode="json", by_alias=True)
                        | {
                            "attempt_id": str(uuid4()),
                            "expected_source_identity": _SOURCE,
                            "intended_target_identity": _TARGET,
                            "recovery_objects": [],
                        }
                    ),
                )
            )
            await appended.wait()
            assert len(lane.read_bytes().splitlines()) == before + 1
            validator = host.JournalInventoryValidator(anchor_quiescence=service.quiesce_anchors)
            async with asyncio.timeout(15):
                await host._check_static_authority_host(config, validator, None)  # noqa: SLF001
                assert check_failed.is_set()
                assert (await mutation).category == "target"
                await host._check_static_authority_host(config, validator, None)  # noqa: SLF001
        finally:
            # A failure before the check must not leave the paused anchor holding close() open.
            check_failed.set()
            await service.close()
