"""Provider target cleanup through a registered worker and its database role."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from functools import partial
from uuid import UUID, uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kdive.db.repositories import JOBS
from kdive.domain.capacity.state import JobState, SystemState
from kdive.domain.catalog.resources import ResourceKind
from kdive.domain.operations.jobs import JobKind
from kdive.jobs import queue
from kdive.jobs.handlers.systems import provision_handler
from kdive.jobs.models import HandlerRegistry
from kdive.jobs.payloads import Authorizing, SystemPayload
from kdive.jobs.worker import Worker
from kdive.profiles.provisioning import ProvisioningProfile
from kdive.providers.core.resolver import ProviderResolver
from kdive.providers.fault_inject.composition import build_runtime
from kdive.providers.fault_inject.inventory import FaultInjectInventory
from kdive.security.secrets.secret_registry import SecretRegistry
from tests.mcp.lifecycle.runs_support import seed_system
from tests.support.worker_fence import register_worker


@pytest.mark.parametrize("recorded_name", [None, "", "retained-provider-target"])
def test_stale_provision_reclaims_the_actual_provider_target(
    migrated_url: str, authority_role_dsns: Callable[[str], str], recorded_name: str | None
) -> None:
    async def run() -> None:
        profile = ProvisioningProfile.model_validate(
            {
                "schema_version": 1,
                "arch": "x86_64",
                "vcpu": 1,
                "memory_mb": 512,
                "disk_gb": 1,
                "boot_method": "direct-kernel",
                "kernel_source_ref": "git+https://git.kernel.org#v6.9",
                "provider": {"fault-inject": {}},
            }
        )
        inventory = FaultInjectInventory()
        runtime = build_runtime(inventory=inventory)
        resolver = ProviderResolver({ResourceKind.FAULT_INJECT: runtime})
        registry = HandlerRegistry()
        registry.register(JobKind.PROVISION, partial(provision_handler, resolver=resolver))
        worker_id = f"target-worker-{uuid4().hex}"
        async with AsyncConnectionPool(migrated_url, min_size=1, max_size=5, open=False) as admin:
            system_id = UUID(
                await seed_system(
                    admin,
                    system_state=SystemState.TORN_DOWN,
                    provisioning_profile=profile.model_dump(by_alias=True),
                )
            )
            domain = runtime.provisioner.provision(system_id, profile)
            assert inventory.system_id_of(domain) == system_id
            if recorded_name:
                inventory.forget(domain)
                domain = recorded_name
                inventory.record(system_id, domain)
            async with admin.connection() as conn:
                await conn.execute(
                    "UPDATE resources SET kind=%s WHERE id=(SELECT resource_id FROM allocations "
                    "WHERE id=(SELECT allocation_id FROM systems WHERE id=%s))",
                    (ResourceKind.FAULT_INJECT.value, system_id),
                )
                await conn.execute(
                    "UPDATE systems SET domain_name=%s WHERE id=%s", (recorded_name, system_id)
                )
                credential = await register_worker(conn, worker_id)
                job = await queue.enqueue(
                    conn,
                    JobKind.PROVISION,
                    SystemPayload(system_id=str(system_id)),
                    Authorizing(principal="user-1", project="proj"),
                    f"target-proof:{system_id}",
                )
            async with AsyncConnectionPool(
                authority_role_dsns("kdive_worker"),
                min_size=1,
                max_size=5,
                open=False,
            ) as worker_pool:
                worker = Worker(
                    worker_pool,
                    registry,
                    worker_id=worker_id,
                    incarnation_credential=credential,
                    secret_registry=SecretRegistry(),
                )
                claimed = await worker.run_once(job.dispatch_lane)
                assert claimed is not None and claimed.id == job.id
            async with admin.connection() as conn:
                stored = await JOBS.get(conn, job.id)
                assert stored is not None and stored.state is JobState.SUCCEEDED
            assert inventory.system_id_of(domain) is None, "stale provision leaked its target"

    asyncio.run(run())
