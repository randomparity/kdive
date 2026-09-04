"""In-memory non-libvirt consumer of the external-boot ports (ADR-0583)."""

from __future__ import annotations

from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    MaterializedArtifacts,
    OpaqueProviderRef,
    PresentComponentState,
    ProviderStateIdentity,
    RecoveryPoint,
    RunningKernelObservation,
)


class FaultInjectExternalBoot:
    """Deterministic contract consumer with no hypervisor or transport types."""

    def __init__(self) -> None:
        self._observations: dict[str, RunningKernelObservation] = {}
        self._cmdlines: dict[str, bytes] = {}

    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef
    ) -> ExternalBootMaterialization:
        del authority
        suffix = plan.identity.removeprefix("sha256:")
        materialization = ExternalBootMaterialization(
            architecture=plan.architecture,
            provider_kind="fault-inject",
            ownership={
                "system_id": plan.ownership.system_id,
                "run_id": plan.ownership.run_id,
            },
            plan_identity=plan.identity,
            extracted_vmlinuz_sha256=plan.bundle.vmlinuz_sha256,
            source_module_manifest=plan.module_obligation.source_manifest,
            installed_module_tree=plan.module_obligation.source_manifest,
            verified_bundle_sha256=plan.bundle.sha256,
            verified_initrd_sha256=plan.initrd.sha256 if plan.initrd else None,
            kernel_observation={
                "architecture": plan.architecture,
                "release": plan.module_obligation.release,
                "gnu_build_id": "00" * plan.bundle.gnu_build_id_size_bytes,
            },
            artifacts=MaterializedArtifacts(
                kernel=OpaqueProviderRef(ref=f"kernel/{suffix}"),
                modules=OpaqueProviderRef(ref=f"modules/{suffix}"),
                initrd=OpaqueProviderRef(ref=f"initrd/{suffix}") if plan.initrd else None,
            ),
        )
        self._cmdlines[materialization.identity] = plan.cmdline.encode()
        return materialization

    def prepare(
        self,
        materialization: ExternalBootMaterialization,
        binding: ExternalBootActivationBinding,
        authority: OpaqueProviderRef,
    ) -> RecoveryPoint:
        del authority
        if (
            binding.system_id != materialization.ownership.system_id
            or binding.run_id != materialization.ownership.run_id
        ):
            raise ValueError("activation binding does not match materialization ownership")
        recovery_ref = OpaqueProviderRef(
            ref=f"recovery/{materialization.identity.removeprefix('sha256:')}"
        )
        point = RecoveryPoint(
            binding=binding,
            plan_identity=materialization.plan_identity,
            materialization_identity=materialization.identity,
            recovery_ref=recovery_ref,
            source_state=ProviderStateIdentity(
                definition=materialization.plan_identity,
                modules=AbsentComponentState(),
            ),
            target_state=ProviderStateIdentity(
                definition=materialization.identity,
                modules=PresentComponentState(manifest=materialization.installed_module_tree),
            ),
        )
        cmdline = self._cmdlines[materialization.identity]
        self._observations[recovery_ref.ref] = RunningKernelObservation(
            identity=materialization.kernel_observation,
            cmdline=cmdline,
            expected_cmdline=cmdline,
        )
        return point

    def activate(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        del recovery, authority

    def observe(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> RunningKernelObservation:
        del authority
        return self._observations[recovery.recovery_ref.ref]

    def recover(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        del recovery, authority

    def cleanup(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        del authority
        self._observations.pop(recovery.recovery_ref.ref, None)
        self._cmdlines.pop(recovery.materialization_identity, None)
