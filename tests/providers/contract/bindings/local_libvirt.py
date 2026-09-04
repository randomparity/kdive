"""Local-libvirt's registration in the shared external-boot contract suite.

The real ``LocalLibvirtExternalBoot`` coordinator runs; only ``LocalExternalBootIO`` —
the libvirt connection, libguestfs guest tree, and durable recovery store beneath it — is
doubled, so the suite exercises provider code rather than a stub of it. Nothing in
``tests/providers/contract/`` changed to admit this provider.
"""

from __future__ import annotations

import hashlib
from typing import cast

from kdive.providers.local_libvirt.lifecycle.boot.external_boot import (
    LocalExternalBootIO,
    LocalLibvirtExternalBoot,
    LocalObservedState,
    LocalRecoveryMetadataV1,
    RecoveryPhase,
)
from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    ExternalBootPorts,
    KernelIdentity,
    MaterializedArtifacts,
    OpaqueProviderRef,
    PresentComponentState,
    ProviderStateIdentity,
    RecoveryPoint,
    RunningKernelObservation,
)
from tests.providers.contract.plans import ACTIVATION_ID, RELEASE, sample_plan_data
from tests.providers.contract.registry import ProviderBinding

_SOURCE_XML = "<domain type='kvm'><name>d</name><devices><disk src='/old'/></devices></domain>"
_TARGET_XML = _SOURCE_XML.replace("/old", "/new")
_SOURCE_BOOT = "sha256:" + "b" * 64
_TARGET_BOOT = "sha256:" + "c" * 64
_IDENTITY = KernelIdentity(architecture="x86_64", release=RELEASE, gnu_build_id="01020304")
_OBSERVATION = RunningKernelObservation(
    identity=_IDENTITY,
    cmdline=b"root=UUID=x",
    expected_cmdline=b"root=UUID=x",
)


def _materialization(plan: ExternalBootPlan) -> ExternalBootMaterialization:
    return ExternalBootMaterialization(
        architecture=plan.architecture,
        provider_kind="local-libvirt",
        ownership={
            "system_id": plan.ownership.system_id,
            "run_id": plan.ownership.run_id,
        },
        plan_identity=plan.identity,
        extracted_vmlinuz_sha256=plan.bundle.vmlinuz_sha256,
        source_module_manifest=plan.module_obligation.source_manifest,
        installed_module_tree="sha256:" + "3" * 64,
        verified_bundle_sha256=plan.bundle.sha256,
        verified_initrd_sha256=None,
        kernel_observation=_IDENTITY,
        artifacts=MaterializedArtifacts(
            kernel=OpaqueProviderRef(ref="artifacts/kernel"),
            modules=OpaqueProviderRef(ref="artifacts/modules"),
            initrd=None,
        ),
    )


