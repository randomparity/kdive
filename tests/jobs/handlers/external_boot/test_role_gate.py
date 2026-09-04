"""Charter criterion 8: allocation is gated on the worker role, in two layers, proven both ways.

``allocate_external_boot_authority`` is defended twice, and the two are worth separating because a
test that conflates them reports the wrong one:

- **The EXECUTE grant.** ``0122_external_boot_authority.sql:1738-1740`` grants the function to
  ``kdive_worker`` alone. A ``kdive_server`` session is refused here, before the body runs, with
  SQLSTATE ``42501`` and the message ``permission denied for function
  allocate_external_boot_authority``.
- **The in-body gate.** ``:359-361`` raises ``worker authority is required`` with the same SQLSTATE
  when ``pg_has_role(session_user, 'kdive_worker', 'member')`` is false.

The criterion names the second message, and it is asserted below — but reaching it takes a role
that *can* execute the function and is *not* a ``kdive_worker`` member, which the grant means no
production role is. The test constructs exactly that principal rather than claiming the
``kdive_server`` arm produces that message, which it does not.

Two further things make a test of this easy to get wrong, and both are handled here:

- **Never run any arm as the superuser.** Everything in the suite connects as the backend superuser
  by default, and ``pg_has_role`` is true for a superuser against every role — so a superuser arm
  asserts nothing. Every arm goes through a real LOGIN principal.
- **A denied arm alone proves nothing.** A call broken for *everyone* denies the server too, so the
  identical call is also run as ``kdive_worker`` and asserted to succeed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4

import psycopg
import pytest
from psycopg import AsyncConnection
from psycopg.conninfo import make_conninfo
from psycopg.sql import SQL, Identifier, Literal
from pydantic import SecretStr

from kdive.domain.operations.jobs import JobKind
from kdive.jobs.handlers.external_boot.authority import allocate_authority
from kdive.jobs.models import ExternalBootAuthorityMarkerV1
from tests.jobs.handlers.external_boot.conftest import role_connection
from tests.jobs.handlers.external_boot.seeding import SeededCase, seed_case
from tests.jobs.handlers.external_boot.support import build_job
from tests.jobs.handlers.external_boot.vehicle import Vehicle

_ALLOCATE = (
    "public.allocate_external_boot_authority"
    "(bytea,uuid,integer,uuid,uuid,uuid,text,text,text,text,text)"
)
_PASSWORD = "external-boot-role-gate-test"  # pragma: allowlist secret


async def _allocate(conn: AsyncConnection, case: SeededCase) -> object:
    job = build_job(
        JobKind.BOOT,
        {"run_id": str(case.vehicle.run_id), "external_boot_authority_v1": case.marker},
    ).model_copy(update={"id": case.job_id, "attempt": case.attempt})
    return await allocate_authority(
        conn,
        job,
        ExternalBootAuthorityMarkerV1.model_validate(case.marker),
        incarnation_credential=SecretStr(case.credential),
    )


def _drive(
    migrated_url: str,
    dsn: str,
    body: Callable[[AsyncConnection, SeededCase], Awaitable[None]],
    vehicle: Vehicle,
) -> None:
    async def _main() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(seed, vehicle, purpose="activate")
            async with await role_connection(dsn) as conn:
                await body(conn, case)

    asyncio.run(_main())


def test_allocation_under_a_server_session_is_denied_by_the_grant(
    migrated_url: str, authority_role_dsns: Callable[[str], str], vehicle: Vehicle
) -> None:
    """The production-realistic arm: ``kdive_server`` never reaches the function body."""

    async def body(conn: AsyncConnection, case: SeededCase) -> None:
        with pytest.raises(psycopg.errors.InsufficientPrivilege) as excinfo:
            await _allocate(conn, case)

        assert excinfo.value.sqlstate == "42501"
        assert "permission denied for function allocate_external_boot_authority" in str(
            excinfo.value
        )

    _drive(migrated_url, authority_role_dsns("kdive_server"), body, vehicle)


def test_the_in_body_worker_gate_raises_its_own_message(
    migrated_url: str, vehicle: Vehicle
) -> None:
    """Criterion 8's named message, from the gate the criterion cites.

    Reaching it needs a principal that holds EXECUTE and is not a ``kdive_worker`` member — which
    the grant means no production role is, so the test creates one. That is the only way to show
    the in-body check is live rather than dead code shadowed by the grant.
    """
    login = f"kdive_ebrg_{uuid4().hex[:16]}"

    async def _main() -> None:
        async with await psycopg.AsyncConnection.connect(migrated_url, autocommit=True) as seed:
            case = await seed_case(seed, vehicle, purpose="activate")
            await seed.execute(
                SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    Identifier(login), Literal(_PASSWORD)
                )
            )
            try:
                await seed.execute(
                    SQL("GRANT EXECUTE ON FUNCTION " + _ALLOCATE + " TO {}").format(
                        Identifier(login)
                    )
                )
                parameters = {
                    **seed.info.get_parameters(),
                    "user": login,
                    "password": _PASSWORD,
                }
                dsn = make_conninfo(**parameters)
                async with await role_connection(dsn) as conn:
                    with pytest.raises(psycopg.errors.InsufficientPrivilege) as excinfo:
                        await _allocate(conn, case)
                assert excinfo.value.sqlstate == "42501"
                assert "worker authority is required" in str(excinfo.value)
            finally:
                # The grant is a dependent object; DROP ROLE fails with DependentObjectsStillExist
                # until it is revoked, which would leave the role behind for every later test.
                await seed.execute(
                    SQL("REVOKE ALL ON FUNCTION " + _ALLOCATE + " FROM {}").format(
                        Identifier(login)
                    )
                )
                await seed.execute(SQL("DROP ROLE IF EXISTS {}").format(Identifier(login)))

    asyncio.run(_main())


def test_the_identical_allocation_under_a_worker_session_succeeds(
    migrated_url: str, authority_role_dsns: Callable[[str], str], vehicle: Vehicle
) -> None:
    """Without this arm the denials above would also pass for a call broken for every role."""

    async def body(conn: AsyncConnection, case: SeededCase) -> None:
        allocated = await _allocate(conn, case)

        assert allocated is not None

    _drive(migrated_url, authority_role_dsns("kdive_worker"), body, vehicle)
