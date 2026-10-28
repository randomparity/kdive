"""Allocation release after the teardown of an activated authority System (#3382).

The ownership row of an activated System ends ``torn-down`` with its first activation recorded.
Only allocation release is admitted for it; every other mutation keeps the pre-activation fence,
and a never-activated owner still needs the consumed absence receipt.
"""

from __future__ import annotations

import asyncio
from typing import LiteralString
from uuid import UUID

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.mcp.tools.lifecycle.allocations.lifecycle import release_allocation
from kdive.security.authz.context import RequestContext
from kdive.security.authz.rbac import Role
from kdive.services.external_boot.admission import (
    AUTHORITY_PREACTIVATION_DENIAL_REASON,
    ExternalBootDenied,
    ExternalBootOperation,
    check_external_boot_admission,
)
from tests.db.external_boot_authority_support import _AuthorityCase, _RoleDsns, _seed_case

_PROFILE = "sha256:" + "d" * 64
_ROOT = "sha256:" + "e" * 64
_CONTEXT = RequestContext("q", "release", ("proj",), {"proj": Role.CONTRIBUTOR})


def _scalar(dsn: str, query: LiteralString, params: tuple[object, ...]) -> object:
    with psycopg.connect(dsn) as conn:
        row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


def _torn_down_owner(
    dsn: str, *, activated: bool, state: str = "torn-down", orphan_row: bool = False
) -> _AuthorityCase:
    """An authority owner whose System was torn down, with or without a first activation."""
    with psycopg.connect(dsn) as seed:
        case = _seed_case(seed, purpose="teardown")
        row = seed.execute(
            "SELECT resource_id FROM allocations WHERE id=%s", (case.allocation_id,)
        ).fetchone()
        assert row is not None
        seed.execute("UPDATE resources SET name='host-owned' WHERE id=%s", (row[0],))
        seed.execute(
            "INSERT INTO authority_system_ownership "
            "(system_id,allocation_id,resource_id,provider_kind,resource_name,authority_instance,"
            "profile_identity,root_identity,state,first_activation_id) VALUES "
            "(%s,%s,%s,%s,'host-owned',%s,%s,%s,%s,%s)",
            (
                case.system_id,
                case.allocation_id,
                row[0],
                case.provider_kind,
                case.authority_instance,
                _PROFILE,
                _ROOT,
                state,
                case.activation_id if activated and state == "torn-down" else None,
            ),
        )
        seed.execute("UPDATE systems SET state='torn_down' WHERE id=%s", (case.system_id,))
        if orphan_row:
            seed.execute(
                "UPDATE external_boot_activations SET state='preparing', materialization=NULL, "
                "pre_recovery_evidence=NULL, recovery_point=NULL WHERE id=%s",
                (case.activation_id,),
            )
        else:
            seed.execute(
                "UPDATE external_boot_activations SET state='abandoned', cleanup_complete=true, "
                "materialization=NULL, recovery_point=NULL, pre_recovery_evidence=NULL, "
                "terminal_evidence=jsonb_build_object('schema','external-boot-terminal-evidence-v1',"
                "'outcome','abandoned','activation_id',id::text,'system_id',system_id::text), "
                "cleanup_evidence=jsonb_build_object('schema','external-boot-cleanup-evidence-v1',"
                "'mode','ordinary','activation_id',id::text,'system_id',system_id::text) "
                "WHERE id=%s",
                (case.activation_id,),
            )
        if not activated and not orphan_row:
            seed.execute("DELETE FROM external_boot_activations WHERE id=%s", (case.activation_id,))
    return case


def _admit(dsn: str, system_id: UUID, operation: ExternalBootOperation) -> None:
    async def run() -> None:
        async with await psycopg.AsyncConnection.connect(dsn) as conn:
            await check_external_boot_admission(conn, system_id, operation, project="proj")

    asyncio.run(run())


def _release(dsn: str, allocation_id: UUID) -> object:
    async def run() -> object:
        async with AsyncConnectionPool(dsn, min_size=1, max_size=1, open=False) as pool:
            return await release_allocation(pool, _CONTEXT, str(allocation_id))

    return asyncio.run(run())


def test_release_is_admitted_for_a_torn_down_activated_system(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _torn_down_owner(migrated_url, activated=True)

    _admit(
        authority_role_dsns("kdive_server"),
        case.system_id,
        ExternalBootOperation.ALLOCATION_RELEASE,
    )


def test_release_releases_the_allocation_of_a_torn_down_activated_system(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _torn_down_owner(migrated_url, activated=True)

    result = _release(authority_role_dsns("kdive_server"), case.allocation_id)

    assert getattr(result, "status", None) != "error", result
    assert (
        _scalar(migrated_url, "SELECT state FROM allocations WHERE id=%s", (case.allocation_id,))
        == "released"
    )


@pytest.mark.parametrize(
    "operation",
    [
        ExternalBootOperation.SYSTEM_POWER,
        ExternalBootOperation.SYSTEM_REPROVISION,
        ExternalBootOperation.SYSTEM_AUTHORIZE_SSH_KEY,
    ],
)
def test_every_other_operation_stays_fenced_for_the_torn_down_owner(
    migrated_url: str, authority_role_dsns: _RoleDsns, operation: ExternalBootOperation
) -> None:
    case = _torn_down_owner(migrated_url, activated=True)

    with pytest.raises(ExternalBootDenied) as denied:
        _admit(authority_role_dsns("kdive_server"), case.system_id, operation)

    assert denied.value.details["reason"] == AUTHORITY_PREACTIVATION_DENIAL_REASON


def test_a_never_activated_torn_down_owner_without_a_receipt_stays_fenced(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    case = _torn_down_owner(migrated_url, activated=False)

    with pytest.raises(ExternalBootDenied) as denied:
        _admit(
            authority_role_dsns("kdive_server"),
            case.system_id,
            ExternalBootOperation.ALLOCATION_RELEASE,
        )

    assert denied.value.details["reason"] == AUTHORITY_PREACTIVATION_DENIAL_REASON


@pytest.mark.parametrize("state", ["ready", "teardown-requested", "repair-required"])
def test_an_owner_that_is_not_torn_down_stays_fenced_even_with_an_activation_row(
    migrated_url: str, authority_role_dsns: _RoleDsns, state: str
) -> None:
    case = _torn_down_owner(migrated_url, activated=True, state=state)

    with pytest.raises(ExternalBootDenied) as denied:
        _admit(
            authority_role_dsns("kdive_server"),
            case.system_id,
            ExternalBootOperation.ALLOCATION_RELEASE,
        )

    assert denied.value.details["reason"] == AUTHORITY_PREACTIVATION_DENIAL_REASON


def test_an_unclaimed_activation_row_left_by_a_blocked_claim_still_denies_release(
    migrated_url: str, authority_role_dsns: _RoleDsns
) -> None:
    """A claim blocked by a requested teardown can leave a live activation row behind.

    The row is not a first activation, but while it is not cleaned the activation policy denies
    release, so the activation-row proxy for activation history never admits it.
    """
    case = _torn_down_owner(migrated_url, activated=False, orphan_row=True)

    with pytest.raises(ExternalBootDenied):
        _admit(
            authority_role_dsns("kdive_server"),
            case.system_id,
            ExternalBootOperation.ALLOCATION_RELEASE,
        )