class _ContractIO:
    """A durable-store and host double for one activation's lifecycle."""

    def __init__(self) -> None:
        self.metadata: LocalRecoveryMetadataV1 | None = None
        self.tombstone = False
        # `publish_tombstone` unlinks `intent.json`, so the real store cannot rebuild the
        # record after a cleanup. Tracked here so this double is not more permissive than the
        # store it stands in for.
        self.record_unlinked = False

    # -- LocalExternalBootIO -------------------------------------------------------
    def open(self, authority: OpaqueProviderRef, expected: object) -> _ContractContext:
        del authority, expected
        return _ContractContext(self)

    def finalize_tombstone(self, recovery: RecoveryPoint, proof: object) -> None:
        del recovery, proof

    # -- LocalExternalBootOperation ------------------------------------------------
    def materialize(self, plan: ExternalBootPlan) -> ExternalBootMaterialization:
        return _materialization(plan)

    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
    ) -> LocalRecoveryMetadataV1:
        self.metadata = LocalRecoveryMetadataV1(
            binding=binding,
            plan_identity=materialization.plan_identity,
            materialization_identity=materialization.identity,
            release=RELEASE,
            materialized_modules=OpaqueProviderRef(ref="artifacts/modules"),
            materialized_modules_sha256="sha256:" + "8" * 64,
            materialized_modules_bytes=123,
            source_xml_sha256="sha256:" + hashlib.sha256(_SOURCE_XML.encode()).hexdigest(),
            source_xml=_SOURCE_XML,
            source_definition="sha256:" + "a" * 64,
            source_boot=_SOURCE_BOOT,
            target_boot=_TARGET_BOOT,
            target_projection_sha256="sha256:" + "d" * 64,
            target_xml_sha256="sha256:" + hashlib.sha256(_TARGET_XML.encode()).hexdigest(),
            target_xml=_TARGET_XML,
            expected_running=materialization.kernel_observation,
            source_state=ProviderStateIdentity(
                definition=_SOURCE_BOOT, modules=AbsentComponentState()
            ),
            target_state=ProviderStateIdentity(
                definition=_TARGET_BOOT,
                modules=PresentComponentState(manifest=materialization.installed_module_tree),
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

    def reopen(self, recovery: RecoveryPoint) -> LocalRecoveryMetadataV1:
        del recovery
        return self._required()

    def reopen_binding(self, binding: ExternalBootActivationBinding) -> LocalRecoveryMetadataV1:
        del binding
        return self._required()

    def observe_state(self, metadata: LocalRecoveryMetadataV1) -> LocalObservedState:
        return LocalObservedState(
            definition=metadata.source_state.definition,
            modules=metadata.source_state.modules,
            active=False,
        )

    def activate_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.record_phase(metadata, "module-restored")

    def define_target(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.record_phase(metadata, "target-defined")

    def observe_running(self, metadata: LocalRecoveryMetadataV1) -> RunningKernelObservation:
        return _OBSERVATION

    def recover_modules(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.record_phase(metadata, "module-restored")

    def define_source(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.record_phase(metadata, "source-restored")

    def restore_power(self, metadata: LocalRecoveryMetadataV1) -> None:
        self.record_phase(metadata, "recovered")

    def record_phase(
        self, metadata: LocalRecoveryMetadataV1, phase: RecoveryPhase
    ) -> LocalRecoveryMetadataV1:
        self.metadata = metadata.model_copy(update={"phase": phase})
        return self.metadata

    def cleanup_complete(self, recovery: RecoveryPoint) -> bool:
        del recovery
        return self.tombstone

    def cleanup(self, metadata: LocalRecoveryMetadataV1, point_digest: str) -> None:
        del point_digest
        self.record_phase(metadata, "cleaned")
        self.tombstone = True
        self.record_unlinked = True

    def _required(self) -> LocalRecoveryMetadataV1:
        if self.record_unlinked:
            raise FileNotFoundError("intent.json")
        if self.metadata is None:
            raise LookupError("no prepared external-boot recovery record")
        return self.metadata


class _ContractContext:
    def __init__(self, io: _ContractIO) -> None:
        self._io = io

    def __enter__(self) -> _ContractIO:
        return self._io

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback


def _build() -> ExternalBootPorts:
    return LocalLibvirtExternalBoot(cast(LocalExternalBootIO, _ContractIO()))


def _plan() -> ExternalBootPlan:
    return ExternalBootPlan.model_validate(sample_plan_data())


def _activation(
    materialization: ExternalBootMaterialization,
) -> ExternalBootActivationBinding:
    return ExternalBootActivationBinding(
        system_id=materialization.ownership.system_id,
        run_id=materialization.ownership.run_id,
        activation_id=ACTIVATION_ID,
    )


def _authority() -> OpaqueProviderRef:
    return OpaqueProviderRef(ref="authority/current")


BINDING = ProviderBinding(
    name="local-libvirt",
    build=_build,
    plan=_plan,
    activation=_activation,
    authority=_authority,
)
