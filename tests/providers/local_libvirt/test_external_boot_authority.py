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
from uuid import UUID

import pytest

from kdive.providers.external_boot_authority.protocol import (
    AuthorityMutationRequestV1,
    AuthorityOperation,
    RecoveryObjectBindingV1,
    operation_is_permitted,
)
from kdive.providers.external_boot_authority.service import (
    AuthorityMutationAdapter,
    AuthorityServiceError,
)
from kdive.providers.local_libvirt import external_boot_authority as adapter_module
from kdive.providers.local_libvirt.external_boot_authority import (
    LocalExternalBootAuthorityAdapter,
)
from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
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
    OpaqueProviderRef,
    PresentComponentState,
    ProviderStateIdentity,
    RecoveryPoint,
    RunningKernelObservation,
)

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
        target_xml=_SOURCE_XML.replace("/old", "/new"),
        expected_running=RunningKernelObservation(
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
    ) -> None:
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

    # -- LocalExternalBootIO -------------------------------------------------------
    def open(self, authority: OpaqueProviderRef, expected: object) -> _FakeContext:
        del expected
        self.actions.append(f"open:{authority.ref}")
        return _FakeContext(self)

    def finalize_tombstone(self, recovery: RecoveryPoint, proof: object) -> None:
        del recovery, proof
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
        if self.reopen_fault:
            raise LookupError("libguestfs: /var/lib/kdive/secret.key unreadable")
        return self.metadata

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
        self.record_phase(metadata, "target-defined")

    def observe_running(self, metadata: LocalRecoveryMetadataV1) -> RunningKernelObservation:
        self.actions.append("observe-running")
        return metadata.expected_running

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


@pytest.mark.parametrize("purpose", ["activate", "recover", "resolve-conflict", "release"])
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
        await _adapter(io).commit(request, operation.value)

    assert caught.value.category == "provider_conflict"
    assert io.actions == []


async def test_commit_refuses_a_commit_point_that_is_not_an_operation_at_all() -> None:
    io = _FakeIO()

    with pytest.raises(AuthorityServiceError) as caught:
        await _adapter(io).commit(_request(), "rm -rf /")

    assert caught.value.category == "provider_conflict"
    assert io.actions == []


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

    await _adapter(io).commit(_request(), AuthorityOperation.ACTIVATE.value)

    assert "activate-modules" in io.actions
    assert "define-target" in io.actions


async def test_recover_drives_the_named_recovery_primitives() -> None:
    io = _FakeIO(_metadata("target-defined"))
    request = _request(purpose="recover", operation=AuthorityOperation.RECOVER)

    await _adapter(io).commit(request, AuthorityOperation.RECOVER.value)

    assert "recover-modules" in io.actions
    assert "define-source" in io.actions
    assert "restore-power" in io.actions


async def test_bookkeeping_operations_mutate_nothing() -> None:
    io = _FakeIO(_metadata("target-defined"))
    request = _request(purpose="recover", operation=AuthorityOperation.DEADLINE)

    await _adapter(io).commit(request, AuthorityOperation.DEADLINE.value)

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
        await _adapter(io).commit(request, AuthorityOperation.ACTIVATE.value)

    assert caught.value.category == "provider_conflict"
    assert "activate-modules" not in io.actions


async def test_commit_refuses_a_mismatched_intended_target_identity() -> None:
    io = _FakeIO()
    request = _request(intended_target="sha256:" + "f" * 64)

    with pytest.raises(AuthorityServiceError) as caught:
        await _adapter(io).commit(request, AuthorityOperation.ACTIVATE.value)

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
            _request(plan_identity="sha256:" + "f" * 64), AuthorityOperation.ACTIVATE.value
        )

    assert "activate-modules" not in io.actions


# --------------------------------------------------------------------------------------
# AC 6 - stable recovery-object ownership and quarantine
# --------------------------------------------------------------------------------------


async def test_owned_recovery_objects_keep_ownership_across_observe_and_commit() -> None:
    io = _FakeIO()
    request = _request(recovery_objects=(_owned_object(),))

    observed = await _adapter(io).observe(request)
    committed = await _adapter(io).commit(request, AuthorityOperation.ACTIVATE.value)

    assert observed.category == "source"
    assert committed.category in {"source", "target", "mixed"}


async def test_unproven_recovery_object_is_quarantined_not_reused_or_deleted() -> None:
    io = _FakeIO()
    request = _request(recovery_objects=(_foreign_object(),))

    observation = await _adapter(io).observe(request)
    with pytest.raises(AuthorityServiceError) as caught:
        await _adapter(io).commit(request, AuthorityOperation.ACTIVATE.value)

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
        await _adapter(io).commit(hostile, AuthorityOperation.ACTIVATE.value)

    assert "activate-modules" not in io.actions


# --------------------------------------------------------------------------------------
# AC 8 - bounded categories carrying no provider output
# --------------------------------------------------------------------------------------


async def test_provider_failure_becomes_a_bounded_category_without_provider_output() -> None:
    io = _FakeIO(reopen_fault=True)

    with pytest.raises(AuthorityServiceError) as caught:
        await _adapter(io).commit(_request(), AuthorityOperation.ACTIVATE.value)

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
        await _adapter(io).commit(_request(), AuthorityOperation.ACTIVATE.value)

    assert caught.value.category in bounded


# --------------------------------------------------------------------------------------
# AC 9 - stale generation and partial commit
# --------------------------------------------------------------------------------------


async def test_a_generation_behind_the_admitted_watermark_is_superseded() -> None:
    io = _FakeIO()
    instance = _adapter(io)
    await instance.commit(_request(generation=9), AuthorityOperation.ACTIVATE.value)
    io.actions.clear()

    with pytest.raises(AuthorityServiceError) as caught:
        await instance.commit(_request(generation=8), AuthorityOperation.ACTIVATE.value)

    assert caught.value.category == "superseded"
    assert io.actions == []


async def test_an_interrupted_commit_stays_observable_and_does_not_double_apply() -> None:
    io = _FakeIO(_metadata("pre-stop-intent"))
    instance = _adapter(io)

    await instance.commit(_request(), AuthorityOperation.ACTIVATE.value)
    first = list(io.actions)
    io.actions.clear()
    await instance.commit(_request(), AuthorityOperation.ACTIVATE.value)

    assert first.count("activate-modules") == 1
    # The durable phase advanced to target-defined, so the retry re-reads and returns
    # without republishing the module tree or redefining the domain.
    assert "activate-modules" not in io.actions
    assert "define-target" not in io.actions
    assert (await instance.observe(_request())).category in {
        "source",
        "target",
        "mixed",
        "conflict",
    }
