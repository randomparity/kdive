"""The investigation-rootfs reclaim liveness gate (ADR-0441 §6, now worker-side per ADR-0442).

``rootfs_base_reclaimable`` is reclaimable only when **no** referencing System pins the base:
condition (a) a referencer's overlay file is present, or (b) a referencer is in a pre-overlay/
re-materialize state. The gate moved to the worker with the rest of the reclaim (#1522); its
behavior is unchanged, and the ``rootfs_dir_accessible`` co-location probe it used to sit behind is
gone — the worker that claims a local-libvirt job is the guest host by construction.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
from psycopg.types.json import Jsonb

from kdive.artifacts.content_address import rootfs_object_token
from kdive.jobs.handlers.artifacts.rootfs_reclaim import rootfs_base_reclaimable
from kdive.providers.local_libvirt.lifecycle.storage import overlay_name
from tests.reconciler.conftest import connect


def _checksum(seed: bytes) -> str:
    return base64.b64encode(hashlib.sha256(seed).digest()).decode("ascii")


_CHECKSUM_X = _checksum(b"rootfs-x")
_TOKEN_X = rootfs_object_token(_CHECKSUM_X)
_CHECKSUM_Y = _checksum(b"rootfs-y")


def _upload_profile(checksum: str) -> str:
    return json.dumps(
        {"provider": {"local-libvirt": {"rootfs": {"kind": "upload", "checksum_sha256": checksum}}}}
    )


def _catalog_profile() -> str:
    return json.dumps(
        {"provider": {"local-libvirt": {"rootfs": {"kind": "catalog", "name": "rhel"}}}}
    )


async def _seed_investigation(conn: psycopg.AsyncConnection) -> UUID:
    inv_id = uuid4()
    await conn.execute(
        "INSERT INTO investigations (id, principal, project, title, state) "
        "VALUES (%s, 'p', 'proj', 't', 'open')",
        (inv_id,),
    )
    return inv_id


async def _seed_allocation(conn: psycopg.AsyncConnection) -> UUID:
    resource_id, alloc_id = uuid4(), uuid4()
    await conn.execute(
        "INSERT INTO resources (id, kind, pool, cost_class, status, host_uri) "
        "VALUES (%s, 'local-libvirt', 'p', 'c', 'available', 'qemu:///system')",
        (resource_id,),
    )
    await conn.execute(
        "INSERT INTO allocations (id, resource_id, state, principal, project) "
        "VALUES (%s, %s, 'active', 'p', 'proj')",
        (alloc_id, resource_id),
    )
    return alloc_id


async def _seed_system(
    conn: psycopg.AsyncConnection,
    *,
    allocation_id: UUID,
    investigation_id: UUID | None,
    state: str,
    profile: str,
) -> UUID:
    system_id = uuid4()
    await conn.execute(
        "INSERT INTO systems (id, allocation_id, investigation_id, state, provisioning_profile, "
        "principal, project) VALUES (%s, %s, %s, %s, %s::jsonb, 'p', 'proj')",
        (system_id, allocation_id, investigation_id, state, profile),
    )
    return system_id


def _write_overlay(rootfs_dir: Path, system_id: UUID) -> None:
    rootfs_dir.mkdir(parents=True, exist_ok=True)
    (rootfs_dir / overlay_name(str(system_id))).write_bytes(b"overlay")


def _reclaimable(migrated_url: str, inv: UUID, rootfs_dir: Path) -> bool:
    async def _run() -> bool:
        conn = await connect(migrated_url)
        try:
            return await rootfs_base_reclaimable(conn, inv, _TOKEN_X, rootfs_dir=str(rootfs_dir))
        finally:
            await conn.close()

    return asyncio.run(_run())


def test_overlay_present_pins_the_base(migrated_url: str, tmp_path: Path) -> None:
    async def _seed() -> tuple[UUID, UUID]:
        conn = await connect(migrated_url)
        try:
            inv = await _seed_investigation(conn)
            alloc = await _seed_allocation(conn)
            sys_id = await _seed_system(
                conn,
                allocation_id=alloc,
                investigation_id=inv,
                state="ready",
                profile=_upload_profile(_CHECKSUM_X),
            )
            return inv, sys_id
        finally:
            await conn.close()

    inv, sys_id = asyncio.run(_seed())
    _write_overlay(tmp_path, sys_id)
    assert not _reclaimable(migrated_url, inv, tmp_path)  # condition (a) pins it


def test_failed_referencer_with_overlay_gone_drains(migrated_url: str, tmp_path: Path) -> None:
    # AC-8: a `failed` referencer (terminal, never reaches torn_down) whose overlay has been
    # reclaimed MUST drain — a state-keyed gate that pins `failed` forever is the regression.
    async def _seed() -> UUID:
        conn = await connect(migrated_url)
        try:
            inv = await _seed_investigation(conn)
            alloc = await _seed_allocation(conn)
            await _seed_system(
                conn,
                allocation_id=alloc,
                investigation_id=inv,
                state="failed",
                profile=_upload_profile(_CHECKSUM_X),
            )
            return inv
        finally:
            await conn.close()

    inv = asyncio.run(_seed())
    tmp_path.mkdir(parents=True, exist_ok=True)  # accessible root, no overlay file
    assert _reclaimable(migrated_url, inv, tmp_path)


def test_reprovisioning_referencer_pins_via_condition_b(migrated_url: str, tmp_path: Path) -> None:
    # AC-8c: overlay momentarily absent mid-re-materialize; condition (b) defers.
    async def _seed() -> UUID:
        conn = await connect(migrated_url)
        try:
            inv = await _seed_investigation(conn)
            alloc = await _seed_allocation(conn)
            await _seed_system(
                conn,
                allocation_id=alloc,
                investigation_id=inv,
                state="reprovisioning",
                profile=_upload_profile(_CHECKSUM_X),
            )
            return inv
        finally:
            await conn.close()

    inv = asyncio.run(_seed())
    tmp_path.mkdir(parents=True, exist_ok=True)
    assert not _reclaimable(migrated_url, inv, tmp_path)  # pinned by state, no overlay needed


def test_unrelated_and_catalog_systems_do_not_pin(migrated_url: str, tmp_path: Path) -> None:
    # AC-8f: a live System referencing a DIFFERENT checksum or a catalog rootfs is not a
    # referencer of X — X reclaims even while that System stays live with an overlay present.
    async def _seed() -> tuple[UUID, UUID, UUID]:
        conn = await connect(migrated_url)
        try:
            inv = await _seed_investigation(conn)
            alloc = await _seed_allocation(conn)
            other = await _seed_system(
                conn,
                allocation_id=alloc,
                investigation_id=inv,
                state="ready",
                profile=_upload_profile(_CHECKSUM_Y),
            )
            cat = await _seed_system(
                conn,
                allocation_id=alloc,
                investigation_id=inv,
                state="ready",
                profile=_catalog_profile(),
            )
            return inv, other, cat
        finally:
            await conn.close()

    inv, other, cat = asyncio.run(_seed())
    _write_overlay(tmp_path, other)
    _write_overlay(tmp_path, cat)
    assert _reclaimable(migrated_url, inv, tmp_path)


def test_torn_down_referencer_is_excluded(migrated_url: str, tmp_path: Path) -> None:
    async def _seed() -> tuple[UUID, UUID]:
        conn = await connect(migrated_url)
        try:
            inv = await _seed_investigation(conn)
            alloc = await _seed_allocation(conn)
            sys_id = await _seed_system(
                conn,
                allocation_id=alloc,
                investigation_id=inv,
                state="torn_down",
                profile=_upload_profile(_CHECKSUM_X),
            )
            return inv, sys_id
        finally:
            await conn.close()

    inv, sys_id = asyncio.run(_seed())
    _write_overlay(tmp_path, sys_id)  # even with an overlay, torn_down is not enumerated
    assert _reclaimable(migrated_url, inv, tmp_path)


def test_the_state_classifier_is_not_widened_by_the_fetch_lease(
    migrated_url: str, tmp_path: Path
) -> None:
    # The AC-8 reconciliation for #1702/ADR-0515, asserted rather than left implicit.
    #
    # The issue's title proposes widening *this* classifier so a torn_down/failed System keeps
    # pinning. That is the one shape AC-8 forbids: `failed` is terminal with no transition out of
    # it, nothing removes a failed System's overlay, and `torn_down` is the achieved post-state — so
    # a state-keyed pin on either has nothing that can ever release it, and the base leaks until an
    # operator intervenes. ADR-0515 therefore puts the new evidence in a rootfs_fetch_leases row
    # read by the per-checksum gate — fenced on its holding job since ADR-0522 — and leaves this
    # classifier alone.
    #
    # This pins that separation of duties. A live fetch lease exists for the very token this failed
    # System references, and `rootfs_base_reclaimable` still says "reclaimable" — because answering
    # the liveness question is not its job. Were someone to "simplify" ADR-0515 by adding FAILED to
    # ROOTFS_BASE_PRE_OVERLAY_SYSTEM_STATES, this reddens alongside
    # test_failed_referencer_with_overlay_gone_drains above.
    async def _seed() -> UUID:
        conn = await connect(migrated_url)
        try:
            inv = await _seed_investigation(conn)
            alloc = await _seed_allocation(conn)
            system_id = await _seed_system(
                conn,
                allocation_id=alloc,
                investigation_id=inv,
                state="failed",
                profile=_upload_profile(_CHECKSUM_X),
            )
            job_id = uuid4()
            await conn.execute(
                "INSERT INTO jobs (id, kind, state, max_attempts, authorizing, dedup_key, "
                "lease_expires_at) VALUES (%s, 'provision', 'running', 3, %s, %s, "
                "now() + interval '5 minutes')",
                (job_id, Jsonb({"principal": "p", "project": "proj"}), f"lease-{job_id}"),
            )
            await conn.execute(
                "INSERT INTO rootfs_fetch_leases "
                "(id, investigation_id, token, system_id, job_id) VALUES (%s, %s, %s, %s, %s)",
                (uuid4(), inv, _TOKEN_X, system_id, job_id),
            )
            return inv
        finally:
            await conn.close()

    inv = asyncio.run(_seed())
    tmp_path.mkdir(parents=True, exist_ok=True)
    assert _reclaimable(migrated_url, inv, tmp_path)
