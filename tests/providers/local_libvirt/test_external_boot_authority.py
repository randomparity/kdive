"""Local-libvirt external-boot authority adapter (ADR-0584, #2199).

The fake below stands in for ``LocalExternalBootIO`` so the real coordinator
``LocalLibvirtExternalBoot`` runs under test; only the host resources beneath it are
doubled.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
from pathlib import Path
from typing import Literal, cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from kdive.providers.external_boot_authority.journal import FileAuthorityJournal
from kdive.providers.external_boot_authority.protocol import (
    AuthorityCommitContextV1,
    AuthorityMutationRequestV1,
    AuthorityOperation,
    AuthorityTakeoverRequestV1,
    JournalPhase,
    JournalRecordV1,
    RecoveryObjectBindingV1,
    operation_is_permitted,
    record_digest,
)
from kdive.providers.external_boot_authority.service import (
    AuthenticatedPeer,
    AuthorityMutationAdapter,
    AuthorityServiceError,
    ExternalBootAuthorityService,
)
from kdive.providers.local_libvirt import external_boot_authority as adapter_module
from kdive.providers.local_libvirt.external_boot_authority import (
    LocalExternalBootAuthorityAdapter,
)
from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    CleanupTombstoneV1,
    FinalizeCleanupProof,
    LocalExternalBootIO,
    LocalLibvirtExternalBoot,
    LocalObservedState,
    LocalRecoveryMetadataV1,
    RecoveryPhase,
)
from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ComponentState,
    ExternalBootActivationBinding,
    KernelIdentity,
    OpaqueProviderRef,
    PresentComponentState,
    ProviderStateIdentity,
    RecoveryPoint,
    RunningKernelObservation,
)

# The authority service's own repository double. Reused rather than reimplemented here:
# a second implementation of `AuthorityRepository` in this package could drift from the
# contract the service is actually tested against, which is the thing these tests rely on.
from tests.providers.external_boot_authority.service_support import _Repository

pytestmark = pytest.mark.anyio

SYSTEM_ID = UUID("00000000-0000-0000-0000-000000000001")
RUN_ID = UUID("00000000-0000-0000-0000-000000000002")
ACTIVATION_ID = UUID("00000000-0000-0000-0000-000000000003")
AUTHORITY_ID = UUID("00000000-0000-0000-0000-00000000000a")
ATTEMPT_ID = UUID("00000000-0000-0000-0000-00000000000b")

PLAN_IDENTITY = "sha256:" + "6" * 64
SOURCE_IDENTITY = "sha256:" + "b" * 64
TARGET_IDENTITY = "sha256:" + "c" * 64
SOURCE_MODULES = AbsentComponentState()
TARGET_MODULES = PresentComponentState(manifest="sha256:" + "3" * 64)

_SOURCE_XML = "<domain type='kvm'><name>d</name><devices><disk src='/old'/></devices></domain>"

_BINDING = ExternalBootActivationBinding(
    system_id=str(SYSTEM_ID),
    run_id=str(RUN_ID),
    activation_id=str(ACTIVATION_ID),
)
_RECOVERY_REF = OpaqueProviderRef(ref=f"local-recovery-v1/{SYSTEM_ID}/{ACTIVATION_ID}")

# Provider-native vocabulary that must never appear in the adapter module: generic libvirt
# power operations, domain-XML synthesis, and host-resource selectors.
_FORBIDDEN_NAMES = (
    "create",
    "destroy",
    "reset",
    "shutdown",
    "define_xml",
    "defineXML",
    "XMLDesc",
    "createXML",
    "render_target_xml",
    "target_xml",
    "source_xml",
    "open_artifact",
    "libvirt",
    "guestfs",
    "subprocess",
)


def _metadata(phase: RecoveryPhase = "pre-stop-intent") -> LocalRecoveryMetadataV1:
    return LocalRecoveryMetadataV1(
        binding=_BINDING,
        plan_identity=PLAN_IDENTITY,
        materialization_identity="sha256:" + "7" * 64,
        release="6.12.0",
        materialized_modules=OpaqueProviderRef(ref="artifacts/system/run/modules"),
        materialized_modules_sha256="sha256:" + "8" * 64,
        materialized_modules_bytes=123,
        source_xml_sha256="sha256:" + hashlib.sha256(_SOURCE_XML.encode()).hexdigest(),
        source_xml=_SOURCE_XML,
        source_definition="sha256:" + "a" * 64,
        source_boot=SOURCE_IDENTITY,
        target_boot=TARGET_IDENTITY,
        target_projection_sha256="sha256:" + "d" * 64,
        target_xml_sha256="sha256:"
        + hashlib.sha256(_SOURCE_XML.replace("/old", "/new").encode()).hexdigest(),
        target_xml=_SOURCE_XML.replace("/old", "/new"),
        expected_running=KernelIdentity(
            architecture="x86_64", release="6.12.0", gnu_build_id="01020304"
        ),
        source_state=ProviderStateIdentity(definition=SOURCE_IDENTITY, modules=SOURCE_MODULES),
        target_state=ProviderStateIdentity(definition=TARGET_IDENTITY, modules=TARGET_MODULES),
        prior_power="running",
        capture={"state": "absent"},
        phase=phase,
    )


class _FakeIO:
    """Stands in for ``LocalExternalBootIO``; the coordinator above it is the real one."""

    def __init__(
        self,
        metadata: LocalRecoveryMetadataV1 | None = None,
        *,
        observed: LocalObservedState | None = None,
        reopen_fault: bool = False,
        observe_fault: bool = False,
        define_target_faults: int = 0,
    ) -> None:
        self.define_target_faults = define_target_faults
        self.metadata = metadata if metadata is not None else _metadata()
        self.observed = (
            observed
            if observed is not None
            else LocalObservedState(
                definition=SOURCE_IDENTITY, modules=SOURCE_MODULES, active=False
            )
        )
        self.reopen_fault = reopen_fault
        self.observe_fault = observe_fault
        self.actions: list[str] = []
        self.tombstone = False
        self.tombstone_error: BaseException | None = None
        # `publish_tombstone` writes the tombstone and then unlinks `intent.json`. Modelling
        # both lets `finalize_tombstone` below refuse exactly where the real store refuses.
        self.intent_present = True
        self.finalized_proof: FinalizeCleanupProof | None = None
        # Raised by `reopen_binding` in place of returning the record. `FileNotFoundError` is
        # what the real store raises for every state in which the recovery record cannot be
        # rebuilt; `OSError` stands for a read that merely failed.
        self.reopen_error: BaseException | None = None

    # -- LocalExternalBootIO -------------------------------------------------------
    def open(self, authority: OpaqueProviderRef, expected: object) -> _FakeContext:
        del expected
        self.actions.append(f"open:{authority.ref}")
        return _FakeContext(self)

    def finalize_tombstone(self, recovery: RecoveryPoint, proof: FinalizeCleanupProof) -> None:
        del recovery
        if self.tombstone and self.intent_present:
            self.intent_present = False
        self.tombstone = False
        self.finalized_proof = proof
        self.actions.append("finalize")

    # -- LocalExternalBootOperation ------------------------------------------------
    def recovery_ref(self, binding: ExternalBootActivationBinding) -> OpaqueProviderRef:
        del binding
        return _RECOVERY_REF

    def reopen(self, recovery: RecoveryPoint) -> LocalRecoveryMetadataV1:
        del recovery
        return self.reopen_binding(_BINDING)

    def reopen_binding(self, binding: ExternalBootActivationBinding) -> LocalRecoveryMetadataV1:
        del binding
        self.actions.append("reopen")
        if self.reopen_error is not None:
            raise self.reopen_error
        if not self.intent_present:
            # `publish_tombstone` unlinked `intent.json`, so the real store can no longer
            # rebuild the record. Without this the double would exhibit "cleanup completed,
            # record still resolvable", which production cannot reach.
            raise FileNotFoundError("intent.json")
        if self.reopen_fault:
            raise LookupError("libguestfs: /var/lib/kdive/secret.key unreadable")
        return self.metadata

    def reopen_cleanup_tombstone(
        self, binding: ExternalBootActivationBinding
    ) -> CleanupTombstoneV1:
        if self.tombstone_error is not None:
            raise self.tombstone_error
        if not self.tombstone:
            raise FileNotFoundError("tombstone.json")
        point = _point(self.metadata)
        return CleanupTombstoneV1(
            binding=binding,
            recovery_point=point,
            point_digest=LocalLibvirtExternalBoot.point_digest(point),
        )

    def observe_state(self, metadata: LocalRecoveryMetadataV1) -> LocalObservedState:
        del metadata
        self.actions.append("observe-state")
        if self.observe_fault:
            raise LookupError("libvirt: qemu+ssh://operator@vmhost/system refused")
        return self.observed

    def activate_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("activate-modules")
        self.record_phase(metadata, "module-restored")

    def define_target(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.actions.append("define-target")
        if self.define_target_faults > 0:
            # Interrupt the operation after activate_modules committed the module tree but
            # before the target definition is acknowledged.
            self.define_target_faults -= 1
            raise LookupError("libvirt: define interrupted")
        self.record_phase(metadata, "target-defined")

    def observe_running(self, metadata: LocalRecoveryMetadataV1) -> RunningKernelObservation:
        self.actions.append("observe-running")
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
        self.actions.append(f"phase:{phase}")
        self.metadata = metadata.model_copy(update={"phase": phase})
        return self.metadata

    def cleanup_complete(self, recovery: RecoveryPoint) -> bool:
        del recovery
        return self.tombstone

    def cleanup(self, metadata: LocalRecoveryMetadataV1, point_digest: str) -> None:
        del metadata, point_digest
        self.actions.append("cleanup")
        self.tombstone = True
        self.intent_present = False

    def materialize(self, plan: object) -> object:
        raise AssertionError("the authority adapter must not materialize")

    def prepare(self, materialization: object, binding: object) -> object:
        raise AssertionError("the authority adapter must not prepare")


class _FakeContext:
    def __init__(self, io: _FakeIO) -> None:
        self._io = io

    def __enter__(self) -> _FakeIO:
        return self._io

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback


def _adapter(io: _FakeIO) -> LocalExternalBootAuthorityAdapter:
    ports = LocalLibvirtExternalBoot(cast(LocalExternalBootIO, io))
    return LocalExternalBootAuthorityAdapter(ports)


def _request(
    *,
    purpose: str = "activate",
    operation: AuthorityOperation = AuthorityOperation.ACTIVATE,
    generation: int = 7,
    expected_source: str = SOURCE_IDENTITY,
    intended_target: str = TARGET_IDENTITY,
    plan_identity: str = PLAN_IDENTITY,
    recovery_objects: tuple[RecoveryObjectBindingV1, ...] = (),
) -> AuthorityMutationRequestV1:
    return AuthorityMutationRequestV1(
        authority_id=AUTHORITY_ID,
        generation=generation,
        system_id=SYSTEM_ID,
        activation_id=ACTIVATION_ID,
        run_id=RUN_ID,
        plan_identity=plan_identity,
        purpose=cast(Literal["activate"], purpose),
        operation=operation,
        provider_kind="local-libvirt",
        authority_instance="local-authority",
        operation_identity="op-1",
        operation_digest="sha256:" + "9" * 64,
        attempt_id=ATTEMPT_ID,
        expected_source_identity=expected_source,
        intended_target_identity=intended_target,
        recovery_objects=recovery_objects,
    )


def _context(
    operation: AuthorityOperation = AuthorityOperation.ACTIVATE,
    *,
    sequence: int = 4,
    generation: int = 7,
    operation_identity: str = "op-1",
) -> AuthorityCommitContextV1:
    """The context the service builds from the record it anchored for this request.

    Built through ``for_record`` rather than by hand, so a test can never present the adapter
    a context the service could not have produced.
    """
    record = JournalRecordV1(
        authority_id=AUTHORITY_ID,
        generation=generation,
        system_id=SYSTEM_ID,
        activation_id=ACTIVATION_ID,
        run_id=RUN_ID,
        plan_identity=PLAN_IDENTITY,
        purpose=cast(Literal["activate"], _PURPOSE_FOR[operation]),
        operation=operation,
        provider_kind="local-libvirt",
        authority_instance="local-authority",
        operation_identity=operation_identity,
        operation_digest="sha256:" + "9" * 64,
        sequence=sequence,
        previous_digest="sha256:" + "0" * 64,
        phase=JournalPhase.MUTATION_STARTED,
        attempt_id=ATTEMPT_ID,
        expected_source_identity=SOURCE_IDENTITY,
        intended_target_identity=TARGET_IDENTITY,
        recovery_objects=(),
    )
    return AuthorityCommitContextV1.for_record(record)


_PURPOSE_FOR: dict[AuthorityOperation, str] = {
    AuthorityOperation.ACTIVATE: "activate",
    AuthorityOperation.DEADLINE: "activate",
    AuthorityOperation.FAIL: "activate",
    AuthorityOperation.RECOVER: "recover",
    AuthorityOperation.RECOVERY_ATTEMPT: "recover",
    AuthorityOperation.RESOLVE_CONFLICT: "resolve-conflict",
    AuthorityOperation.RELEASE: "release",
    AuthorityOperation.CLEANUP: "release",
    AuthorityOperation.TEARDOWN: "teardown",
}


def _point(metadata: LocalRecoveryMetadataV1) -> RecoveryPoint:
    """The recovery point the coordinator rebuilds from the durable record."""
    return RecoveryPoint(
        binding=metadata.binding,
        plan_identity=metadata.plan_identity,
        materialization_identity=metadata.materialization_identity,
        recovery_ref=_RECOVERY_REF,
        source_state=metadata.source_state,
        target_state=metadata.target_state,
    )


def _owned_object() -> RecoveryObjectBindingV1:
    return RecoveryObjectBindingV1(
        system_id=SYSTEM_ID, activation_id=ACTIVATION_ID, reference=_RECOVERY_REF.ref
    )


def _foreign_object() -> RecoveryObjectBindingV1:
    return RecoveryObjectBindingV1(
        system_id=SYSTEM_ID,
        activation_id=ACTIVATION_ID,
        reference="local-recovery-v1/other/unproven",
    )


def _observed(
    definition: str | None, modules: ComponentState | None, active: bool | None = False
) -> LocalObservedState:
    return LocalObservedState(definition=definition, modules=modules, active=active)


# --------------------------------------------------------------------------------------
# AC 1 - the adapter satisfies the authority seam
# --------------------------------------------------------------------------------------


def test_adapter_satisfies_the_authority_mutation_adapter_protocol() -> None:
    instance: AuthorityMutationAdapter = _adapter(_FakeIO())

    assert instance is not None


# --------------------------------------------------------------------------------------
# AC 2 - commit-point legality, checked before any provider call
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "purpose", ["activate", "recover", "resolve-conflict", "release", "teardown"]
)
@pytest.mark.parametrize("operation", list(AuthorityOperation))
async def test_commit_refuses_every_illegal_purpose_operation_pair(
    purpose: str, operation: AuthorityOperation
) -> None:
    if operation_is_permitted(purpose, operation):
        pytest.skip("legal pair is covered by the accepted-commit-point tests")
    io = _FakeIO()
    legal = next(
        candidate for candidate in AuthorityOperation if operation_is_permitted(purpose, candidate)
    )
    request = _request(purpose=purpose, operation=legal)

    with pytest.raises(AuthorityServiceError) as caught:
        await _adapter(io).commit(request, _context(operation))

    assert caught.value.category == "provider_conflict"
    assert io.actions == []


async def test_a_commit_point_that_is_not_an_operation_cannot_reach_the_adapter() -> None:
    """The ground this used to cover moved down a layer when the seam stopped taking a str.

    It used to pass ``"rm -rf /"`` as the commit point and assert the adapter refused it.
    ``AuthorityCommitContextV1.commit_point`` is an ``AuthorityOperation``, so a closed model
    cannot carry a non-member and the adapter can no longer be handed one. The check is now
    at construction, which is where it belongs, and it still fails closed.
    """
    values = _context().model_dump(mode="json", by_alias=True)

    with pytest.raises(ValidationError):
        AuthorityCommitContextV1.model_validate(values | {"commit_point": "rm -rf /"})


# --------------------------------------------------------------------------------------
# AC 3 - only named local commit points; no generic power call, no XML synthesis
# --------------------------------------------------------------------------------------


def test_adapter_module_names_no_generic_power_operation_or_domain_xml() -> None:
    source = Path(inspect.getfile(adapter_module)).read_text()
    tree = ast.parse(source)

    attributes = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }

    reachable = attributes | called | imported
    for forbidden in _FORBIDDEN_NAMES:
        assert forbidden not in reachable, forbidden


async def test_accepted_commit_points_drive_named_local_primitives() -> None:
    io = _FakeIO(_metadata("pre-stop-intent"))

    await _adapter(io).commit(_request(), _context(AuthorityOperation.ACTIVATE))

    assert "activate-modules" in io.actions
    assert "define-target" in io.actions


async def test_recover_drives_the_named_recovery_primitives() -> None:
    io = _FakeIO(_metadata("target-defined"))
    request = _request(purpose="recover", operation=AuthorityOperation.RECOVER)

    await _adapter(io).commit(request, _context(AuthorityOperation.RECOVER))

    assert "recover-modules" in io.actions
    assert "define-source" in io.actions
    assert "restore-power" in io.actions


async def test_bookkeeping_operations_mutate_nothing() -> None:
    io = _FakeIO(_metadata("target-defined"))
    request = _request(purpose="recover", operation=AuthorityOperation.DEADLINE)

    await _adapter(io).commit(request, _context(AuthorityOperation.DEADLINE))

    mutating = {"activate-modules", "define-target", "recover-modules", "cleanup"}
    assert mutating.isdisjoint(io.actions)


# --------------------------------------------------------------------------------------
# AC 4 - categories derived from a real read of source and target state
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("definition", "modules", "expected"),
    [
        (SOURCE_IDENTITY, SOURCE_MODULES, "source"),
        (TARGET_IDENTITY, TARGET_MODULES, "target"),
        (TARGET_IDENTITY, SOURCE_MODULES, "mixed"),
        (SOURCE_IDENTITY, TARGET_MODULES, "mixed"),
        (None, SOURCE_MODULES, "unreadable"),
        (SOURCE_IDENTITY, None, "unreadable"),
        ("sha256:" + "f" * 64, SOURCE_MODULES, "conflict"),
        (SOURCE_IDENTITY, PresentComponentState(manifest="sha256:" + "e" * 64), "conflict"),
    ],
)
async def test_observe_classifies_every_source_target_category(
    definition: str | None, modules: ComponentState | None, expected: str
) -> None:
    io = _FakeIO(observed=_observed(definition, modules))

    observation = await _adapter(io).observe(_request())

    assert observation.category == expected
    assert "observe-state" in io.actions


async def test_composite_state_moves_when_either_observed_identity_moves() -> None:
    baseline = await _adapter(_FakeIO(observed=_observed(SOURCE_IDENTITY, SOURCE_MODULES))).observe(
        _request()
    )
    moved_definition = await _adapter(
        _FakeIO(observed=_observed(TARGET_IDENTITY, SOURCE_MODULES))
    ).observe(_request())
    moved_modules = await _adapter(
        _FakeIO(observed=_observed(SOURCE_IDENTITY, TARGET_MODULES))
    ).observe(_request())

    digests = {
        baseline.composite_state,
        moved_definition.composite_state,
        moved_modules.composite_state,
    }
    assert len(digests) == 3


async def test_unreadable_provider_read_is_a_category_not_an_exception() -> None:
    io = _FakeIO(observe_fault=True)

    observation = await _adapter(io).observe(_request())

    assert observation.category == "unreadable"


# --------------------------------------------------------------------------------------
# AC 5 - exact identity comparison, never a silent overwrite
# --------------------------------------------------------------------------------------


async def test_commit_refuses_a_mismatched_expected_source_identity() -> None:
    io = _FakeIO()
    request = _request(expected_source="sha256:" + "f" * 64)

    with pytest.raises(AuthorityServiceError) as caught:
        await _adapter(io).commit(request, _context(AuthorityOperation.ACTIVATE))

    assert caught.value.category == "provider_conflict"
    assert "activate-modules" not in io.actions


async def test_commit_refuses_a_mismatched_intended_target_identity() -> None:
    io = _FakeIO()
    request = _request(intended_target="sha256:" + "f" * 64)

    with pytest.raises(AuthorityServiceError) as caught:
        await _adapter(io).commit(request, _context(AuthorityOperation.ACTIVATE))

    assert caught.value.category == "provider_conflict"
    assert "activate-modules" not in io.actions


async def test_observe_reports_conflict_for_a_mismatched_identity() -> None:
    io = _FakeIO()

    observation = await _adapter(io).observe(_request(expected_source="sha256:" + "f" * 64))

    assert observation.category == "conflict"


async def test_commit_refuses_a_mismatched_plan_identity() -> None:
    io = _FakeIO()

    with pytest.raises(AuthorityServiceError):
        await _adapter(io).commit(
            _request(plan_identity="sha256:" + "f" * 64), _context(AuthorityOperation.ACTIVATE)
        )

    assert "activate-modules" not in io.actions


# --------------------------------------------------------------------------------------
# AC 6 - stable recovery-object ownership and quarantine
# --------------------------------------------------------------------------------------


async def test_owned_recovery_objects_keep_ownership_across_observe_and_commit() -> None:
    io = _FakeIO()
    request = _request(recovery_objects=(_owned_object(),))

    observed = await _adapter(io).observe(request)
    committed = await _adapter(io).commit(request, _context(AuthorityOperation.ACTIVATE))

    assert observed.category == "source"
    assert committed.category in {"source", "target", "mixed"}


async def test_unproven_recovery_object_is_quarantined_not_reused_or_deleted() -> None:
    io = _FakeIO()
    request = _request(recovery_objects=(_foreign_object(),))

    observation = await _adapter(io).observe(request)
    with pytest.raises(AuthorityServiceError) as caught:
        await _adapter(io).commit(request, _context(AuthorityOperation.ACTIVATE))

    assert observation.category == "conflict"
    assert caught.value.category == "provider_conflict"
    assert "activate-modules" not in io.actions
    assert "cleanup" not in io.actions
    assert "finalize" not in io.actions


# --------------------------------------------------------------------------------------
# AC 7 - no protocol-reachable URI, path, command, XML, or credential
# --------------------------------------------------------------------------------------


def test_construction_takes_only_the_coordinator() -> None:
    parameters = list(inspect.signature(LocalExternalBootAuthorityAdapter.__init__).parameters)

    assert parameters == ["self", "ports"]


def test_no_request_field_can_select_a_host_resource() -> None:
    hostile = _request(
        expected_source="../../etc/shadow",
        intended_target="qemu+ssh://operator@vmhost/system",
    )

    assert hostile.expected_source_identity == "../../etc/shadow"
    assert hostile.intended_target_identity == "qemu+ssh://operator@vmhost/system"
    # Identities are only ever compared, never opened, joined, or executed. The single
    # provider reference the adapter derives is closed against traversal and transport.
    with pytest.raises(ValueError, match="opaque"):
        OpaqueProviderRef(ref="../../etc/shadow")
    with pytest.raises(ValueError, match="opaque"):
        OpaqueProviderRef(ref="qemu+ssh://operator@vmhost/system")


async def test_hostile_identity_input_is_refused_without_touching_the_provider() -> None:
    io = _FakeIO()
    hostile = _request(expected_source="/etc/shadow")

    with pytest.raises(AuthorityServiceError):
        await _adapter(io).commit(hostile, _context(AuthorityOperation.ACTIVATE))

    assert "activate-modules" not in io.actions


# --------------------------------------------------------------------------------------
# AC 8 - bounded categories carrying no provider output
# --------------------------------------------------------------------------------------


async def test_provider_failure_becomes_a_bounded_category_without_provider_output() -> None:
    io = _FakeIO(reopen_fault=True)

    with pytest.raises(AuthorityServiceError) as caught:
        await _adapter(io).commit(_request(), _context(AuthorityOperation.ACTIVATE))

    assert caught.value.category == "provider_conflict"
    rendered = f"{caught.value!r} {caught.value.args}"
    for leaked in ("libguestfs", "libvirt", "/var/lib/kdive", "secret.key", "qemu+ssh"):
        assert leaked not in rendered, leaked


async def test_unreadable_observation_carries_no_provider_output() -> None:
    io = _FakeIO(observe_fault=True)

    observation = await _adapter(io).observe(_request())

    rendered = observation.model_dump_json()
    for leaked in ("libvirt", "operator", "vmhost", "qemu+ssh"):
        assert leaked not in rendered, leaked


async def test_every_error_category_is_one_of_the_four_bounded_values() -> None:
    io = _FakeIO(reopen_fault=True)
    bounded = {"unauthenticated", "superseded", "journal_conflict", "provider_conflict"}

    with pytest.raises(AuthorityServiceError) as caught:
        await _adapter(io).commit(_request(), _context(AuthorityOperation.ACTIVATE))

    assert caught.value.category in bounded


# --------------------------------------------------------------------------------------
# AC 9 - stale generation and partial commit
# --------------------------------------------------------------------------------------


async def test_a_generation_behind_the_admitted_watermark_is_superseded() -> None:
    io = _FakeIO()
    instance = _adapter(io)
    await instance.commit(_request(generation=9), _context(AuthorityOperation.ACTIVATE))
    io.actions.clear()

    with pytest.raises(AuthorityServiceError) as caught:
        await instance.commit(_request(generation=8), _context(AuthorityOperation.ACTIVATE))

    assert caught.value.category == "superseded"
    assert io.actions == []


async def test_a_commit_interrupted_after_the_mutation_does_not_double_apply() -> None:
    """Interrupt between the two commit points of one activation, then retry.

    ``activate_modules`` has published the module tree and advanced the durable phase, but
    ``define_target`` fails before the operation is acknowledged. The retry must complete
    the activation without republishing the module tree.
    """
    io = _FakeIO(_metadata("pre-stop-intent"), define_target_faults=1)
    instance = _adapter(io)

    with pytest.raises(AuthorityServiceError) as caught:
        await instance.commit(_request(), _context(AuthorityOperation.ACTIVATE))
    interrupted = list(io.actions)
    io.actions.clear()
    await instance.commit(_request(), _context(AuthorityOperation.ACTIVATE))

    assert caught.value.category == "provider_conflict"
    assert interrupted.count("activate-modules") == 1
    assert io.metadata.phase == "target-defined"
    # The module tree is published once across both attempts, not once per attempt.
    assert "activate-modules" not in io.actions
    assert io.actions.count("define-target") == 1


async def test_a_completed_commit_replayed_does_not_repeat_either_commit_point() -> None:
    io = _FakeIO(_metadata("pre-stop-intent"))
    instance = _adapter(io)

    await instance.commit(_request(), _context(AuthorityOperation.ACTIVATE))
    io.actions.clear()
    await instance.commit(_request(), _context(AuthorityOperation.ACTIVATE))

    assert "activate-modules" not in io.actions
    assert "define-target" not in io.actions
    assert (await instance.observe(_request())).category == "source"


async def test_commit_refuses_a_commit_point_that_is_not_the_journalled_operation() -> None:
    """A legal-for-purpose commit point that disagrees with the request is refused.

    Otherwise the journal record — ADR-0584's evidence of what mutation may have
    happened — would name an operation the adapter classifies as non-mutating while a
    full provider recovery ran.
    """
    io = _FakeIO(_metadata("target-defined"))
    request = _request(purpose="recover", operation=AuthorityOperation.DEADLINE)

    with pytest.raises(AuthorityServiceError) as caught:
        await _adapter(io).commit(request, _context(AuthorityOperation.RECOVER))

    assert caught.value.category == "provider_conflict"
    assert io.actions == []


@pytest.mark.parametrize("operation", [AuthorityOperation.CLEANUP, AuthorityOperation.TEARDOWN])
async def test_a_deleting_commit_point_must_name_the_object_it_destroys(
    operation: AuthorityOperation,
) -> None:
    """An empty recovery-object set is not a proof of ownership.

    ADR-0584 permits deleting a recovery object only when the stable binding is proven, so
    an operation that destroys recovery evidence has to name what it destroys.
    """
    purpose = "release" if operation is AuthorityOperation.CLEANUP else "teardown"
    io = _FakeIO(_metadata("recovered"))
    unnamed = _request(purpose=purpose, operation=operation)

    with pytest.raises(AuthorityServiceError) as caught:
        await _adapter(io).commit(unnamed, _context(operation))

    assert caught.value.category == "provider_conflict"
    assert "cleanup" not in io.actions


@pytest.mark.parametrize("operation", [AuthorityOperation.CLEANUP, AuthorityOperation.TEARDOWN])
async def test_a_deleting_commit_point_drives_cleanup_when_ownership_is_named(
    operation: AuthorityOperation,
) -> None:
    purpose = "release" if operation is AuthorityOperation.CLEANUP else "teardown"
    io = _FakeIO(_metadata("recovered"))
    named = _request(purpose=purpose, operation=operation, recovery_objects=(_owned_object(),))

    await _adapter(io).commit(named, _context(operation))

    assert "cleanup" in io.actions
    assert io.tombstone is True


async def test_release_without_cleanup_mutates_nothing() -> None:
    io = _FakeIO(_metadata("recovered"))
    request = _request(purpose="release", operation=AuthorityOperation.RELEASE)

    await _adapter(io).commit(request, _context(AuthorityOperation.RELEASE))

    assert "cleanup" not in io.actions
    assert io.tombstone is False


async def test_the_admitted_watermark_is_bounded_across_many_activations() -> None:
    """The in-process watermark must not grow without limit.

    Evicting the oldest lane can only make this check under-reject, which the service's
    own journal watermark still catches; retaining every lane forever cannot be undone.
    """
    instance = _adapter(_FakeIO())
    overflow = adapter_module._MAX_ADMITTED_LANES + 25
    for _ in range(overflow):
        lane = _request().model_copy(update={"system_id": uuid4(), "activation_id": uuid4()})
        instance._require_admissible_generation(lane)

    assert len(instance._admitted) == adapter_module._MAX_ADMITTED_LANES


async def test_composite_state_is_bound_to_the_activation_it_observed() -> None:
    """Identical observed content on two activations must not mint the same token.

    ``recover_from_conflict`` refuses a recovery unless the acknowledged composite state
    equals the recorded conflict evidence, so the digest proves a *particular* activation
    was observed. Observed content alone cannot carry that: a plain source domain's boot
    identity derives only from the ``<os>`` kernel, initrd and cmdline, all absent and
    therefore constant fleet-wide.
    """
    observed = _observed(SOURCE_IDENTITY, SOURCE_MODULES)
    mine = _request()
    theirs = mine.model_copy(
        update={"system_id": uuid4(), "activation_id": uuid4(), "run_id": uuid4()}
    )

    digest = LocalExternalBootAuthorityAdapter._composite_state
    assert digest(mine, observed) != digest(theirs, observed)


async def test_an_unreadable_observation_is_still_bound_to_its_activation() -> None:
    """The all-None path carries no content, so binding is the only distinguisher."""
    blank = _observed(None, None, None)
    mine = _request()
    theirs = mine.model_copy(
        update={"system_id": uuid4(), "activation_id": uuid4(), "run_id": uuid4()}
    )

    digest = LocalExternalBootAuthorityAdapter._composite_state
    assert digest(mine, blank) != digest(theirs, blank)


async def test_composite_state_still_moves_with_the_plan_it_was_observed_under() -> None:
    observed = _observed(SOURCE_IDENTITY, SOURCE_MODULES)
    mine = _request()
    replanned = mine.model_copy(update={"plan_identity": "sha256:" + "f" * 64})

    digest = LocalExternalBootAuthorityAdapter._composite_state
    assert digest(mine, observed) != digest(replanned, observed)


async def test_the_recovery_record_must_match_the_whole_requested_binding() -> None:
    """A Run mismatch is refused at the seam that made the claim, not two layers below."""
    foreign_run = _metadata().model_copy(
        update={
            "binding": _BINDING.model_copy(
                update={"run_id": "00000000-0000-0000-0000-0000000000ee"}
            )
        }
    )
    io = _FakeIO(foreign_run)

    with pytest.raises(AuthorityServiceError) as caught:
        await _adapter(io).commit(_request(), _context(AuthorityOperation.ACTIVATE))

    assert caught.value.category == "provider_conflict"
    assert "activate-modules" not in io.actions


# --------------------------------------------------------------------------------------
# #2207 - the cleanup commit point finalizes the tombstone against the anchored record
# --------------------------------------------------------------------------------------


def _cleanup_takeover() -> AuthorityTakeoverRequestV1:
    return AuthorityTakeoverRequestV1(
        authority_id=AUTHORITY_ID,
        generation=7,
        system_id=SYSTEM_ID,
        activation_id=ACTIVATION_ID,
        run_id=RUN_ID,
        plan_identity=PLAN_IDENTITY,
        purpose="release",
        operation=AuthorityOperation.RELEASE,
        provider_kind="local-libvirt",
        authority_instance="local-authority",
        operation_identity="takeover-release",
        operation_digest="sha256:" + "9" * 64,
    )


type _CleanupLane = tuple[
    ExternalBootAuthorityService,
    _Repository,
    AuthenticatedPeer,
    AuthorityTakeoverRequestV1,
]


def _cleanup_service(io: _FakeIO, tmp_path: Path) -> _CleanupLane:
    """The real authority service over the real adapter, on a `release` lane.

    Only the host IO beneath the coordinator is doubled, and only the repository and journal
    beneath the service. The commit path under test is production code end to end.
    """
    peer = AuthenticatedPeer(uuid4())
    takeover = _cleanup_takeover()
    repository = _Repository(peer, takeover)
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=_adapter(io),
    )
    return service, repository, peer, takeover


def _cleanup_mutation(operation_identity: str = "cleanup-1") -> AuthorityMutationRequestV1:
    return _request(
        purpose="release",
        operation=AuthorityOperation.CLEANUP,
        recovery_objects=(_owned_object(),),
    ).model_copy(update={"operation_identity": operation_identity, "attempt_id": uuid4()})


async def test_a_cleanup_commit_finalizes_the_tombstone_against_the_anchored_record(
    tmp_path: Path,
) -> None:
    """`finalize_cleanup_tombstone` gets a production caller, driven through the service.

    Calling `instance.commit` directly would bypass the service and prove nothing about the
    wiring, so this drives `execute_mutation` and compares the proof against the record the
    service actually wrote to the journal.
    """
    io = _FakeIO(_metadata("recovered"))
    service, repository, peer, takeover = _cleanup_service(io, tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True

    mutation = _cleanup_mutation()
    await service.execute_mutation(peer, mutation)

    assert io.actions.count("cleanup") == 1
    assert io.actions.count("finalize") == 1
    started = [
        record
        for record in repository.records
        if record.phase is JournalPhase.MUTATION_STARTED
        and record.operation_identity == mutation.operation_identity
    ][-1]
    proof = io.finalized_proof
    assert proof is not None
    assert proof.journal_sequence == started.sequence
    assert proof.journal_digest == record_digest(started)
    assert proof.operation_id == started.operation_identity
    assert proof.attempt_id == str(started.attempt_id)
    assert proof.phase == "mutation-started"
    assert proof.binding == _BINDING
    assert proof.point_digest == LocalLibvirtExternalBoot.point_digest(_point(io.metadata))

    # The tombstone remains observable until the authority has anchored the terminal receipt;
    # only then may finalization remove the recovery directory.
    terminal = repository.records[-1]
    assert terminal.phase is JournalPhase.TERMINAL
    assert terminal.outcome == "absent"
    assert terminal.observation is not None
    assert terminal.observation.category == "absent"


async def test_a_teardown_commit_finalizes_the_tombstone(tmp_path: Path) -> None:
    io = _FakeIO(_metadata("recovered"))
    peer = AuthenticatedPeer(uuid4())
    takeover = _cleanup_takeover().model_copy(
        update={"purpose": "teardown", "operation": AuthorityOperation.TEARDOWN}
    )
    repository = _Repository(peer, takeover)
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=_adapter(io),
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True

    await service.execute_mutation(
        peer,
        _request(
            purpose="teardown",
            operation=AuthorityOperation.TEARDOWN,
            recovery_objects=(_owned_object(),),
        ),
    )

    assert io.actions.count("cleanup") == 1
    assert io.actions.count("finalize") == 1


@pytest.mark.parametrize(
    "unresolvable",
    [
        # Every state in which the recovery record cannot be rebuilt raises the same error
        # from the same place. The point of the parametrisation is that none is special:
        # tombstone live, fully finalized, never prepared and prepare-interrupted are
        # indistinguishable here, so none may be treated as a completed cleanup.
        pytest.param(FileNotFoundError("intent.json"), id="record-absent"),
        pytest.param(FileNotFoundError("recovery directory"), id="directory-absent"),
        pytest.param(OSError("device busy"), id="unreadable"),
    ],
)
async def test_a_cleanup_commit_refuses_every_unresolvable_recovery_point(
    tmp_path: Path, unresolvable: BaseException
) -> None:
    """Absence never identifies its own cause, so it is never a success answer.

    The fault this catches is adding back any absence-derived success branch: whichever state
    it keyed on would return an observation here instead of raising, and the recovery-object
    deletion gate would be skipped for a peer-chosen binding.
    """
    io = _FakeIO(_metadata("recovered"))
    io.reopen_error = unresolvable
    service, repository, peer, takeover = _cleanup_service(io, tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True

    with pytest.raises(AuthorityServiceError) as caught:
        await service.execute_mutation(peer, _cleanup_mutation())

    assert caught.value.category == "provider_conflict"
    assert "cleanup" not in io.actions
    assert "finalize" not in io.actions


async def test_cleanup_restart_reconstructs_exact_durable_tombstone(tmp_path: Path) -> None:
    io = _FakeIO(_metadata("recovered"))
    io.tombstone = True
    io.intent_present = False
    service, repository, peer, takeover = _cleanup_service(io, tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True

    observation = await service.execute_mutation(peer, _cleanup_mutation())

    assert observation.category == "absent"
    assert "cleanup" not in io.actions
    assert io.actions.count("finalize") == 1
    assert repository.records[-1].phase is JournalPhase.TERMINAL
    assert repository.records[-1].outcome == "absent"


async def test_teardown_restart_reconstructs_exact_durable_tombstone(tmp_path: Path) -> None:
    io = _FakeIO(_metadata("recovered"))
    io.tombstone = True
    io.intent_present = False
    peer = AuthenticatedPeer(uuid4())
    takeover = _cleanup_takeover().model_copy(
        update={"purpose": "teardown", "operation": AuthorityOperation.TEARDOWN}
    )
    repository = _Repository(peer, takeover)
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=_adapter(io),
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    mutation = _request(
        purpose="teardown",
        operation=AuthorityOperation.TEARDOWN,
        recovery_objects=(_owned_object(),),
    )

    observation = await service.execute_mutation(peer, mutation)

    assert observation.category == "absent"
    assert "cleanup" not in io.actions
    assert io.actions.count("finalize") == 1


@pytest.mark.parametrize("operation", [AuthorityOperation.CLEANUP, AuthorityOperation.TEARDOWN])
async def test_deleting_operation_terminal_replay_is_idempotent(
    tmp_path: Path,
    operation: AuthorityOperation,
) -> None:
    purpose = "release" if operation is AuthorityOperation.CLEANUP else "teardown"
    io = _FakeIO(_metadata("recovered"))
    peer = AuthenticatedPeer(uuid4())
    takeover = _cleanup_takeover().model_copy(update={"purpose": purpose, "operation": operation})
    repository = _Repository(peer, takeover)
    service = ExternalBootAuthorityService(
        repository=repository,
        journal_factory=lambda system_id: FileAuthorityJournal(tmp_path, f"{system_id}.journal"),
        adapter=_adapter(io),
    )
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True
    mutation = _request(
        purpose=purpose,
        operation=operation,
        recovery_objects=(_owned_object(),),
    )

    first = await service.execute_mutation(peer, mutation)
    second = await service.execute_mutation(peer, mutation)

    assert second == first
    assert io.actions.count("cleanup") == 1
    assert io.actions.count("finalize") == 1


async def test_restart_finalization_refuses_a_malformed_cleanup_receipt(tmp_path: Path) -> None:
    io = _FakeIO(_metadata("recovered"))
    io.tombstone = True
    io.intent_present = False
    io.tombstone_error = ValueError("malformed cleanup receipt")
    service, repository, peer, takeover = _cleanup_service(io, tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True

    with pytest.raises(AuthorityServiceError) as caught:
        await service.execute_mutation(peer, _cleanup_mutation())

    assert caught.value.category == "provider_conflict"
    assert "cleanup" not in io.actions
    assert "finalize" not in io.actions


async def test_durable_receipt_reconstruction_is_required_after_restart(tmp_path: Path) -> None:
    io = _FakeIO(_metadata("recovered"))
    io.tombstone = True
    io.intent_present = False
    io.tombstone_error = FileNotFoundError("receipt path disabled by controlled fault")
    service, repository, peer, takeover = _cleanup_service(io, tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True

    with pytest.raises(AuthorityServiceError) as caught:
        await service.execute_mutation(peer, _cleanup_mutation())

    assert caught.value.category == "provider_conflict"
    assert io.tombstone is True
    assert "cleanup" not in io.actions
    assert "finalize" not in io.actions


async def test_a_cleanup_commit_finishes_an_interrupted_tombstone_without_recleaning(
    tmp_path: Path,
) -> None:
    """Positive-evidence idempotency, on the durable tombstone rather than on an absence.

    The state is the crash window inside `publish_tombstone`: the tombstone is written and
    `intent.json` is not yet unlinked, so the record is still resolvable. Two things must
    hold, and both are production behaviour rather than fake behaviour.

    No second provider mutation: the coordinator's `cleanup_complete` early return fires.
    Removing that early return makes a second `cleanup` action appear.

    Finalization is required after the terminal record is anchored. The real store validates
    and removes only matching producer residue before removing the tombstone.
    """
    io = _FakeIO(_metadata("recovered"))
    io.tombstone = True  # a cleanup that published its tombstone and did not finalize
    io.intent_present = True  # ...and was interrupted before unlinking the record
    service, repository, peer, takeover = _cleanup_service(io, tmp_path)
    await service.acknowledge_takeover(peer, takeover)
    repository.current = True

    await service.execute_mutation(peer, _cleanup_mutation())

    assert "cleanup" not in io.actions
    assert io.actions.count("finalize") == 1
