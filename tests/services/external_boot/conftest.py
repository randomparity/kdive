"""Seeded external-boot activations for the admission tests (ADR-0583).

`tests/db/external_boot_authority_support.py` seeds activation rows too, but only against
the SQL CHECK constraints: its evidence blobs carry `schema`, ownership, and plan identity
alone. `get_restricting_for_system` validates the row through `ExternalBootActivation`, so
these tests need evidence that satisfies the model as well. `build_activation` therefore
builds the domain object first and the fixture inserts its columns, which makes one
construction answer both sets of invariants.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

import pytest
from psycopg import AsyncConnection
from psycopg.types.json import Jsonb
from pydantic import BaseModel

from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.external_boot_activation import (
    ExternalBootActivation,
    ExternalBootCleanupEvidenceV1,
    ExternalBootConflictEvidenceV1,
    ExternalBootTeardownEvidenceV1,
    ExternalBootTerminalEvidenceV1,
)
from kdive.providers.ports.external_boot import ExternalBootMaterialization, RecoveryPoint

_AT = datetime(2026, 8, 28, tzinfo=UTC)
_PLAN = "sha256:" + "a" * 64
_DIGEST = "sha256:" + "b" * 64
_TARGET = "sha256:" + "c" * 64

_UNMATERIALIZED = frozenset(
    {ExternalBootActivationState.PREPARING, ExternalBootActivationState.ABANDONED}
)
_DEADLINE_STATES = frozenset(
    {ExternalBootActivationState.ACTIVATING, ExternalBootActivationState.ACTIVE}
)
type _Outcome = Literal["active", "abandoned", "recovered", "recovery_failed"]

_TERMINAL_OUTCOMES: dict[ExternalBootActivationState, _Outcome] = {
    ExternalBootActivationState.ACTIVE: "active",
    ExternalBootActivationState.ABANDONED: "abandoned",
}
_ATTEMPT_STATES = {
    ExternalBootActivationState.RECOVERING: "recovering",
    ExternalBootActivationState.RECOVERY_CONFLICT: "conflict",
    ExternalBootActivationState.RECOVERY_FAILED: "failed",
    ExternalBootActivationState.RECOVERED: "recovered",
}
_ATTEMPT_OUTCOMES: dict[ExternalBootActivationState, _Outcome] = {
    ExternalBootActivationState.RECOVERY_FAILED: "recovery_failed",
    ExternalBootActivationState.RECOVERED: "recovered",
}
_ORDINARY_CLEANUP = frozenset(
    {ExternalBootActivationState.RECOVERED, ExternalBootActivationState.ABANDONED}
)

_ACTIVATION_COLUMNS = (
    "id",
    "system_id",
    "run_id",
    "plan_identity",
    "operation_owner_id",
    "authority_generation",
    "state",
    "cleanup_complete",
    "activation_readiness_deadline",
    "materialization",
    "recovery_point",
    "terminal_evidence",
    "teardown_evidence",
    "cleanup_evidence",
    "current_attempt_id",
)


@dataclass(frozen=True, slots=True)
class SeededActivation:
    """The identities a seeded activation binds, for assertions and follow-up calls."""

    activation: ExternalBootActivation
    system_id: UUID
    run_id: UUID


class SeedActivation(Protocol):
    """Insert one activation, its System, and its Run into a migrated database."""

    async def __call__(
        self,
        conn: AsyncConnection,
        *,
        state: ExternalBootActivationState,
        cleanup_complete: bool = False,
        ready_reservation: bool = False,
        system_id: UUID | None = None,
        run_id: UUID | None = None,
    ) -> SeededActivation: ...


def _materialization(system_id: UUID, run_id: UUID) -> ExternalBootMaterialization:
    return ExternalBootMaterialization.model_validate(
        {
            "architecture": "x86_64",
            "provider_kind": "remote-libvirt",
            "ownership": {"system_id": str(system_id), "run_id": str(run_id)},
            "plan_identity": _PLAN,
            "extracted_vmlinuz_sha256": _DIGEST,
            "source_module_manifest": _DIGEST,
            "installed_module_tree": _DIGEST,
            "verified_bundle_sha256": _DIGEST,
            "verified_initrd_sha256": None,
            "kernel_observation": {
                "architecture": "x86_64",
                "release": "6.12.0",
                "gnu_build_id": "deadbeef",
            },
            "artifacts": {
                "kernel": {"ref": "objects/kernel"},
                "modules": {"ref": "objects/modules"},
                "initrd": None,
            },
        }
    )


def _recovery_point(
    activation_id: UUID,
    system_id: UUID,
    run_id: UUID,
    materialization: ExternalBootMaterialization,
) -> RecoveryPoint:
    return RecoveryPoint.model_validate(
        {
            "binding": {
                "system_id": str(system_id),
                "run_id": str(run_id),
                "activation_id": str(activation_id),
            },
            "plan_identity": _PLAN,
            "materialization_identity": materialization.identity,
            "recovery_ref": {"ref": "objects/recovery"},
            "source_state": {"definition": _DIGEST, "modules": {"state": "absent"}},
            "target_state": {
                "definition": _TARGET,
                "modules": {"state": "present", "manifest": _DIGEST},
            },
        }
    )


def _cleanup_evidence(
    activation_id: UUID,
    system_id: UUID,
    state: ExternalBootActivationState,
) -> tuple[ExternalBootCleanupEvidenceV1, ExternalBootTeardownEvidenceV1 | None]:
    teardown = (
        None
        if state in _ORDINARY_CLEANUP
        else ExternalBootTeardownEvidenceV1(system_id=system_id, observed_at=_AT)
    )
    cleanup = ExternalBootCleanupEvidenceV1(
        activation_id=activation_id,
        system_id=system_id,
        release_identity=_DIGEST,
        mode="ordinary" if teardown is None else "system_teardown",
        teardown_identity=None if teardown is None else teardown.identity,
        completed_at=_AT,
    )
    return cleanup, teardown


def build_activation(
    *,
    activation_id: UUID,
    system_id: UUID,
    run_id: UUID,
    state: ExternalBootActivationState,
    cleanup_complete: bool = False,
) -> ExternalBootActivation:
    """Build a valid activation in ``state`` carrying exactly the evidence that state needs."""
    materialization = None if state in _UNMATERIALIZED else _materialization(system_id, run_id)
    outcome = _TERMINAL_OUTCOMES.get(state)
    cleanup, teardown = (
        _cleanup_evidence(activation_id, system_id, state) if cleanup_complete else (None, None)
    )
    return ExternalBootActivation(
        id=activation_id,
        system_id=system_id,
        run_id=run_id,
        plan_identity=_PLAN,
        operation_owner_id=uuid4(),
        authority_generation=7,
        state=state,
        cleanup_complete=cleanup_complete,
        activation_readiness_deadline=_AT if state in _DEADLINE_STATES else None,
        materialization=materialization,
        recovery_point=(
            None
            if materialization is None
            else _recovery_point(activation_id, system_id, run_id, materialization)
        ),
        terminal_evidence=(
            None
            if outcome is None
            else ExternalBootTerminalEvidenceV1(
                activation_id=activation_id,
                system_id=system_id,
                outcome=outcome,
                composite_state=_DIGEST,
                objects=(),
                observed_at=_AT,
            )
        ),
        teardown_evidence=teardown,
        cleanup_evidence=cleanup,
        current_attempt_id=uuid4() if state in _ATTEMPT_STATES else None,
        created_at=_AT,
        updated_at=_AT,
    )


def _column_value(activation: ExternalBootActivation, column: str) -> Any:
    value = getattr(activation, column)
    if isinstance(value, BaseModel):
        return Jsonb(value.model_dump(mode="json", by_alias=True))
    return value


async def _insert_owners(conn: AsyncConnection, system_id: UUID, run_id: UUID) -> None:
    resource_id, allocation_id, investigation_id = uuid4(), uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'default', 'standard', 'available', 'qemu:///system')",
        (resource_id,),
    )
    await conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'granted', 'p', 'proj')",
        (allocation_id, resource_id),
    )
    await conn.execute(
        "INSERT INTO systems (id, allocation_id, state, provisioning_profile, principal, project) "
        "VALUES (%s, %s, 'ready', '{}'::jsonb, 'p', 'proj')",
        (system_id, allocation_id),
    )
    await conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'p', 'proj', 't', 'open')",
        (investigation_id,),
    )
    await conn.execute(
        "INSERT INTO runs (id, investigation_id, system_id, target_kind, state, build_profile, "
        "principal, project) VALUES "
        "(%s, %s, %s, 'local-libvirt', 'created', '{}'::jsonb, 'p', 'proj')",
        (run_id, investigation_id, system_id),
    )


async def _insert_attempt(conn: AsyncConnection, activation: ExternalBootActivation) -> None:
    attempt_state = _ATTEMPT_STATES[activation.state]
    outcome = _ATTEMPT_OUTCOMES.get(activation.state)
    conflict = (
        ExternalBootConflictEvidenceV1(
            activation_id=activation.id,
            system_id=activation.system_id,
            observation_id=uuid4(),
            composite_state=_DIGEST,
            objects=(),
            observed_at=_AT,
        )
        if attempt_state == "conflict"
        else None
    )
    terminal = (
        None
        if outcome is None
        else ExternalBootTerminalEvidenceV1(
            activation_id=activation.id,
            system_id=activation.system_id,
            outcome=outcome,
            composite_state=_DIGEST,
            objects=(),
            observed_at=_AT,
        )
    )
    await conn.execute(
        "INSERT INTO external_boot_recovery_attempts "
        "(activation_id, attempt_number, attempt_id, authority_generation, recovery_basis, "
        "recovery_readiness_deadline, state, conflict_evidence, terminal_evidence) "
        "VALUES (%s, 1, %s, %s, 'recovery_point', %s, %s, %s, %s)",
        (
            activation.id,
            activation.current_attempt_id,
            activation.authority_generation,
            _AT if attempt_state == "recovering" else None,
            attempt_state,
            None if conflict is None else Jsonb(conflict.model_dump(mode="json", by_alias=True)),
            None if terminal is None else Jsonb(terminal.model_dump(mode="json", by_alias=True)),
        ),
    )


async def seed_activation(
    conn: AsyncConnection,
    *,
    state: ExternalBootActivationState,
    cleanup_complete: bool = False,
    ready_reservation: bool = False,
    system_id: UUID | None = None,
    run_id: UUID | None = None,
) -> SeededActivation:
    """Seed one activation in a requested state, with the System and Run it is bound to.

    ``system_id`` / ``run_id`` restrict an **existing** System and Run instead — the call-site
    tests drive real handlers, which need a System seeded by the lifecycle helpers rather than
    the minimal owners this inserts on its own. Exposed as a plain function as well as the
    ``seeded_activation`` fixture, because tests outside this package cannot reach the fixture
    and must not grow a second insert path.
    """
    if (system_id is None) != (run_id is None):
        raise ValueError("pass both system_id and run_id, or neither")
    if system_id is None or run_id is None:
        system_id, run_id = uuid4(), uuid4()
        await _insert_owners(conn, system_id, run_id)
    activation = build_activation(
        activation_id=uuid4(),
        system_id=system_id,
        run_id=run_id,
        state=state,
        cleanup_complete=cleanup_complete,
    )
    placeholders = ", ".join(["%s"] * len(_ACTIVATION_COLUMNS))
    await conn.execute(
        f"INSERT INTO external_boot_activations ({', '.join(_ACTIVATION_COLUMNS)}) "  # noqa: S608
        f"VALUES ({placeholders})",
        tuple(_column_value(activation, column) for column in _ACTIVATION_COLUMNS),
    )
    if activation.current_attempt_id is not None:
        await _insert_attempt(conn, activation)
    if ready_reservation:
        await conn.execute(
            "INSERT INTO external_boot_reservations "
            "(activation_id, store_identity, owner_key, reserved_bytes, state, ready_at) "
            "VALUES (%s, 'stores/main', %s, 4096, 'ready', %s)",
            (activation.id, f"owners/{activation.id}", _AT),
        )
    return SeededActivation(activation=activation, system_id=system_id, run_id=run_id)


@pytest.fixture
def seeded_activation() -> SeedActivation:
    """The :func:`seed_activation` seeding path, as a fixture."""
    return seed_activation
