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

import kdive.config as config_registry
from kdive.db.remote_module_attempt_obligations import (
    ModuleAttempt,
    RemoteModuleAttemptObligationRepository,
)
from kdive.domain.capacity.state import ExternalBootActivationState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers.external_boot.admission import build_external_boot_payload
from kdive.jobs.payloads import BootPayload, TeardownPayload, dump_payload, load_payload
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.fault_inject.lifecycle.external_boot import FaultInjectExternalBoot
from tests.db.remote_module_attempt_obligations_support import _evidence
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


def test_preparing_activation_persists_plan_without_provider_mutation(
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
        assert row == (ExternalBootActivationState.PREPARING.value, None, None)
        assert kind is JobKind.BOOT
        assert isinstance(payload, BootPayload)
        assert payload.external_boot_authority_v1 is not None
        assert payload.external_boot_plan_v1 == vehicle.plan
        assert provider.preparation_mutations == {"materialize": 0, "prepare": 0}

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
        assert provider.preparation_mutations == {"materialize": 0, "prepare": 0}

    _drive(migrated_url, body)


@pytest.mark.parametrize(
    "configured",
    [
        {},
        {"KDIVE_WORKER_EXTERNAL_BOOT_AUTHORITY_INSTANCE": AUTHORITY_INSTANCE},
        {"KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE": "different-authority"},
    ],
    ids=["absent", "partial-worker-route", "mismatch"],
)
def test_preparing_without_direct_ports_requires_exact_fixed_server_route(
    migrated_url: str, configured: dict[str, str]
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
        config_registry.load(configured)
        with pytest.raises(CategorizedError, match="fixed server route"):
            await build_external_boot_payload(
                conn,
                activation_id=vehicle.activation_id,
                purpose="activate",
                operation="activate",
                provider_kind="local-libvirt",
                authority_instance=AUTHORITY_INSTANCE,
                operation_identity="activate-with-fixed-route",
                resolver=provider_resolver(
                    external_boot=None,
                    external_boot_preparation=None,
                ),
                preparation_plan=vehicle.plan,
            )
        assert await _authority_count(conn) == 0

    _drive(migrated_url, body)


def test_preparing_with_fixed_server_route_needs_no_direct_provider_ports(
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
        config_registry.load({"KDIVE_EXTERNAL_BOOT_AUTHORITY_INSTANCE": AUTHORITY_INSTANCE})
        kind, payload = await build_external_boot_payload(
            conn,
            activation_id=vehicle.activation_id,
            purpose="activate",
            operation="activate",
            provider_kind="local-libvirt",
            authority_instance=AUTHORITY_INSTANCE,
            operation_identity="activate-with-fixed-route",
            resolver=provider_resolver(external_boot=None, external_boot_preparation=None),
            preparation_plan=vehicle.plan,
        )
        assert kind is JobKind.BOOT
        assert payload.external_boot_authority_v1 is not None
        assert payload.external_boot_authority_v1.authority_instance == AUTHORITY_INSTANCE

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


@pytest.mark.parametrize("retained", [False, True], ids=["missing", "retained"])
def test_remote_lifecycle_payload_carries_exact_retained_prep_receipt(
    migrated_url: str, retained: bool
) -> None:
    async def body(conn: AsyncConnection, vehicle: Vehicle) -> None:
        await seed_case(
            conn,
            vehicle,
            purpose="release",
            activation_state="recovered",
            with_reservation=True,
        )
        await conn.execute(
            "UPDATE resources SET kind='remote-libvirt' WHERE id=("
            "SELECT a.resource_id FROM systems s JOIN allocations a ON a.id=s.allocation_id "
            "WHERE s.id=%s)",
            (vehicle.system_id,),
        )
        await conn.execute(
            "UPDATE runs SET target_kind='remote-libvirt' WHERE id=%s", (vehicle.run_id,)
        )
        attempt = ModuleAttempt(vehicle.system_id, vehicle.run_id, "1" * 32)
        if retained:
            repository = RemoteModuleAttemptObligationRepository()
            await repository.open_mutation_obligation(conn, attempt)
            await repository.record_terminal_evidence(conn, attempt, _evidence(attempt))
            await repository.open_reap_obligation(conn, attempt)
        local = resolver_for(vehicle).resolve(ResourceKind.LOCAL_LIBVIRT)
        resolver = ProviderResolver({ResourceKind.REMOTE_LIBVIRT: local})

        if not retained:
            with pytest.raises(CategorizedError, match="no retained PREP evidence"):
                await build_external_boot_payload(
                    conn,
                    activation_id=vehicle.activation_id,
                    purpose="release",
                    operation="release",
                    provider_kind="remote-libvirt",
                    authority_instance=AUTHORITY_INSTANCE,
                    operation_identity="release-remote",
                    resolver=resolver,
                )
            return

        kind, payload = await build_external_boot_payload(
            conn,
            activation_id=vehicle.activation_id,
            purpose="release",
            operation="release",
            provider_kind="remote-libvirt",
            authority_instance=AUTHORITY_INSTANCE,
            operation_identity="release-remote",
            resolver=resolver,
        )
        assert kind is JobKind.BOOT
        assert isinstance(payload, BootPayload)
        assert payload.remote_module_attempt_v1 is not None
        assert payload.remote_module_attempt_v1.module_attempt_obligation.model_dump() == {
            "schema_": "module-attempt-obligation-receipt-v1",
            "system_id": vehicle.system_id,
            "run_id": vehicle.run_id,
            "operation_nonce": "1" * 32,
        }

    _drive(migrated_url, body)


def test_conflict_payload_binds_the_exact_observed_composite(migrated_url: str) -> None:
    async def body(conn: AsyncConnection, vehicle: Vehicle) -> None:
        await seed_case(
            conn,
            vehicle,
            purpose="resolve-conflict",
            activation_state="recovery_conflict",
            attempt_state="conflict",
        )
        expected = "sha256:" + "d" * 64

        kind, payload = await build_external_boot_payload(
            conn,
            activation_id=vehicle.activation_id,
            purpose="resolve-conflict",
            operation="resolve-conflict",
            provider_kind="local-libvirt",
            authority_instance=AUTHORITY_INSTANCE,
            operation_identity="resolve-conflict-1",
            resolver=resolver_for(vehicle),
            expected_observed_composite=expected,
        )

        assert kind is JobKind.BOOT
        assert isinstance(payload, BootPayload)
        assert payload.external_boot_authority_v1 is not None
        assert payload.external_boot_authority_v1.expected_observed_composite == expected

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
