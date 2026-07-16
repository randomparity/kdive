"""The per-System bootstrap key is wired into provision/reprovision/teardown (ADR-0289, #963).

`provision_handler`/`reprovision_handler` must ensure the System's bootstrap key **before**
the provision transaction (so a rollback inside that transaction never orphans an
overlay-injected key the DB no longer records) and pass a customizer carrying that key into
the provisioner. `teardown_handler` must delete the key row so a torn-down System leaves no
stale credential behind.

Follows the PROVEN in-repo pattern: sync `def test_(migrated_url)` + inner `async def _run()`
driven by `asyncio.run`, real `AsyncConnectionPool`, and a fake provisioner recording its
kwargs (mirrors `tests/adversarial/test_provider_state_races.py`'s `_TrackingProvisioner` and
`tests/mcp/systems_support.py`'s `provider_resolver`).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEMS
from kdive.domain.capacity.state import AllocationState, ResourceStatus, SystemState
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.errors import CategorizedError, ErrorCategory
from kdive.domain.lifecycle.records import Allocation, System
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.handlers import systems as systems_handlers
from kdive.jobs.payloads import ReprovisionPayload, SystemPayload
from kdive.prereqs.system_bootstrap_key import ensure_system_bootstrap_key
from kdive.profiles.provisioning import ProvisioningProfile, profile_digest
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.systems_support import PROVISIONING_PROFILE, provider_resolver

_DT = datetime(2026, 1, 1, tzinfo=UTC)


class _RecordingProvisioner:
    """Records the ``overlay_customizers`` and ``bootstrap_pubkey`` kwargs passed to provision."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.recorded: dict[str, Any] = {}

    def provision(
        self,
        system_id: UUID,
        profile: Any,
        *,
        overlay_customizers: tuple[Any, ...] = (),
        bootstrap_pubkey: str | None = None,
    ) -> str:
        self.recorded["overlay_customizers"] = overlay_customizers
        self.recorded["bootstrap_pubkey"] = bootstrap_pubkey
        if self.fail:
            raise CategorizedError(
                "simulated provision failure",
                category=ErrorCategory.PROVISIONING_FAILURE,
            )
        return f"kdive-{system_id}"

    def read_resolved_cpu(self, system_id: object) -> None:
        del system_id
        return None

    def reprovision(
        self,
        system_id: UUID,
        profile: Any,
        *,
        overlay_customizers: tuple[Any, ...] = (),
        bootstrap_pubkey: str | None = None,
    ) -> str:
        self.recorded["overlay_customizers"] = overlay_customizers
        self.recorded["bootstrap_pubkey"] = bootstrap_pubkey
        if self.fail:
            raise CategorizedError(
                "simulated reprovision failure",
                category=ErrorCategory.PROVISIONING_FAILURE,
            )
        return f"kdive-{system_id}"

    def teardown(self, domain_name: str) -> None:
        pass


@asynccontextmanager
async def _pool(url: str) -> AsyncIterator[AsyncConnectionPool]:
    pool = AsyncConnectionPool(url, min_size=1, max_size=2, open=False)
    await pool.open()
    try:
        yield pool
    finally:
        await pool.close()


async def _seed_system(
    pool: AsyncConnectionPool, state: SystemState, *, provisioning_profile: dict[str, Any]
) -> UUID:
    async with pool.connection() as conn:
        resource = await RESOURCES.insert(
            conn,
            Resource(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                kind=ResourceKind.LOCAL_LIBVIRT,
                capabilities={"vcpus": 64, "memory_mb": 65536},
                pool="default",
                cost_class="local",
                status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            ),
        )
        allocation = await ALLOCATIONS.insert(
            conn,
            Allocation(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                project="proj",
                resource_id=resource.id,
                state=AllocationState.ACTIVE,
            ),
        )
        system = await SYSTEMS.insert(
            conn,
            System(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                agent_session="s",
                project="proj",
                allocation_id=allocation.id,
                state=state,
                provisioning_profile=provisioning_profile,
            ),
        )
    return system.id


async def _key_row_count(pool: AsyncConnectionPool, system_id: UUID) -> int:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM system_bootstrap_keys WHERE system_id = %s", (system_id,)
        )
        row = await cur.fetchone()
        assert row is not None
        return row[0]


