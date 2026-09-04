"""Charter criterion 3: identity comes from the activation row, provider_kind from the caller.

The criterion's two halves are asserted separately, because they fail differently: the marker's
``provider_kind`` and ``authority_instance`` must be caller-supplied (neither
``ExternalBootActivation`` nor ``ExternalBootReservation`` carries them), and a ``provider_kind``
disagreeing with the resolved runtime must be rejected **at validation** rather than at
``allocate_external_boot_authority``. The second is asserted by showing no authority row exists
afterwards; the exception alone would pass either way.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row

from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers.external_boot.admission import build_external_boot_payload
from kdive.jobs.payloads import BootPayload, TeardownPayload, dump_payload, load_payload
from kdive.providers.fault_inject.lifecycle.external_boot import FaultInjectExternalBoot
from tests.jobs.handlers.external_boot.conftest import resolver_for
from tests.jobs.handlers.external_boot.seeding import AUTHORITY_INSTANCE, seed_case
from tests.jobs.handlers.external_boot.support import build_job
from tests.jobs.handlers.external_boot.vehicle import Vehicle, build_vehicle
from tests.mcp.systems_support import provider_resolver


def _drive(migrated_url: str, body: Callable[[AsyncConnection, Vehicle], Awaitable[None]]) -> None:
    async def _main() -> None:
        vehicle = build_vehicle()
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as conn:
            await body(conn, vehicle)

    asyncio.run(_main())


async def _authority_count(conn: AsyncConnection) -> int:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT count(*) AS n FROM external_boot_authorities")
        row = await cur.fetchone()
    assert row is not None
    return int(row["n"])


def test_preparing_activation_is_prepared_through_production_admission_boundary(
    migrated_url: str,
) -> None:
    async def body(conn: AsyncConnection, vehicle: Vehicle) -> None:
        await seed_case(
            conn,
            vehicle,
            purpose="activate",
            activation_state="preparing",
            with_materialization=False,
            with_recovery_point=False,
            with_reservation=True,
        )
        provider = FaultInjectExternalBoot()
        resolver = provider_resolver(
            external_boot=provider,
            external_boot_preparation=provider,
        )

        kind, payload = await build_external_boot_payload(
            conn,
            activation_id=vehicle.activation_id,
            purpose="activate",
            operation="activate",
            provider_kind="local-libvirt",
            authority_instance=AUTHORITY_INSTANCE,
            operation_identity="activate-after-preparation",
            resolver=resolver,
            preparation_plan=vehicle.plan,
        )

        activation = await conn.execute(
            "SELECT state, materialization, recovery_point "
            "FROM external_boot_activations WHERE id = %s",
            (vehicle.activation_id,),
        )
        row = await activation.fetchone()
        assert row is not None
        assert row[0] == ExternalBootActivationState.PREPARED.value
        assert row[1] is not None and row[2] is not None
        assert kind is JobKind.BOOT
        assert payload.external_boot_authority_v1 is not None
        assert provider.preparation_mutations == {"materialize": 1, "prepare": 1}

        # Re-entry uses identities derived only from the durable activation ownership tuple.
        await conn.execute(
            "UPDATE external_boot_activations SET state = 'preparing', "
            "materialization = NULL, recovery_point = NULL WHERE id = %s",
            (vehicle.activation_id,),
        )
        await build_external_boot_payload(
            conn,
            activation_id=vehicle.activation_id,
            purpose="activate",
            operation="activate",
            provider_kind="local-libvirt",
            authority_instance=AUTHORITY_INSTANCE,
            operation_identity="activate-after-preparation",
            resolver=resolver,
            preparation_plan=vehicle.plan,
        )
        assert provider.preparation_mutations == {"materialize": 1, "prepare": 1}

    _drive(migrated_url, body)


def test_identity_is_sourced_from_the_activation_row(migrated_url: str) -> None:
    """The caller passes no run, system or plan identity, and the marker carries the row's."""

    async def body(conn: AsyncConnection, vehicle: Vehicle) -> None:
        await seed_case(conn, vehicle, purpose="activate")

        kind, payload = await build_external_boot_payload(
            conn,
            activation_id=vehicle.activation_id,
            purpose="activate",
            operation="activate",
            provider_kind="local-libvirt",
            authority_instance=AUTHORITY_INSTANCE,
            operation_identity="activate-1",
            resolver=resolver_for(vehicle),
        )

        assert kind is JobKind.BOOT
        marker = payload.external_boot_authority_v1
        assert marker is not None
        assert marker.activation_id == vehicle.activation_id
        assert marker.run_id == vehicle.run_id
        assert marker.system_id == vehicle.system_id
        assert marker.plan_identity == vehicle.plan_identity

    _drive(migrated_url, body)


