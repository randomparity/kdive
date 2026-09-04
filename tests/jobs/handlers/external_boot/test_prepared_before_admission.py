"""ADR-0593 decision 4's pin, plus the NULL-evidence refusals it depends on.

``materialize`` and ``prepare`` are dispositioned **prepared-before-admission**: preconditions the
handlers verify and consume, never operations they perform. The schema forecloses every other
reading — ``allocate_external_boot_authority`` admits ``purpose = 'activate'`` only from
``prepared`` or ``activating``, and ``external_boot_activation_state_evidence`` admits either state
only once ``materialization`` **and** ``recovery_point`` are already recorded — so an activate job
cannot allocate authority until both exist, and therefore cannot be the thing that records them.

The pin is enforced by the port itself: ``GuardedExternalBoot`` raises ``AssertionError`` from both
methods, so a handler that called one fails whether or not a test remembered to check.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from pydantic import SecretStr, ValidationError

from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.operations.jobs import Job, JobKind
from kdive.jobs.handlers.external_boot.ports import ExternalBootHandlerPorts
from kdive.jobs.handlers.external_boot.registrar import build_operations
from kdive.jobs.models import ExternalBootAuthorityMarkerV1
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.jobs.handlers.external_boot.conftest import resolver_for, role_connection
from tests.jobs.handlers.external_boot.seeding import RecordingAcknowledger, SeededCase, seed_case
from tests.jobs.handlers.external_boot.support import CASES, build_job
from tests.jobs.handlers.external_boot.vehicle import Vehicle, build_vehicle

# operation -> the seeding that makes a NULL recovery_point a *legal* row for that operation.
# Each of these states is one external_boot_activation_state_evidence admits without one: it admits
# `abandoned` on terminal_evidence alone, and the recovery states whenever pre_recovery_evidence is
# present. So the refusal under test is the handler's, not the database's.
NULL_RECOVERY_POINT_CASES: dict[str, dict[str, Any]] = {
    "release": {"purpose": "release", "activation_state": "abandoned", "seed": {}},
    "cleanup": {"purpose": "release", "activation_state": "abandoned", "seed": {}},
    "teardown": {
        "purpose": "teardown",
        "activation_state": "recovery_failed",
        "seed": {"attempt_state": "failed", "with_pre_recovery": True},
    },
}


def _job(case: SeededCase) -> Job:
    kind = JobKind.TEARDOWN if case.purpose == "teardown" else JobKind.BOOT
    key = "system_id" if kind is JobKind.TEARDOWN else "run_id"
    value = case.vehicle.system_id if kind is JobKind.TEARDOWN else case.vehicle.run_id
    return build_job(kind, {key: str(value), "external_boot_authority_v1": case.marker}).model_copy(
        update={"id": case.job_id, "attempt": case.attempt}
    )


async def _authority_count(conn: AsyncConnection) -> int:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute("SELECT count(*) AS n FROM external_boot_authorities")
        row = await cur.fetchone()
    assert row is not None
    return int(row["n"])


async def _dispatch(
    dsns: Callable[[str], str], case: SeededCase, operation: str, vehicle: Vehicle
) -> None:
    ports = ExternalBootHandlerPorts(
        resolver=resolver_for(vehicle),
        incarnation_credential=SecretStr(case.credential),
        secret_registry=SecretRegistry(),
        acknowledger=RecordingAcknowledger(dsns("kdive_provider_authority")),
    )
    handler = build_operations(ports).get(operation)
    assert handler is not None
    async with await role_connection(dsns("kdive_worker")) as worker:
        await handler(worker, _job(case), ExternalBootAuthorityMarkerV1.model_validate(case.marker))


def _drive(migrated_url: str, body: Callable[[AsyncConnection], Awaitable[None]]) -> None:
    async def _main() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            await body(seed)

    asyncio.run(_main())


@pytest.mark.parametrize("operation", list(NULL_RECOVERY_POINT_CASES))
def test_a_null_recovery_point_is_refused_before_allocation(
    migrated_url: str, authority_role_dsns: Callable[[str], str], operation: str
) -> None:
    """A NULL evidence column is a categorized refusal, never read as a finished operation.

    The consequence is stated rather than left to be discovered: an activation whose required
    evidence column is NULL cannot be released, cleaned up, or torn down by this change at all —
    each refuses here, each refusal wedges its job, the reservation stays charged, and
    ``external_boot_activations_one_live_per_system`` keeps matching so the System can take no new
    activation. The alternative is worse: reading a NULL as a finished operation would commit
    evidence for work nothing performed. No writer produces that state today, and guaranteeing it
    stays unproducible is the preparation path's job (#2204).
    """
    spec = NULL_RECOVERY_POINT_CASES[operation]

    async def body(seed: AsyncConnection) -> None:
        vehicle = build_vehicle()
        case = await seed_case(
            seed,
            vehicle,
            purpose=spec["purpose"],
            operation=operation,
            activation_state=spec["activation_state"],
            with_recovery_point=False,
            **spec["seed"],
        )

        with pytest.raises(CategorizedError, match="has no recovery_point") as excinfo:
            await _dispatch(authority_role_dsns, case, operation, vehicle)

        assert excinfo.value.category is ErrorCategory.CONFIGURATION_ERROR
        assert excinfo.value.terminal is True
        assert vehicle.port.calls == []
        assert await _authority_count(seed) == 0

    _drive(migrated_url, body)


def test_an_activating_activation_cannot_hold_null_evidence_at_all(migrated_url: str) -> None:
    """Why there is no ``activate``-with-NULL-evidence handler case: the row is unconstructible.

    ``external_boot_activation_state_evidence`` admits ``activating`` only with **both**
    ``materialization`` and ``recovery_point`` non-null, so the database refuses the row before any
    handler could refuse the job. That is the schema half of ADR-0593 decision 4's argument, and it
    is asserted rather than assumed — if the constraint were relaxed, the activate handler would
    need the refusal case the other three operations have, and this test is what would say so.
    """

    async def body(seed: AsyncConnection) -> None:
        vehicle = build_vehicle()

        with pytest.raises(psycopg.errors.CheckViolation, match="state_evidence"):
            await seed_case(
                seed,
                vehicle,
                purpose="activate",
                activation_state="activating",
                with_recovery_point=False,
            )

    _drive(migrated_url, body)


def test_no_handler_calls_materialize_or_prepare_on_any_path(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    """ADR-0593 decision 4, across all six handlers and both their success and refusal paths.

    The fixture drives ``materialize`` and ``prepare`` itself **before** installing the wrapper,
    which is the disposition rather than a hole in it: the pin is that the *handler* never performs
    them, and the activation row the handler reads is prepared precisely because something else
    already did.
    """

    async def body(seed: AsyncConnection) -> None:
        observed: list[str] = []
        for operation, spec in CASES.items():
            vehicle = build_vehicle()
            case = await seed_case(
                seed,
                vehicle,
                purpose=spec["purpose"],
                operation=operation,
                activation_state=spec["activation_state"],
                **spec["seed"],
            )
            await _dispatch(authority_role_dsns, case, operation, vehicle)
            observed.extend(vehicle.port.calls)

        assert observed, "no operation ran, so this asserts nothing"
        assert "materialize" not in observed
        assert "prepare" not in observed

    _drive(migrated_url, body)


def test_a_recovery_point_without_a_materialization_cannot_decode_at_all(
    migrated_url: str, authority_role_dsns: Callable[[str], str]
) -> None:
    """Why ``release`` can never reach its observation check with a NULL ``materialization``.

    The branch review expected this shape: ``release`` admits ``abandoned``, whose row the table
    CHECK permits with ``materialization`` NULL, so a row carrying ``recovery_point`` without
    ``materialization`` would pass step 2b and then be refused inside ``build_result`` —
    post-allocation, post-port-call, reporting "produced no kernel observation to verify" and
    blaming the provider for a missing column.

    **That row is not constructible, one layer above the CHECK.**
    ``ExternalBootActivation``'s own validator (`domain/external_boot_activation.py:303-311`) lists
    ``self.materialization is None`` among the disjuncts that raise
    ``recovery point ownership does not match activation``, so the repository cannot decode it and
    no handler ever sees it. The misleading refusal is therefore unreachable rather than merely
    unproduced.

    ``release`` still names ``materialization`` in its required evidence, which is accurate — it
    does read ``materialization.kernel_observation`` — and costs one word. This test is what says
    the requirement is defence in depth rather than a live bug fix, and what would turn red if the
    model rule were ever relaxed and the reviewer's path became real.
    """

    async def body(seed: AsyncConnection) -> None:
        vehicle = build_vehicle()
        case = await seed_case(
            seed,
            vehicle,
            purpose="release",
            operation="release",
            activation_state="abandoned",
            with_materialization=False,
            with_reservation=True,
        )

        with pytest.raises(ValidationError, match="recovery point ownership"):
            await _dispatch(authority_role_dsns, case, "release", vehicle)

        assert vehicle.port.calls == []
        assert await _authority_count(seed) == 0

    _drive(migrated_url, body)
