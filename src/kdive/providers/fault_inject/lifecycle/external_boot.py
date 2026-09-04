"""In-memory non-libvirt consumer of the external-boot ports (ADR-0583)."""

from __future__ import annotations

from kdive.providers.ports.external_boot import (
    AbsentComponentState,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    ExternalBootPreparationObservation,
    ExternalBootPreparationRequest,
    MaterializedArtifacts,
    OpaqueProviderRef,
    PresentComponentState,
    ProviderStateIdentity,
    RecoveryPoint,
    RunningKernelObservation,
)


class PreparationInterrupted(RuntimeError):
    """Test-only process-loss boundary after a durable preparation receipt."""


class FaultInjectExternalBoot:
    """Deterministic contract consumer with no hypervisor or transport types."""

    def __init__(self) -> None:
        self._observations: dict[str, RunningKernelObservation] = {}
        self._preparation_receipts: dict[tuple[str, str], ExternalBootPreparationObservation] = {}
        self._interrupt_after_receipt: set[str] = set()
        self.preparation_mutations = {"materialize": 0, "prepare": 0}

    def interrupt_after_receipt(self, phase: str) -> None:
        """Arm a one-shot interruption after ``phase`` publishes its receipt."""
        if phase not in self.preparation_mutations:
            raise ValueError("preparation phase must be materialize or prepare")
        self._interrupt_after_receipt.add(phase)

    def observe_preparation(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootPreparationObservation:
        receipt = self._preparation_receipts.get((request.binding.activation_id, request.phase))
        if receipt is not None:
            if (
                receipt.binding != request.binding
                or receipt.plan_identity != request.plan.identity
                or receipt.authority != request.authority
                or receipt.operation_identity != request.operation_identity
            ):
                raise ValueError("preparation receipt identity conflicts with request")
            return receipt
        return ExternalBootPreparationObservation(
            state="absent",
            binding=request.binding,
            plan_identity=request.plan.identity,
            authority=request.authority,
            operation_identity=request.operation_identity,
        )

    def execute_preparation(
        self, request: ExternalBootPreparationRequest
    ) -> ExternalBootPreparationObservation:
        observed = self.observe_preparation(request)
        if observed.state != "absent":
            return observed
        self.preparation_mutations[request.phase] += 1
        if request.phase == "materialize":
            materialization = self.materialize(request.plan, request.authority)
            receipt = observed.model_copy(
                update={"state": "materialized", "materialization": materialization}
            )
        else:
            materialize_receipt = self._preparation_receipts.get(
                (request.binding.activation_id, "materialize")
            )
            if materialize_receipt is None or materialize_receipt.materialization is None:
                raise ValueError("prepare requires a durable materialization receipt")
            materialization = materialize_receipt.materialization
            recovery_point = self.prepare(materialization, request.binding, request.authority)
            receipt = ExternalBootPreparationObservation(
                state="prepared",
                binding=request.binding,
                plan_identity=request.plan.identity,
                authority=request.authority,
                operation_identity=request.operation_identity,
                materialization=materialization,
                recovery_point=recovery_point,
            )
        self._preparation_receipts[(request.binding.activation_id, request.phase)] = receipt
        if request.phase in self._interrupt_after_receipt:
            self._interrupt_after_receipt.remove(request.phase)
            raise PreparationInterrupted(f"interrupted after {request.phase} receipt")
        return receipt

    def materialize(
        self, plan: ExternalBootPlan, authority: OpaqueProviderRef
    ) -> ExternalBootMaterialization:
        del authority
        suffix = plan.identity.removeprefix("sha256:")
        return ExternalBootMaterialization(
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
        self._observations[recovery_ref.ref] = materialization.kernel_observation
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
