"""The shared destructive-operation authorize helper, called directly (ADR-0028, ADR-0828)."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import UUID

import pytest

from kdive.db.repositories import SYSTEMS
from kdive.domain.operations.jobs import JobKind
from kdive.mcp.tools.lifecycle.support._destructive import authorize_destructive
from kdive.security.authz.rbac import Role
from tests.mcp.lifecycle import runs_support
from tests.mcp.systems_support import ctx as _ctx
from tests.mcp.systems_support import provider_resolver, provisioning_profile

_KINDS = [JobKind.FORCE_CRASH, JobKind.FORCE_UNPLUG_DEVICE]


def _profile(ops: list[str]) -> dict[str, Any]:
    profile = provisioning_profile()
    profile["provider"]["local-libvirt"]["destructive_ops"] = ops
    return profile


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize(
    ("role", "opted_in", "missing"),
    [
        (Role.ADMIN, True, None),
        (Role.ADMIN, False, ["profile_opt_in"]),
        (Role.CONTRIBUTOR, True, ["admin_role"]),
        (Role.CONTRIBUTOR, False, ["admin_role", "profile_opt_in"]),
    ],
)
def test_the_gate_allows_only_an_admin_on_an_opted_in_profile(
    migrated_url: str, kind: JobKind, role: Role, opted_in: bool, missing: list[str] | None
) -> None:
    async def run() -> None:
        async with runs_support.pool(migrated_url) as pool:
            ops = [kind.value] if opted_in else []
            system_id = await runs_support.seed_system(pool, provisioning_profile=_profile(ops))
            async with pool.connection() as conn:
                system = await SYSTEMS.get(conn, UUID(system_id))
                assert system is not None
                denial = await authorize_destructive(
                    conn,
                    _ctx(role),
                    system,
                    UUID(system_id),
                    kind,
                    resolver=provider_resolver(),
                    tool="test.tool",
                )
                cur = await conn.execute(
                    "SELECT tool, transition FROM audit_log WHERE object_id = %s",
                    (UUID(system_id),),
                )
                audited = await cur.fetchall()
        if missing is None:
            assert denial is None and audited == []
            return
        assert denial is not None and denial.error_category == "authorization_denied"
        checks = cast(list[Any], denial.data["missing_checks"])
        assert sorted(str(check) for check in checks) == sorted(missing)
        assert audited == [("test.tool", f"{kind.value}:denied")]

    asyncio.run(run())
