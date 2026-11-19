"""Shared seeding for child-ledger repository tests (System with its Resource and Allocation)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import psycopg

from kdive.db.repositories import ALLOCATIONS, RESOURCES, SYSTEMS
from kdive.domain.capacity.state import AllocationState, ResourceStatus, SystemState
from kdive.domain.catalog.resources import Resource, ResourceKind
from kdive.domain.lifecycle.records import Allocation, System

_DT = datetime(2026, 7, 17, tzinfo=UTC)


async def connect(url: str) -> psycopg.AsyncConnection:
    return await psycopg.AsyncConnection.connect(url, autocommit=True)


async def seed_system(conn: psycopg.AsyncConnection) -> UUID:
    res = await RESOURCES.insert(
        conn,
        Resource.model_validate(
            dict(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                kind=ResourceKind.LOCAL_LIBVIRT,
                pool="p",
                cost_class="c",
                status=ResourceStatus.AVAILABLE,
                host_uri="qemu:///system",
            )
        ),
    )
    alloc = await ALLOCATIONS.insert(
        conn,
        Allocation.model_validate(
            dict(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                project="proj",
                resource_id=res.id,
                state=AllocationState.REQUESTED,
            )
        ),
    )
    sysm = await SYSTEMS.insert(
        conn,
        System.model_validate(
            dict(
                id=uuid4(),
                created_at=_DT,
                updated_at=_DT,
                principal="alice",
                project="proj",
                allocation_id=alloc.id,
                state=SystemState.READY,
                provisioning_profile={"k": "v"},
            )
        ),
    )
    return sysm.id