def test_provision_handler_ensures_key_and_passes_one_customizer(migrated_url: str) -> None:
    async def _run() -> tuple[int, int, str, str | None]:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(
                pool, SystemState.PROVISIONING, provisioning_profile=PROVISIONING_PROFILE
            )
            prov = _RecordingProvisioner()
            resolver = provider_resolver(provisioner=prov)
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.PROVISION,
                    SystemPayload(system_id=str(system_id)),
                    {"principal": "alice", "agent_session": "s", "project": "proj"},
                    f"{system_id}:provision",
                )
            async with pool.connection() as conn:
                await systems_handlers.provision_handler(conn, job, resolver=resolver)
            count = await _key_row_count(pool, system_id)
            async with pool.connection() as conn:
                pubkey = await ensure_system_bootstrap_key(
                    conn, system_id, secret_registry=SecretRegistry()
                )
            return (
                len(prov.recorded["overlay_customizers"]),
                count,
                pubkey,
                prov.recorded["bootstrap_pubkey"],
            )

    n_customizers, count, pubkey, threaded_pubkey = asyncio.run(_run())
    assert n_customizers == 1
    assert count == 1
    assert pubkey.startswith("ssh-ed25519 ")
    # The ensured public key is threaded to the provider as bootstrap_pubkey (ADR-0291): remote
    # injects it over the guest agent; local ignores it (uses the overlay customizer).
    assert threaded_pubkey == pubkey


def test_key_row_survives_provision_transaction_rollback(migrated_url: str) -> None:
    """A provisioner failure after the key was ensured+committed leaves the key row present.

    This is the load-bearing invariant (ADR-0289): the ensure runs in its own committed
    transaction *before* the main provision transaction, so a later failure/rollback never
    un-records a key the overlay may already trust.
    """

    async def _run() -> int:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(
                pool, SystemState.PROVISIONING, provisioning_profile=PROVISIONING_PROFILE
            )
            prov = _RecordingProvisioner(fail=True)
            resolver = provider_resolver(provisioner=prov)
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.PROVISION,
                    SystemPayload(system_id=str(system_id)),
                    {"principal": "alice", "agent_session": "s", "project": "proj"},
                    f"{system_id}:provision",
                )
            async with pool.connection() as conn:
                with pytest.raises(CategorizedError):
                    await systems_handlers.provision_handler(conn, job, resolver=resolver)
            return await _key_row_count(pool, system_id)

    assert asyncio.run(_run()) == 1


def test_reprovision_handler_ensures_key_and_passes_one_customizer(migrated_url: str) -> None:
    async def _run() -> tuple[int, int]:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(
                pool, SystemState.REPROVISIONING, provisioning_profile=PROVISIONING_PROFILE
            )
            prov = _RecordingProvisioner()
            resolver = provider_resolver(provisioner=prov)
            fingerprint = profile_digest(ProvisioningProfile.parse(PROVISIONING_PROFILE))
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.REPROVISION,
                    ReprovisionPayload(system_id=str(system_id), profile_digest=fingerprint),
                    {"principal": "alice", "agent_session": "s", "project": "proj"},
                    f"{system_id}:reprovision",
                )
            async with pool.connection() as conn:
                await systems_handlers.reprovision_handler(conn, job, resolver=resolver)
            count = await _key_row_count(pool, system_id)
            return len(prov.recorded["overlay_customizers"]), count

    n_customizers, count = asyncio.run(_run())
    assert n_customizers == 1
    assert count == 1


def test_teardown_handler_deletes_key_row(migrated_url: str) -> None:
    async def _run() -> int:
        async with _pool(migrated_url) as pool:
            system_id = await _seed_system(
                pool, SystemState.READY, provisioning_profile=PROVISIONING_PROFILE
            )
            async with pool.connection() as conn:
                await ensure_system_bootstrap_key(conn, system_id, secret_registry=SecretRegistry())
            prov = _RecordingProvisioner()
            resolver = provider_resolver(provisioner=prov)
            async with pool.connection() as conn:
                job = await queue.enqueue(
                    conn,
                    JobKind.TEARDOWN,
                    SystemPayload(system_id=str(system_id)),
                    {"principal": "alice", "agent_session": "s", "project": "proj"},
                    f"{system_id}:teardown",
                )
            async with pool.connection() as conn:
                await systems_handlers.teardown_handler(conn, job, resolver=resolver)
            return await _key_row_count(pool, system_id)

    assert asyncio.run(_run()) == 0