def test_a_mismatched_provider_kind_is_refused_and_allocates_nothing(migrated_url: str) -> None:
    async def body(conn: AsyncConnection, vehicle: Vehicle) -> None:
        await seed_case(conn, vehicle, purpose="activate")

        with pytest.raises(CategorizedError, match="provider_kind") as excinfo:
            await build_external_boot_payload(
                conn,
                activation_id=vehicle.activation_id,
                purpose="activate",
                operation="activate",
                provider_kind="remote-libvirt",
                authority_instance=AUTHORITY_INSTANCE,
                operation_identity="activate-1",
                resolver=resolver_for(vehicle),
            )

        assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert await _authority_count(conn) == 0

    _drive(migrated_url, body)


def test_a_runtime_with_no_external_boot_port_is_refused(migrated_url: str) -> None:
    async def body(conn: AsyncConnection, vehicle: Vehicle) -> None:
        await seed_case(conn, vehicle, purpose="activate")

        with pytest.raises(CategorizedError, match="no external_boot port"):
            await build_external_boot_payload(
                conn,
                activation_id=vehicle.activation_id,
                purpose="activate",
                operation="activate",
                provider_kind="local-libvirt",
                authority_instance=AUTHORITY_INSTANCE,
                operation_identity="activate-1",
                resolver=provider_resolver(),
            )

        assert await _authority_count(conn) == 0

    _drive(migrated_url, body)


def test_an_absent_activation_is_refused(migrated_url: str) -> None:
    async def body(conn: AsyncConnection, vehicle: Vehicle) -> None:
        del vehicle

        with pytest.raises(CategorizedError, match="does not exist"):
            await build_external_boot_payload(
                conn,
                activation_id=uuid4(),
                purpose="activate",
                operation="activate",
                provider_kind="local-libvirt",
                authority_instance=AUTHORITY_INSTANCE,
                operation_identity="activate-1",
                resolver=provider_resolver(),
            )

    _drive(migrated_url, body)


@pytest.mark.parametrize("operation", ["deadline", "recovery-attempt", "fail"])
def test_a_non_enqueueable_operation_is_refused(migrated_url: str, operation: str) -> None:
    """Refused before the row is even read: these are commit points, never admissions."""

    async def body(conn: AsyncConnection, vehicle: Vehicle) -> None:
        await seed_case(conn, vehicle, purpose="activate")

        with pytest.raises(CategorizedError, match=operation):
            await build_external_boot_payload(
                conn,
                activation_id=vehicle.activation_id,
                purpose="activate",
                operation=operation,
                provider_kind="local-libvirt",
                authority_instance=AUTHORITY_INSTANCE,
                operation_identity=f"{operation}-1",
                resolver=resolver_for(vehicle),
            )

    _drive(migrated_url, body)


def test_the_teardown_purpose_is_the_only_one_that_yields_the_teardown_kind(
    migrated_url: str,
) -> None:
    """``0122…sql:465`` pins the pairing, so no caller picks the kind by hand."""

    async def body(conn: AsyncConnection, vehicle: Vehicle) -> None:
        await seed_case(
            conn,
            vehicle,
            purpose="teardown",
            activation_state="recovery_failed",
            attempt_state="failed",
        )

        kind, payload = await build_external_boot_payload(
            conn,
            activation_id=vehicle.activation_id,
            purpose="teardown",
            operation="teardown",
            provider_kind="local-libvirt",
            authority_instance=AUTHORITY_INSTANCE,
            operation_identity="teardown-1",
            resolver=resolver_for(vehicle),
        )

        assert kind is JobKind.TEARDOWN
        assert isinstance(payload, TeardownPayload)

    _drive(migrated_url, body)


def test_the_built_payload_survives_dump_and_load(migrated_url: str) -> None:
    """What the helper returns must be enqueueable, so it goes through the real chokepoint."""

    async def body(conn: AsyncConnection, vehicle: Vehicle) -> None:
        await seed_case(conn, vehicle, purpose="activate")

        kind, payload = await build_external_boot_payload(
            conn,
            activation_id=vehicle.activation_id,
            purpose="activate",
            operation="activate",
            provider_kind="local-libvirt",
            authority_instance=AUTHORITY_INSTANCE,
            operation_identity="activate-1",
            resolver=resolver_for(vehicle),
        )

        dumped = dump_payload(kind, payload)
        assert set(dumped) == {"run_id", "external_boot_authority_v1"}
        decoded = load_payload(build_job(kind, dumped), BootPayload)
        assert decoded == payload

    _drive(migrated_url, body)
