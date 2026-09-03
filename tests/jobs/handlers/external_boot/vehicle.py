"""The execution vehicle every Postgres test in this package runs on.

"Against the fault-inject port" is not constructible by handing a handler a fresh
``FaultInjectExternalBoot``, and three independent mechanisms make it so. All three are this
module's job to solve, and none of them weakens ADR-0593 decision 4.

1. **``observe`` answers only for a recovery point ``prepare`` produced.**
   ``FaultInjectExternalBoot.observe`` returns ``self._observations[recovery.recovery_ref.ref]``
   and ``_observations`` is written in exactly one place — ``prepare``. Four of the six operations
   route through ``observe``, so a port whose ``_observations`` is empty raises ``KeyError`` on all
   four. Seeding an activation row straight into Postgres does not populate it.

2. **No composed runtime binds that port under an admissible ``provider_kind``.** The only runtime
   carrying ``FaultInjectExternalBoot`` is the fault-inject composition, whose kind is
   ``ResourceKind.FAULT_INJECT`` — a value the marker's ``provider_kind`` cannot hold and
   ``allocate_external_boot_authority`` rejects. So the port is bound under
   ``ResourceKind.LOCAL_LIBVIRT`` instead: the fault-inject *port* without the fault-inject *kind*.

3. **A persisted recovery point is bound to its activation row by a CHECK, so the ids cannot be
   minted independently.** ``0124_external_boot_activation_binding.sql:96-111`` requires a non-NULL
   ``recovery_point`` to carry a ``binding`` object of exactly three keys whose UUIDs equal the
   row's ``system_id``, ``run_id`` and ``id``, to carry **no** ``ownership`` key, and to have a
   matching ``plan_identity``; ``:92-95`` requires the same of ``materialization.ownership``. Every
   one of those values is fixed by the port from its inputs, so a seeder that mints its own ids and
   a chosen ``plan_identity`` constant cannot agree with them and the INSERT fails with a
   ``CheckViolation`` before any handler runs.

**So the order below is forced, not a style choice:** mint the ids first, derive the plan from
them, drive the port through ``materialize`` then ``prepare`` out of band, and only then write the
resulting objects' canonical JSON into the row. The port handed to the handler is that **same
instance**, wrapped so ``materialize`` and ``prepare`` raise — the pin ADR-0593 decision 4 asks for
is about the *handler*, so the fixture performing those two calls before the wrapper is installed
is exactly the disposition rather than a hole in it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID, uuid4

from kdive.providers.fault_inject.lifecycle.external_boot import FaultInjectExternalBoot
from kdive.providers.ports.external_boot import (
    BundleSource,
    ExternalBootActivationBinding,
    ExternalBootMaterialization,
    ExternalBootPlan,
    InitrdSource,
    ModuleObligation,
    OpaqueProviderRef,
    PlanOwnership,
    RecoveryPoint,
    RootSource,
    RootSpecV1,
    RunningKernelObservation,
)

_SHA = "sha256:" + "11" * 32
_INITRD_SHA = "sha256:" + "22" * 32
_MANIFEST = "sha256:" + "33" * 32
_ROOT_IDENTITY = "sha256:" + "55" * 32


def synthetic_plan(*, system_id: UUID, run_id: UUID) -> ExternalBootPlan:
    """An ``ExternalBootPlan`` owned by ``system_id``/``run_id``.

    Modelled on ``tests/providers/remote_libvirt/lifecycle/test_external_boot.py``, the repo's only
    existing ``ExternalBootPlan(...)`` construction. Deliberately **not** modelled on ADR-0583's
    golden vector: that vector is a libvirt domain-XML identity under the hash prefix
    ``kdive-libvirt-boot-projection-v1``, whereas ``ExternalBootPlan.identity`` uses
    ``kdive-external-boot-plan-v1``, and ADR-0583 carries no plan example at all. A hand-rolled plan
    is easy to get rejected: ``_validate_composed_plan`` requires the root arguments to occur
    exactly once in ``platform_arguments``, ``cmdline`` to compose them exactly, unique argument
    keys, and a 2047-byte cap.
    """
    arguments = ("root=/dev/vda1", "console=ttyS0")
    return ExternalBootPlan(
        schema="external-boot-plan-v1",
        architecture="x86_64",
        ownership=PlanOwnership(
            system_id=str(system_id), run_id=str(run_id), build_generation=str(uuid4())
        ),
        bundle=BundleSource(
            key="builds/kernel.tar",
            version="v1",
            sha256=_SHA,
            vmlinuz_sha256=_SHA,
            member_count=12,
            uncompressed_bytes=4096,
            vmlinuz_size_bytes=2048,
            decoded_kernel_size_bytes=4096,
            elf_metadata_bytes=512,
            gnu_build_id_size_bytes=8,
        ),
        initrd=InitrdSource(
            key="builds/initrd.img", version="v1", sha256=_INITRD_SHA, size_bytes=1024
        ),
        cmdline="root=/dev/vda1 console=ttyS0",
        debug_cmdline=None,
        platform_arguments=arguments,
        module_obligation=ModuleObligation(
            release="6.9.0-kdive", source_manifest=_MANIFEST, member_count=3, uncompressed_bytes=64
        ),
        root=RootSpecV1(
            schema="root-spec-v1",
            architecture="x86_64",
            root="/dev/vda1",
            arguments=("root=/dev/vda1",),
            authority="stage-inspection",
            source=RootSource(kind="staged-image", identity=_ROOT_IDENTITY),
        ),
    )


class GuardedExternalBoot:
    """Delegates the four worker operations and refuses the two the handler must never perform.

    ADR-0593 decision 4 dispositions ``materialize`` and ``prepare`` as *prepared-before-admission*:
    preconditions the handlers verify and consume, never operations they perform. This wrapper is
    what makes that a test failure rather than a docstring — and the assertion cannot be satisfied
    by a test forgetting to check, because the raise comes from the port itself.
    """

    def __init__(self, inner: FaultInjectExternalBoot) -> None:
        self._inner = inner
        self.calls: list[str] = []
        self.recoveries: list[RecoveryPoint] = []

    def materialize(self, *_args: object, **_kwargs: object) -> ExternalBootMaterialization:
        raise AssertionError("a worker handler must never call materialize (ADR-0593 decision 4)")

    def prepare(self, *_args: object, **_kwargs: object) -> RecoveryPoint:
        raise AssertionError("a worker handler must never call prepare (ADR-0593 decision 4)")

    def activate(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        self.calls.append("activate")
        self.recoveries.append(recovery)
        self._inner.activate(recovery, authority)

    def observe(
        self, recovery: RecoveryPoint, authority: OpaqueProviderRef
    ) -> RunningKernelObservation:
        self.calls.append("observe")
        self.recoveries.append(recovery)
        return self._inner.observe(recovery, authority)

    def recover(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        self.calls.append("recover")
        self.recoveries.append(recovery)
        self._inner.recover(recovery, authority)

    def cleanup(self, recovery: RecoveryPoint, authority: OpaqueProviderRef) -> None:
        self.calls.append("cleanup")
        self.recoveries.append(recovery)
        self._inner.cleanup(recovery, authority)


@dataclass(frozen=True, slots=True)
class Vehicle:
    """The ids, the persisted evidence, and the port the handler is handed."""

    system_id: UUID
    run_id: UUID
    activation_id: UUID
    plan: ExternalBootPlan
    materialization: ExternalBootMaterialization
    recovery_point: RecoveryPoint
    port: GuardedExternalBoot

    @property
    def plan_identity(self) -> str:
        return self.plan.identity

    @property
    def materialization_json(self) -> dict[str, object]:
        return _canonical(self.materialization)

    @property
    def recovery_point_json(self) -> dict[str, object]:
        return _canonical(self.recovery_point)


def _canonical(value: ExternalBootMaterialization | RecoveryPoint) -> dict[str, object]:
    """The exact JSON the port produced, by alias, so the row round-trips back into the model."""
    return json.loads(value.model_dump_json(by_alias=True))


def build_vehicle(
    *, system_id: UUID | None = None, run_id: UUID | None = None, activation_id: UUID | None = None
) -> Vehicle:
    """Mint the ids, derive the plan, and drive the port through materialize then prepare."""
    system_id = system_id or uuid4()
    run_id = run_id or uuid4()
    activation_id = activation_id or uuid4()

    plan = synthetic_plan(system_id=system_id, run_id=run_id)
    inner = FaultInjectExternalBoot()
    authority = OpaqueProviderRef(ref="fixture/seed")
    materialization = inner.materialize(plan, authority)
    recovery_point = inner.prepare(
        materialization,
        ExternalBootActivationBinding(
            system_id=str(system_id), run_id=str(run_id), activation_id=str(activation_id)
        ),
        authority,
    )
    return Vehicle(
        system_id=system_id,
        run_id=run_id,
        activation_id=activation_id,
        plan=plan,
        materialization=materialization,
        recovery_point=recovery_point,
        port=GuardedExternalBoot(inner),
    )
